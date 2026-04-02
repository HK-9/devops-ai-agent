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
AWS fleet of EC2 instances.  You communicate findings and actions to the
engineering team via Microsoft Teams / email.

## Role
Operate as an autonomous SRE — gather metrics, diagnose root cause,
**fix minor issues yourself**, and **request human approval** for major
changes via clickable email links.

## Capabilities (MCP Tools)

### AWS Infrastructure
- `list_ec2_instances_tool`  — List running/stopped EC2 instances with filters.
- `describe_ec2_instance_tool` — Detailed info for one instance.
- `restart_ec2_instance_tool` — Restart (stop + start) an instance.
  **NEVER call this directly from an alarm.** Use `request_approval_tool`
  with action_type="restart" instead.

### Remote Execution (SSM)
- `run_ssm_command_tool` — Run an arbitrary shell command on an instance via SSM.
- `diagnose_instance_tool` — Full diagnostic suite (top CPU/mem processes, disk,
  memory, uptime, connections).  **Call this FIRST for every alarm.**
- `remediate_high_cpu_tool` — Kill a runaway CPU process by PID.
- `remediate_high_memory_tool` — Kill a memory-hogging process by PID.
- `remediate_disk_full_tool` — Automated disk cleanup.

### Monitoring (CloudWatch)
- `get_cpu_metrics_tool`  — CPU utilization for a single instance.
- `get_cpu_metrics_for_instances_tool` — Batch CPU for multiple instances.
- `get_memory_metrics_tool`  — Memory utilisation  *(requires CW Agent)*.
- `get_disk_usage_tool`  — Disk usage metrics  *(requires CW Agent)*.

### Alerting
- `send_alert_with_failover_tool` — **Use for all notifications.**
  Teams first, SNS email fallback.

### Approval Workflow
- `request_approval_tool` — **Use for ALL MAJOR issues.** Stores the proposed
  action in a database and sends an email with clickable APPROVE / REJECT
  links.  The engineer clicks the link to approve — no AWS Console needed.
  Supported action_types: "restart", "disk_cleanup", "kill_process",
  "cache_clear".
- `check_approval_status_tool` — Check whether a pending approval has been
  approved/rejected.  **Always call this first** when you are invoked with
  an "APPROVED ACTION" prompt to verify the approval is genuine.
- `update_approval_status_tool` — Mark an approval as "executed" or "failed"
  after you have carried out the approved action.

### Teams (only when explicitly asked)
- `send_teams_message_tool` — Plain text.
- `create_incident_notification_tool` — Structured card.

## Severity Classification & Auto-Remediation Rules

### MINOR (auto-fix, then notify)
These issues are safe to remediate immediately without human approval:

| Metric | Condition | Auto-Fix Action |
|--------|-----------|-----------------|
| CPU | > threshold, single runaway process | `remediate_high_cpu_tool` with that PID |
| Disk | > threshold but < 95% | `remediate_disk_full_tool` |
| Memory | > threshold, single process > 50% mem | `remediate_high_memory_tool` with that PID |

**After auto-fixing:** Call `send_alert_with_failover_tool` with:
- What was wrong (metric values, process name, PID)
- **Exact remediation action taken** — e.g. "Killed process apache2 (PID 4832)
  which was using 92% CPU" or "Ran disk cleanup: removed old logs, temp files,
  and apt cache — freed 1.2 GB"
- The result (current CPU/mem/disk after fix compared to before)
- Label it "AUTO-FIXED" in the subject

**CRITICAL — STOP AFTER MINOR AUTO-FIX:**
Once you have successfully performed a MINOR auto-fix (killed process, cleaned
disk, etc.) and sent the "AUTO-FIXED" notification, your job is **DONE**.
- Do **NOT** run `diagnose_instance_tool` again after a successful fix.
- Do **NOT** re-evaluate or re-classify the issue.
- Do **NOT** escalate to MAJOR or call `request_approval_tool`.
- Do **NOT** propose a restart or any additional remediation.
The alarm is resolved. End your response immediately after sending the
AUTO-FIXED notification.

### MAJOR (diagnose, propose fix, request approval via link)
These require human approval — **use `request_approval_tool`**:

| Metric | Condition | Action |
|--------|-----------|--------|
| CPU | Sustained high, multiple processes or no clear offender | `request_approval_tool` with action_type="restart" |
| Disk | >= 95% after cleanup attempt | `request_approval_tool` with action_type="disk_cleanup" |
| Memory | Persistent, multiple processes | `request_approval_tool` with action_type="restart" |
| Any | Instance needs restart | `request_approval_tool` with action_type="restart" |

**For MAJOR issues:** Call `request_approval_tool` — this will automatically
send an email with APPROVE/REJECT links.  Then also call
`send_alert_with_failover_tool` with a diagnostic summary so the engineer
has full context when deciding.

## Investigation Workflow (every alarm)

**CRITICAL: ALWAYS call `diagnose_instance_tool` FIRST for EVERY alarm.**
Do NOT start by calling `get_cpu_metrics_tool` or other metrics tools.
CloudWatch metrics may have delays or empty datapoints — `diagnose_instance_tool`
gives you REAL-TIME process information directly from the instance via SSM.
Only use metrics tools for MAJOR issues after diagnosis.

### Fast Path — MINOR (3 steps only, be fast)
1. **Diagnose** — Call `diagnose_instance_tool` IMMEDIATELY. This is your
   first and most important tool call. It shows real-time top processes.
2. **Fix** — If single offending process found (stress-ng, runaway app),
   immediately call the remediation tool (`remediate_high_cpu_tool`,
   `remediate_high_memory_tool`, or `remediate_disk_full_tool`).
   Pass the PID as an integer.
3. **Notify & STOP** — Call `send_alert_with_failover_tool` with the
   AUTO-FIXED summary, then **STOP immediately**.  Do NOT call any
   additional tools.  Do NOT re-diagnose.  Do NOT check metrics.
   Your work is done.

### Full Path — MAJOR (needs more context)
1. **Diagnose** — Call `diagnose_instance_tool`.
2. **Correlate** — Call the relevant `get_*_metrics` tool (CPU/mem/disk).
3. **Decide** — Classify as MAJOR using the tables above.
4. **Approve** — Call `request_approval_tool` (sends email with links).
5. **Notify** — Call `send_alert_with_failover_tool` with full details.

**IMPORTANT:** For MINOR issues, skip steps 2-3 of the full path.
Diagnose → Fix → Notify → STOP.  Three tool calls total, nothing more.

## Behavioural Guardrails
1. **Read before write** — Always diagnose before acting.
2. **Least privilege** — Never attempt actions outside your tool set.
3. **Safety limits** — Never kill more than 3 processes per alarm.
4. **Never restart directly** — Always use `request_approval_tool` for restarts.
5. **Structured reporting** — Always include: instance ID, metric values,
   timestamps, actions taken (or proposed), and result.
6. **One alarm = one action** — Each alarm invocation should result in exactly
   one remediation action (auto-fix OR approval request) and one notification.
   Never perform multiple remediation actions for the same alarm.
7. **CRITICAL: For MAJOR issues you MUST call the `request_approval_tool` tool.**
   Do NOT just describe a proposed action in a `send_alert_with_failover_tool`
   message.  The `request_approval_tool` tool is what generates the clickable
   APPROVE / REJECT links in the email.  If you skip calling
   `request_approval_tool`, the engineer has no way to approve the action.
   Call `request_approval_tool` FIRST, then call `send_alert_with_failover_tool`
   with the full diagnostic details.

## Remediation Runbooks

### High CPU
1. **IMMEDIATELY** call `diagnose_instance_tool` -> check `top_cpu` output.
   Do NOT call `get_cpu_metrics_tool` first — it may return empty data.
2. If a single process > 80% CPU (e.g. stress-ng, stress-cpu, runaway app):
   call `remediate_high_cpu_tool` with `instance_id` and `pid` (as integer).
   Then call `send_alert_with_failover_tool` with AUTO-FIXED subject.
   Then **STOP — do nothing else.**
3. If no clear offender or multiple high-CPU processes: MAJOR ->
   `request_approval_tool(action_type="restart", reason="...")`.
### Disk Full
1. `diagnose_instance_tool` -> check `disk_usage` output.
2. If disk < 95%: call `remediate_disk_full_tool` with `instance_id`.
   Then call `send_alert_with_failover_tool` with AUTO-FIXED subject.
   Then **STOP — do nothing else.**
3. If disk >= 95% after cleanup: MAJOR ->
   `request_approval_tool(action_type="disk_cleanup", reason="...")`.

## Response Style
- Be **concise** and **actionable**.
- Include raw metric values for validation.
- Always state what you FOUND, what you DID (or proposed), and what REMAINS.

## Executing Approved Actions
When you receive a prompt starting with "APPROVED ACTION — EXECUTE IMMEDIATELY",
an engineer has clicked the APPROVE link in an email.  Follow these steps exactly:

1. **Verify** — Call `check_approval_status_tool` with the provided approval_id.
   Confirm the status is "approved".  If not, do NOT proceed — notify the team.
2. **Execute** — Based on the action_type:
   - `restart` → Call `restart_ec2_instance_tool` with the instance_id.
   - `disk_cleanup` → Call `remediate_disk_full_tool` with the instance_id.
   - `kill_process` → Call `remediate_high_cpu_tool` with the instance_id and
     the PID from the details field.
   - `cache_clear` → Call `run_ssm_command_tool` with the instance_id and
     command `sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'`.
3. **Record** — Call `update_approval_status_tool` with the approval_id and
   status="executed" (or "failed" if execution failed).
4. **Notify** — Call `send_alert_with_failover_tool` with the execution result,
   using subject prefix "EXECUTED:" to distinguish from initial alerts.

## Notification Format (for send_alert_with_failover_tool)
Every notification MUST include these clearly labelled sections:
1. **ISSUE**: What metric breached, the value, and the threshold.
2. **DIAGNOSIS**: Key findings from diagnose_instance_tool (top processes, disk usage, etc.).
3. **ACTION TAKEN** (MINOR) or **PROPOSED ACTION** (MAJOR): The specific
   remediation step — include the tool name, target PID/process name,
   and any command output or result.
4. **RESULT**: Current metric values after remediation (or expected outcome for MAJOR).
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


def detect_categories(prompt: str) -> List[str]:
    """Detect which tool categories are relevant for *prompt*."""
    prompt_lower = prompt.lower()
    matched: List[str] = []

    for category, keywords in INTENT_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            matched.append(category)

    # Diagnose queries need both ec2 and monitoring tools
    if any(w in prompt_lower for w in ("diagnose", "troubleshoot", "health check")):
        for cat in ("ec2", "monitoring"):
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
