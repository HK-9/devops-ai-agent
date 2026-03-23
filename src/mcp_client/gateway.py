"""
MCP Security Gateway.

Sits between AgentCore and the MCPClient, intercepting every tool call
to enforce:

  1. Tool allowlist   — only explicitly permitted tools may be invoked.
  2. Guardrails       — destructive / privileged operations are blocked.
  3. Input sanitizer  — injection-pattern detection on every argument.
  4. Rate limiter     — per-tool token-bucket (calls / minute).
  5. Audit logger     — structured record for every call + decision.

Architecture::

    AgentCore
        │
        ▼
    MCPGateway.call_tool()   ← security checks run here
        │
        ▼
    MCPClient.call_tool()    ← only reached if all checks pass
        │
        ▼
    MCP Server subprocess → AWS

Usage::

    gateway = MCPGateway(mcp_client)
    result  = await gateway.call_tool("list_ec2_instances", {...})
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any

from src.mcp_client.client import MCPClient
from src.utils.aws_helpers import setup_logging

logger = setup_logging("mcp-gateway")


# ── Policy tables ────────────────────────────────────────────────────────

# Tools the agent is explicitly allowed to call.
# Any tool NOT in this set is rejected at the gateway.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        # AWS Infra (read)
        "list_ec2_instances",
        "describe_ec2_instance",
        "get_instance_details",
        # AWS Infra (limited write — restart only)
        "restart_ec2_instance",
        # Monitoring (read)
        "get_cpu_metrics",
        "get_memory_metrics",
        "get_disk_usage",
        "get_cloudwatch_alarms",
        "list_cloudwatch_alarms",
        "get_metric_statistics",
        # Notification (write — allowed for alarm context only)
        "send_teams_message",
        "send_alert_with_failover",
        "create_incident_notification",
    }
)

# Tools that are always blocked regardless of context.
# Add anything destructive, IAM-related, or overly privileged here.
BLOCKED_TOOLS: frozenset[str] = frozenset(
    {
        "terminate_ec2_instance",
        "delete_ec2_instance",
        "stop_ec2_instance",
        "create_iam_role",
        "delete_iam_role",
        "attach_iam_policy",
        "detach_iam_policy",
        "create_user",
        "delete_user",
        "put_bucket_policy",
        "delete_s3_object",
        "delete_s3_bucket",
        "invoke_lambda",
        "execute_command",
        "run_command",
        "send_command",
        "assume_role",
    }
)

# Read-only tools — safe even without alarm context.
READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "list_ec2_instances",
        "describe_ec2_instance",
        "get_instance_details",
        "get_cpu_metrics",
        "get_memory_metrics",
        "get_disk_usage",
        "get_cloudwatch_alarms",
        "list_cloudwatch_alarms",
        "get_metric_statistics",
    }
)

# Rate limit: max calls per tool within the rolling window (seconds).
# Format: tool_name → (max_calls, window_seconds)
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "restart_ec2_instance":      (3,  60),   # max 3 restarts / minute
    "send_alert_with_failover":  (5,  60),   # max 5 alerts / minute
    "send_teams_message":        (10, 60),   # max 10 Teams messages / minute
    "create_incident_notification": (10, 60),
    # Default for all other tools
    "__default__":               (30, 60),   # max 30 calls / minute
}

# Regex patterns that trigger injection detection in argument values.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[;&|`$]"),                      # shell metacharacters
    re.compile(r"\.\./"),                         # path traversal
    re.compile(r"<script", re.IGNORECASE),        # XSS
    re.compile(r"(?:drop|delete|truncate)\s+table", re.IGNORECASE),  # SQL
    re.compile(r"\bexec\s*\(", re.IGNORECASE),   # code execution
    re.compile(r"ignore\s+previous\s+instructions", re.IGNORECASE),  # prompt injection
    re.compile(r"system\s*prompt", re.IGNORECASE),  # prompt extraction
]


# ── Rate limiter state ───────────────────────────────────────────────────

# tool_name → list of call timestamps
_call_timestamps: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(tool_name: str) -> tuple[bool, str]:
    """Return (allowed, reason). Prunes old timestamps in place."""
    max_calls, window = _RATE_LIMITS.get(tool_name, _RATE_LIMITS["__default__"])
    now = time.monotonic()
    cutoff = now - window
    timestamps = _call_timestamps[tool_name]

    # Prune expired entries
    _call_timestamps[tool_name] = [t for t in timestamps if t > cutoff]
    current_count = len(_call_timestamps[tool_name])

    if current_count >= max_calls:
        return False, (
            f"Rate limit exceeded for '{tool_name}': "
            f"{current_count}/{max_calls} calls in the last {window}s"
        )

    _call_timestamps[tool_name].append(now)
    return True, ""


# ── Input sanitizer ──────────────────────────────────────────────────────

def _sanitize_arguments(
    tool_name: str, arguments: dict[str, Any]
) -> tuple[bool, str]:
    """Check every string argument for injection patterns.

    Returns (safe, reason).
    """
    for param, value in arguments.items():
        if not isinstance(value, str):
            continue
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(value):
                return False, (
                    f"Suspicious input detected in '{param}' for tool '{tool_name}': "
                    f"matched pattern '{pattern.pattern}' — value: {value!r:.120}"
                )
    return True, ""


# ── Audit logger ─────────────────────────────────────────────────────────

def _audit(
    *,
    tool: str,
    arguments: dict[str, Any],
    decision: str,          # "ALLOW" | "BLOCK"
    reason: str = "",
    result_summary: str = "",
    session_id: str = "",
) -> None:
    """Emit a structured security audit record."""
    import json as _json
    record = {
        "event": "mcp_gateway",
        "tool": tool,
        "decision": decision,
        "reason": reason or "ok",
        "session_id": session_id,
        "args_preview": _json.dumps(
            {k: str(v)[:80] for k, v in arguments.items()}, default=str
        ),
        "result_summary": result_summary[:200] if result_summary else "",
    }
    if decision == "ALLOW":
        logger.info("[GATEWAY ALLOW] %s", _json.dumps(record))
    else:
        logger.warning("[GATEWAY BLOCK] %s", _json.dumps(record))


# ── Gateway ──────────────────────────────────────────────────────────────


class MCPGateway:
    """Security gateway wrapping MCPClient.

    Drop-in replacement for direct ``MCPClient.call_tool()`` calls.
    All enforcement runs synchronously before the async MCP call.

    Args:
        mcp_client:  The underlying MCPClient to delegate allowed calls to.
        readonly:    When True, only READONLY_TOOLS may be invoked.
                     Notification / write tools are blocked.
                     Set to True in chat mode; False in alarm mode.
    """

    def __init__(self, mcp_client: MCPClient, *, readonly: bool = False) -> None:
        self._mcp = mcp_client
        self.readonly = readonly

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Enforce all gateway policies, then delegate to MCPClient."""
        args = arguments or {}

        # ── 1. Hard block list ───────────────────────────────────────
        if name in BLOCKED_TOOLS:
            reason = f"Tool '{name}' is in the gateway hard-block list"
            _audit(tool=name, arguments=args, decision="BLOCK",
                   reason=reason, session_id=session_id)
            return {"error": True, "blocked": True, "message": reason}

        # ── 2. Allowlist ─────────────────────────────────────────────
        if name not in ALLOWED_TOOLS:
            reason = f"Tool '{name}' is not in the gateway allowlist"
            _audit(tool=name, arguments=args, decision="BLOCK",
                   reason=reason, session_id=session_id)
            return {"error": True, "blocked": True, "message": reason}

        # ── 3. Readonly guardrail ────────────────────────────────────
        if self.readonly and name not in READONLY_TOOLS:
            reason = (
                f"Tool '{name}' is a write/notification operation — "
                "blocked in read-only (chat) mode"
            )
            _audit(tool=name, arguments=args, decision="BLOCK",
                   reason=reason, session_id=session_id)
            return {"error": True, "blocked": True, "message": reason}

        # ── 4. Input sanitisation ────────────────────────────────────
        safe, inject_reason = _sanitize_arguments(name, args)
        if not safe:
            _audit(tool=name, arguments=args, decision="BLOCK",
                   reason=inject_reason, session_id=session_id)
            return {"error": True, "blocked": True, "message": inject_reason}

        # ── 5. Rate limiting ─────────────────────────────────────────
        allowed, rate_reason = _check_rate_limit(name)
        if not allowed:
            _audit(tool=name, arguments=args, decision="BLOCK",
                   reason=rate_reason, session_id=session_id)
            return {"error": True, "blocked": True, "message": rate_reason}

        # ── All checks passed — delegate ─────────────────────────────
        _audit(tool=name, arguments=args, decision="ALLOW",
               session_id=session_id)
        result = await self._mcp.call_tool(name, args)

        # Log a brief result summary (errors are always surfaced)
        if result.get("error"):
            logger.warning("[GATEWAY] Tool %s returned error: %s",
                           name, result.get("message", ""))

        return result

    def set_readonly(self, readonly: bool) -> None:
        """Switch readonly mode at runtime (e.g. between alarm and chat)."""
        self.readonly = readonly
 