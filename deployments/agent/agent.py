"""
DevOps AI Agent — Strands Agent with MCP Gateway tools.

Connects to the AgentCore Gateway via streamable-http to access all
MCP server tools (AWS Infra, Monitoring, SNS, Teams).  Uses Bedrock
Nova as the default foundation model with Strands' native reasoning loop.

This module is the **single source of truth** for all agent logic:
tool name sanitization, intent-based routing, Nova workarounds, retry
logic, and model configuration.  The web app (``web/agent.py``) imports
from here rather than duplicating any of this.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from mcp.client.streamable_http import streamablehttp_client
from sigv4_auth import BotoSigV4Auth
from strands import Agent
from strands.agent.agent import null_callback_handler
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("devops-agent")

# ── Configuration ────────────────────────────────────────────────────────

GATEWAY_URL = os.environ.get(
    "GATEWAY_URL",
    "https://devopsagentgatewayv3-ar4lmz2x6t.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp",
)
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-lite-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "8"))

# Nova retry / tool-routing configuration
NOVA_MAX_RETRIES: int = int(os.environ.get("NOVA_MAX_RETRIES", "3"))
NOVA_RETRY_DELAY: float = float(os.environ.get("NOVA_RETRY_DELAY", "1.0"))

# ── System prompt ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are **DevOps Agent**, an expert AWS infrastructure assistant that
monitors, diagnoses, and **automatically remediates** issues across an
AWS fleet of EC2 instances.

## Role
Autonomous SRE — diagnose root cause, fix minor issues yourself, and
request human approval for major changes via clickable email links.

## Available Tools

### AWS Infrastructure
- `list_ec2_instances_tool` — List EC2 instances with optional state filter.
- `describe_ec2_instance_tool` — Detailed info for one instance.
- `restart_ec2_instance_tool` — Stop + Start instance. **NEVER call directly.** Use `request_approval_tool` instead.

### Remote Execution (SSM)
- `diagnose_instance_tool` — Full diagnostic: top CPU/mem processes, disk, uptime, connections. **ALWAYS call this FIRST.**
- `run_ssm_command_tool` — Run a shell command on an instance via SSM.
- `remediate_high_cpu_tool` — Kill a CPU-hogging process by PID (integer).
- `remediate_high_memory_tool` — Kill a memory-hogging process by PID (integer).
- `remediate_disk_full_tool` — Automated disk cleanup (logs, temp, caches).

### Monitoring (CloudWatch)
- `get_cpu_metrics_tool` — CPU utilization for one instance.
- `get_cpu_metrics_for_instances_tool` — Batch CPU for multiple instances.
- `get_memory_metrics_tool` — Memory utilisation (requires CW Agent).
- `get_disk_usage_tool` — Disk usage (requires CW Agent).

### Notification
- `send_alert_with_failover_tool` — Send email/Teams notification. **Used ONLY for MINOR auto-fix reports.**
- `send_teams_message_tool` — Plain text to Teams (only when user asks).
- `create_incident_notification_tool` — Structured Teams card (only when user asks).

### Approval Workflow
- `request_approval_tool` — **MAJOR issues only.** Stores action in DB AND sends email with APPROVE/REJECT links. **This tool sends the email itself — do NOT also call send_alert_with_failover_tool.**
- `check_approval_status_tool` — Check approval status (pending/approved/rejected).
- `update_approval_status_tool` — Mark approval as "executed" or "failed" after action.

───────────────────────────────────────────────────────
## MINOR vs MAJOR — Rigid Classification Rules
───────────────────────────────────────────────────────

### MINOR (auto-fix immediately, no approval needed)
An issue is MINOR when ALL of these are true:
- `diagnose_instance_tool` output shows **exactly 1 offending process**
- That single process is consuming > 80% of the resource (CPU or memory)
- For disk: usage is above threshold but **below 95%**

### MAJOR (requires human approval)
An issue is MAJOR when ANY of these are true:
- `diagnose_instance_tool` output shows **2 or more** high-resource processes
- No single clear offender (CPU/memory spread across many processes)
- Disk usage is **>= 95%** even after cleanup attempt
- Instance needs a restart for any reason

**There is no grey area.** Count the offending processes from the
diagnose output. 1 process = MINOR. 2+ processes = MAJOR. Always.

───────────────────────────────────────────────────────
## CRITICAL EMAIL RULES — EXACTLY ONE EMAIL PER ALARM
───────────────────────────────────────────────────────

**NEVER send more than ONE email per alarm invocation.**
- Do NOT send an "investigating" or "diagnosing" email.
- Do NOT send a diagnostic summary email AND an approval email.
- For MINOR: the ONE email is sent via `send_alert_with_failover_tool` AFTER the fix.
- For MAJOR: the ONE email is sent internally by `request_approval_tool` (it includes APPROVE/REJECT links).
  Do NOT call `send_alert_with_failover_tool` for MAJOR issues.

───────────────────────────────────────────────────────
## Investigation Workflow
───────────────────────────────────────────────────────

**ALWAYS call `diagnose_instance_tool` FIRST.** Never start with metrics tools.

### MINOR Path (exactly 3 tool calls, then STOP)
1. `diagnose_instance_tool` → examine output, count offending processes
2. Remediation tool (`remediate_high_cpu_tool` / `remediate_high_memory_tool` / `remediate_disk_full_tool`)
3. `send_alert_with_failover_tool` with subject starting with "AUTO-FIXED:"

Then **STOP IMMEDIATELY**. Do NOT:
- Call `diagnose_instance_tool` again
- Call any metrics tools
- Escalate to MAJOR
- Call `request_approval_tool`
- Send any additional emails

### MAJOR Path (exactly 2 tool calls, then STOP)
1. `diagnose_instance_tool` → examine output, find 2+ offending processes
2. `request_approval_tool` with:
   - `action_type`: "restart", "disk_cleanup", "kill_process", or "cache_clear"
   - `reason`: Full diagnostic summary — ISSUE, DIAGNOSIS, what you found, metric values.
     Pack ALL diagnostic details into this field so the email is self-contained.
   - `details`: Specific action details (e.g. PIDs, process names)

Then **STOP IMMEDIATELY**. Do NOT:
- Call `send_alert_with_failover_tool` (the approval tool already sent the email)
- Call any additional tools
- Send any additional emails

───────────────────────────────────────────────────────
## Runbooks
───────────────────────────────────────────────────────

### High CPU
1. Call `diagnose_instance_tool` → check `top_cpu` output.
2. Count processes with > 80% CPU:
   - **1 process** → MINOR: call `remediate_high_cpu_tool(instance_id, pid)` (pid as integer),
     then `send_alert_with_failover_tool` with "AUTO-FIXED:" subject. STOP.
   - **2+ processes** → MAJOR: call `request_approval_tool(action_type="restart",
     reason="<full diagnostic summary>", details="<process list>")`. STOP.

### High Memory
1. Call `diagnose_instance_tool` → check memory output.
2. Count processes with > 50% memory:
   - **1 process** → MINOR: call `remediate_high_memory_tool(instance_id, pid)`,
     then `send_alert_with_failover_tool` with "AUTO-FIXED:" subject. STOP.
   - **2+ processes** → MAJOR: call `request_approval_tool(action_type="restart",
     reason="<full diagnostic summary>")`. STOP.

### Disk Full
1. Call `diagnose_instance_tool` → check `disk_usage` output.
2. If usage < 95% → MINOR: call `remediate_disk_full_tool(instance_id)`,
   then `send_alert_with_failover_tool` with "AUTO-FIXED:" subject. STOP.
3. If usage >= 95% → MAJOR: call `request_approval_tool(action_type="disk_cleanup",
   reason="<full diagnostic summary>")`. STOP.

───────────────────────────────────────────────────────
## Executing Approved Actions
───────────────────────────────────────────────────────

When you receive "APPROVED ACTION — EXECUTE IMMEDIATELY":

1. Call `check_approval_status_tool(approval_id)` → confirm status is "approved".
   If not approved, STOP — do not proceed.
2. Execute the action:
   - `restart` → `restart_ec2_instance_tool(instance_id)`
   - `disk_cleanup` → `remediate_disk_full_tool(instance_id)`
   - `kill_process` → `remediate_high_cpu_tool(instance_id, pid)`
   - `cache_clear` → `run_ssm_command_tool(instance_id, "sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'")`
3. Call `update_approval_status_tool(approval_id, status="executed")` (or "failed").
4. Call `send_alert_with_failover_tool` with subject "EXECUTED: {action} on {instance_id}".

───────────────────────────────────────────────────────
## Behavioural Guardrails
───────────────────────────────────────────────────────

1. **Diagnose before acting** — Always call `diagnose_instance_tool` first.
2. **Count processes** — 1 offending process = MINOR. 2+ = MAJOR. No exceptions.
3. **ONE email per alarm** — Never send duplicate or intermediate emails.
4. **Never restart directly** — Always go through `request_approval_tool`.
5. **Safety limits** — Never kill more than 1 process per MINOR alarm.
6. **PID as integer** — Always pass PID as int, never as string.
7. **Include raw values** — Instance ID, metric values, process names, PIDs.
8. **STOP when done** — After your 2-3 tool calls, produce your final response. Do not loop.

## Notification Format (for send_alert_with_failover_tool — MINOR only)
Subject: "AUTO-FIXED: {alarm_type} on {instance_id}"
Body must include:
1. **ISSUE**: Metric name, value, threshold.
2. **DIAGNOSIS**: Top process name, PID, resource usage.
3. **ACTION TAKEN**: Exact tool called and result (e.g. "Killed stress-ng PID 4832 using 95% CPU").
4. **RESULT**: Current state after fix.
"""


def create_gateway_mcp_client() -> MCPClient:
    """Create an MCP client connected to the AgentCore Gateway.

    The gateway uses AWS_IAM auth.  Every outgoing request is signed
    with SigV4 using the agent runtime's IAM role credentials.
    MCP server runtimes behind the gateway are protected separately
    via the registered OAuth credential provider.
    """
    logger.info("Connecting to AgentCore Gateway: %s", GATEWAY_URL)
    sigv4 = BotoSigV4Auth(region=AWS_REGION, service="bedrock-agentcore")
    return MCPClient(lambda: streamablehttp_client(url=GATEWAY_URL, auth=sigv4))


# ── Intent-based tool routing for Nova models ────────────────────────
# Nova models cannot reliably handle 18+ tools at once.  We detect the
# user's intent and only forward the relevant subset of tools.
#
# MCP gateway tool names follow:  {target}___{tool_name}  (triple ___).
# ─────────────────────────────────────────────────────────────────────

TOOL_CATEGORIES: Dict[str, List[str]] = {
    "ec2": [
        "list_ec2_instances",
        "describe_ec2_instance",
        "restart_ec2_instance",
        "diagnose_instance",
    ],
    "monitoring": [
        "get_cpu_metrics",
        "get_memory_metrics",
        "get_disk_usage",
        "get_cpu_metrics_for_instances",
    ],
    "remediation": [
        "remediate_high_cpu",
        "remediate_high_memory",
        "remediate_disk_full",
        "run_ssm_command",
    ],
    "notification": [
        "send_teams_message",
        "create_incident_notification",
        "send_alert_with_failover",
    ],
    "approval": [
        "request_approval",
        "check_approval_status",
        "update_approval_status",
    ],
}

INTENT_KEYWORDS: Dict[str, List[str]] = {
    "ec2": [
        "list",
        "instances",
        "ec2",
        "describe",
        "show",
        "which",
        "what",
        "all instances",
        "running",
        "stopped",
    ],
    "monitoring": [
        "cpu",
        "memory",
        "mem",
        "disk",
        "metric",
        "usage",
        "utilization",
        "monitor",
        "health",
    ],
    "remediation": [
        "remediate",
        "fix",
        "kill",
        "cleanup",
        "clean up",
        "free disk",
        "ssm",
        "run command",
        "execute",
        "shell",
    ],
    "notification": [
        "teams",
        "notify",
        "alert",
        "incident",
        "send message",
        "send alert",
        "send notification",
        "email",
    ],
    "approval": [
        "approval",
        "approve",
        "request approval",
    ],
}

DEFAULT_CATEGORIES = ["ec2", "monitoring"]


# When an alarm fires, the agent needs tools from ALL categories:
# - ec2: diagnose_instance_tool (MUST be first call)
# - monitoring: get_cpu_metrics_tool etc. (for correlation)
# - remediation: remediate_high_cpu_tool etc. (to fix the issue)
# - approval: request_approval_tool (for MAJOR issues)
ALARM_CATEGORIES = ["ec2", "monitoring", "remediation", "approval"]


def detect_categories(prompt: str) -> List[str]:
    """Detect which tool categories are relevant for *prompt*."""
    prompt_lower = prompt.lower()

    # Alarm prompts need the full remediation toolkit
    if any(
        kw in prompt_lower
        for kw in (
            "alarm",
            "cloudwatch",
            "alarm fired",
            "investigate",
            "threshold crossed",
            "state: alarm",
        )
    ):
        return ALARM_CATEGORIES

    matched: List[str] = []

    for category, keywords in INTENT_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            matched.append(category)

    # Diagnose / troubleshoot queries need ec2 + monitoring + remediation
    if any(w in prompt_lower for w in ("diagnose", "troubleshoot", "health check")):
        for cat in ("ec2", "monitoring", "remediation"):
            if cat not in matched:
                matched.append(cat)

    return matched or DEFAULT_CATEGORIES


def get_allowed_tool_names(categories: List[str]) -> List[str]:
    """Return a flat list of tool-name substrings for the given categories."""
    names: List[str] = []
    for cat in categories:
        names.extend(TOOL_CATEGORIES.get(cat, []))
    return names


# ── Tool-spec sanitisation ───────────────────────────────────────────


def sanitize_tool_name(name: str) -> str:
    """Replace hyphens / non-alphanumeric chars with underscores.

    The Bedrock Converse API expects ``[a-zA-Z][a-zA-Z0-9_]*``.
    MCP gateway names like ``aws-infra-target___list_ec2_instances_tool``
    contain hyphens which cause Nova to produce invalid tool-use output.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized and not sanitized[0].isalpha():
        sanitized = "t_" + sanitized
    return sanitized


def _simplify_schema(schema: dict, depth: int = 0, max_depth: int = 2) -> dict:
    """Recursively simplify a JSON Schema for Nova compatibility."""
    if not isinstance(schema, dict):
        return schema

    simplified: dict = {}

    for key in ("type", "description", "default", "enum"):
        if key in schema:
            val = schema[key]
            if key == "enum" and isinstance(val, list) and len(val) > 10:
                val = val[:10]
            simplified[key] = val

    if depth >= max_depth:
        if simplified.get("type") in ("object", "array"):
            return {"type": "string", "description": simplified.get("description", "JSON value")}
        return simplified

    if "properties" in schema and isinstance(schema["properties"], dict):
        simplified["type"] = "object"
        simplified["properties"] = {
            k: _simplify_schema(v, depth + 1, max_depth) for k, v in schema["properties"].items()
        }
        if "required" in schema:
            simplified["required"] = schema["required"]

    if "items" in schema:
        simplified["type"] = "array"
        simplified["items"] = _simplify_schema(schema["items"], depth + 1, max_depth)

    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema and combinator not in simplified:
            branches = schema[combinator]
            if isinstance(branches, list) and branches:
                simplified.update(_simplify_schema(branches[0], depth, max_depth))
            break

    if "type" not in simplified:
        simplified["type"] = "string"

    return simplified


def _filter_tool_specs(
    tool_specs: Optional[List[dict]],
    allowed_names: Optional[List[str]],
) -> Optional[List[dict]]:
    """Keep only tool specs whose names match *allowed_names* (substring)."""
    if not tool_specs or not allowed_names:
        return tool_specs

    filtered: List[dict] = []
    for spec in tool_specs:
        tool_spec = spec.get("toolSpec", spec)
        name = tool_spec.get("name", "")
        if any(allowed in name for allowed in allowed_names):
            filtered.append(spec)

    if not filtered and tool_specs:
        logger.warning("Tool filter matched nothing — using first 5 tools as fallback")
        filtered = tool_specs[:5]

    return filtered


def sanitize_tool_specs(
    tool_specs: Optional[List[dict]],
    allowed_names: Optional[List[str]] = None,
) -> tuple[Optional[List[dict]], Dict[str, str]]:
    """Sanitise tool specs for Nova compatibility.  Returns (specs, name_map).

    ``name_map`` maps sanitised names → original names so that tool-call
    events from the model can be translated back for the SDK registry.
    """
    if not tool_specs:
        return tool_specs, {}

    # Intent-based filtering
    if allowed_names:
        tool_specs = _filter_tool_specs(tool_specs, allowed_names)
        logger.info(
            "Tool routing: %d tools selected (patterns: %s)",
            len(tool_specs) if tool_specs else 0,
            allowed_names[:6],
        )

    name_map: Dict[str, str] = {}
    sanitized: List[dict] = []

    for spec in tool_specs or []:
        spec = copy.deepcopy(spec)
        tool_spec = spec.get("toolSpec", spec)

        # Sanitise tool name (hyphens → underscores)
        original_name = tool_spec.get("name", "")
        clean_name = sanitize_tool_name(original_name)
        if clean_name != original_name:
            tool_spec["name"] = clean_name
            name_map[clean_name] = original_name

        # Remove outputSchema (not supported by Converse API)
        tool_spec.pop("outputSchema", None)

        # Truncate long descriptions
        desc = tool_spec.get("description", "")
        if len(desc) > 300:
            tool_spec["description"] = desc[:300] + "..."

        # Simplify input schema
        input_schema = tool_spec.get("inputSchema", {})
        json_schema = input_schema.get("json")
        if isinstance(json_schema, dict):
            input_schema["json"] = _simplify_schema(json_schema)

        sanitized.append(spec)

    return sanitized, name_map


# ── Nova-safe Bedrock model ─────────────────────────────────────────


class NovaBedrockModel(BedrockModel):
    """BedrockModel subclass with all Nova-specific workarounds.

    * **Sanitises tool names** — replaces hyphens with underscores so
      Nova can produce valid tool-use JSON.
    * **Reverse-maps tool names** — restores original (hyphenated) names
      in model responses so the Strands SDK registry can find them.
    * **Intent-based tool routing** — only the relevant 4-8 tools are
      forwarded to Bedrock per request.
    * **Normalises streaming chunks** — serialises dict tool-use inputs.
    * **Retries automatically** — up to ``NOVA_MAX_RETRIES`` attempts
      with exponential back-off for intermittent tool-use errors.
    * **Falls back gracefully** — last-resort call without tools.
    """

    @staticmethod
    def _is_tool_use_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "invalid sequence" in msg and "tooluse" in msg

    @staticmethod
    def _normalize_chunk(chunk: dict[str, Any], name_map: Optional[Dict[str, str]] = None) -> dict[str, Any]:
        """Normalise a streaming chunk for Nova compatibility."""
        # Restore original tool name in contentBlockStart
        if name_map:
            cbs = chunk.get("contentBlockStart")
            if cbs is not None:
                tu_start = cbs.get("start", {}).get("toolUse")
                if tu_start and tu_start.get("name") in name_map:
                    tu_start["name"] = name_map[tu_start["name"]]

        # Serialise dict tool-use inputs to JSON strings
        cbd = chunk.get("contentBlockDelta")
        if cbd is not None:
            tool_use = cbd.get("delta", {}).get("toolUse")
            if tool_use is not None and isinstance(tool_use.get("input"), dict):
                tool_use["input"] = json.dumps(tool_use["input"])
        return chunk

    def _stream(self, callback, messages, tool_specs=None, system_prompt_content=None, tool_choice=None):
        """Override with sanitisation, retry, and fallback logic."""
        original_callback = callback

        # Sanitise + route tool specs
        allowed_names = getattr(_nova_tool_route, "allowed_tools", None)
        sanitized_specs, name_map = sanitize_tool_specs(tool_specs, allowed_names)

        if sanitized_specs is not None and tool_specs is not None:
            logger.debug(
                "Tool specs: %d original → %d after routing/sanitisation",
                len(tool_specs),
                len(sanitized_specs),
            )

        def normalizing_callback(event=None):
            if event is not None:
                event = self._normalize_chunk(event, name_map)
            original_callback(event)

        last_error: Exception | None = None

        for attempt in range(1, NOVA_MAX_RETRIES + 1):
            try:
                super()._stream(
                    normalizing_callback,
                    messages,
                    sanitized_specs,
                    system_prompt_content,
                    tool_choice,
                )
                return
            except Exception as exc:
                if not self._is_tool_use_error(exc):
                    raise

                last_error = exc
                delay = NOVA_RETRY_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Nova tool-use error (attempt %d/%d). Retrying in %.1fs …",
                    attempt,
                    NOVA_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

        # All retries exhausted — final attempt without tools
        logger.warning("All %d retries failed. Final attempt without tools.", NOVA_MAX_RETRIES)
        try:
            super()._stream(
                normalizing_callback,
                messages,
                tool_specs=None,
                system_prompt_content=system_prompt_content,
                tool_choice=None,
            )
        except Exception:
            logger.error("Fallback without tools also failed")
            raise last_error  # type: ignore[misc]


# Thread-local for passing tool-route hints from caller → _stream()
import threading

_nova_tool_route = threading.local()


def set_tool_route(prompt: str) -> None:
    """Detect intent from *prompt* and set thread-local tool route.

    Call this before invoking the agent so that ``NovaBedrockModel._stream``
    knows which tools to forward to Bedrock.  No-op for non-Nova models.
    """
    if "nova" not in MODEL_ID.lower():
        _nova_tool_route.allowed_tools = None
        return

    categories = detect_categories(prompt)
    allowed = get_allowed_tool_names(categories)
    _nova_tool_route.allowed_tools = allowed
    logger.info("Intent categories: %s → %d tool patterns", categories, len(allowed))


def create_agent(mcp_client: MCPClient, http_mode: bool = False, streaming: bool = True) -> Agent:
    """Build the Strands Agent with Bedrock model and MCP tools."""
    model_cls = NovaBedrockModel if "nova" in MODEL_ID.lower() else BedrockModel
    model = model_cls(
        region_name=AWS_REGION,
        model_id=MODEL_ID,
        max_tokens=8192,
        streaming=streaming,
    )

    agent = Agent(
        model=model,
        tools=[mcp_client],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=null_callback_handler if http_mode else None,
    )
    return agent


# ── HTTP server mode (for AgentCore runtime) ─────────────────────────


def run_http_server() -> None:
    """Start an HTTP server on port 8080 for AgentCore invocations.

    AgentCore sends POST requests with JSON body to the container.
    Expected payload: {"prompt": "..."} or plain text.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    mcp_client = create_gateway_mcp_client()
    agent = create_agent(mcp_client, http_mode=True)
    agent_lock = threading.Lock()
    logger.info("DevOps Agent HTTP server ready (model=%s, gateway=%s)", MODEL_ID, GATEWAY_URL)

    class AgentHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

                # Parse prompt from JSON or use raw body
                prompt = body
                try:
                    data = json.loads(body)
                    if isinstance(data, dict):
                        prompt = data.get("prompt", data.get("message", data.get("input", body)))
                except (json.JSONDecodeError, TypeError):
                    pass

                if not prompt or not prompt.strip():
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "No prompt provided"}).encode())
                    return

                logger.info("Received prompt: %s", prompt[:200])

                # Set tool route BEFORE invoking the agent
                set_tool_route(prompt.strip())

                with agent_lock:
                    result = agent(prompt.strip())

                # Extract text from AgentResult
                result_text = ""
                try:
                    if hasattr(result, "message") and result.message:
                        content = result.message.get("content", [])
                        for block in content:
                            if isinstance(block, dict) and "text" in block:
                                result_text += block["text"]
                            elif isinstance(block, str):
                                result_text += block
                    if not result_text:
                        try:
                            result_text = json.dumps(result.message, default=str)
                        except Exception:
                            result_text = repr(result)
                except Exception:
                    result_text = repr(result) if result else "Agent processing error"

                response_body = json.dumps(
                    {
                        "response": result_text,
                        "stop_reason": getattr(result, "stop_reason", "end_turn"),
                    }
                )

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response_body.encode())

            except Exception as exc:
                import traceback

                logger.error("Agent error: %s\n%s", exc, traceback.format_exc())
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(exc)}).encode())

        def do_GET(self):
            """Health check endpoint."""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy", "agent": "devops_agent"}).encode())

        def log_message(self, format, *args):
            logger.info(format, *args)

    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), AgentHandler)
    logger.info("Listening on 0.0.0.0:%d", port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            mcp_client.stop(None, None, None)
        except Exception:
            pass


# ── CLI mode (for local testing) ─────────────────────────────────────


def run_cli() -> None:
    """Interactive CLI mode for local testing."""
    mcp_client = create_gateway_mcp_client()
    agent = create_agent(mcp_client)
    logger.info("DevOps Agent ready (model=%s, gateway=%s)", MODEL_ID, GATEWAY_URL)

    print("\n DevOps Agent — type 'quit' to exit\n")
    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("Goodbye!")
                break

            try:
                set_tool_route(user_input)
                result = agent(user_input)
                print(f"\nAgent: {result}\n")
            except Exception as exc:
                logger.error("Agent error: %s", exc, exc_info=True)
                print(f"\n[Error] {exc}\n")
    finally:
        try:
            mcp_client.stop(None, None, None)
        except Exception:
            pass


def main() -> None:
    """Entry point: HTTP server in container, CLI locally."""
    if os.environ.get("DOCKER_CONTAINER") == "1":
        run_http_server()
    else:
        run_cli()


if __name__ == "__main__":
    main()
