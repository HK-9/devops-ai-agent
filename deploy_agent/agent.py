"""
DevOps AI Agent — Strands Agent with MCP Gateway tools.

Connects to the AgentCore Gateway via streamable-http to access all
MCP server tools (AWS Infra, Monitoring, SNS, Teams).  Uses Bedrock
Nova Lite as the foundation model with Strands' native reasoning loop.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from strands import Agent
from strands.agent.agent import null_callback_handler
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

from sigv4_auth import BotoSigV4Auth

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
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-pro-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "15"))

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
1. **Diagnose** — Call `diagnose_instance_tool` to get real-time system state.
2. **Correlate** — Call the relevant `get_*_metrics` tool (CPU/mem/disk).
3. **Describe** — Call `describe_ec2_instance_tool` for instance metadata.
4. **Decide** — Classify as MINOR or MAJOR using the tables above.
5. **Act:**
   - MINOR -> auto-fix using the remediation tool, then notify.
   - MAJOR -> `request_approval_tool` (sends email with links), then notify.
6. **Report** — Always call `send_alert_with_failover_tool` with full details.

## Behavioural Guardrails
1. **Read before write** — Always diagnose before acting.
2. **Least privilege** — Never attempt actions outside your tool set.
3. **Safety limits** — Never kill more than 3 processes per alarm.
4. **Never restart directly** — Always use `request_approval_tool` for restarts.
5. **Structured reporting** — Always include: instance ID, metric values,
   timestamps, actions taken (or proposed), and result.
6. **CRITICAL: For MAJOR issues you MUST call the `request_approval_tool` tool.**
   Do NOT just describe a proposed action in a `send_alert_with_failover_tool`
   message.  The `request_approval_tool` tool is what generates the clickable
   APPROVE / REJECT links in the email.  If you skip calling
   `request_approval_tool`, the engineer has no way to approve the action.
   Call `request_approval_tool` FIRST, then call `send_alert_with_failover_tool`
   with the full diagnostic details.

## Remediation Runbooks

### High CPU
1. `diagnose_instance_tool` -> check `top_cpu` output for the offending PID.
2. If a single process > 80% CPU: `remediate_high_cpu_tool` (MINOR auto-fix).
3. If no clear offender or multiple processes: MAJOR ->
   `request_approval_tool(action_type="restart", reason="...")`.
4. Always call `send_alert_with_failover_tool` with the full diagnosis.

### High Memory
1. `diagnose_instance_tool` -> check `top_memory` output.
2. If a single process > 50% memory: `remediate_high_memory_tool` (MINOR).
3. If fragmented across many processes: MAJOR ->
   `request_approval_tool(action_type="restart", reason="...")`.
4. Try cache clear as interim: `run_ssm_command_tool` with
   `sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'`

### Disk Full
1. `diagnose_instance_tool` -> check `disk_usage` output.
2. If disk < 95%: `remediate_disk_full_tool` (MINOR auto-fix).
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
    return MCPClient(
        lambda: streamablehttp_client(url=GATEWAY_URL, auth=sigv4)
    )


# ── Nova-safe Bedrock model ─────────────────────────────────────────

class NovaBedrockModel(BedrockModel):
    """BedrockModel subclass that normalizes Nova Lite streaming chunks.

    Amazon Nova Lite sends tool-use input as a pre-parsed dict in
    ConverseStream deltas, but the Strands SDK event loop expects
    string chunks it can concatenate.  This subclass intercepts the
    raw stream and converts any dict tool inputs to JSON strings
    before they reach the SDK's handle_content_block_delta().
    """

    @staticmethod
    def _normalize_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
        """Ensure toolUse input deltas are always JSON strings."""
        cbd = chunk.get("contentBlockDelta")
        if cbd is not None:
            tool_use = cbd.get("delta", {}).get("toolUse")
            if tool_use is not None and isinstance(tool_use.get("input"), dict):
                tool_use["input"] = json.dumps(tool_use["input"])
        return chunk

    def _stream(self, callback, messages, tool_specs=None, system_prompt_content=None, tool_choice=None):
        """Override to normalize streaming chunks before dispatch."""
        original_callback = callback

        def normalizing_callback(event=None):
            if event is not None:
                event = self._normalize_chunk(event)
            original_callback(event)

        super()._stream(normalizing_callback, messages, tool_specs, system_prompt_content, tool_choice)


def create_agent(mcp_client: MCPClient, http_mode: bool = False, streaming: bool = True) -> Agent:
    """Build the Strands Agent with Bedrock model and MCP tools."""
    # Use NovaBedrockModel for Nova models (chunk normalization),
    # standard BedrockModel for Claude/others.
    model_cls = NovaBedrockModel if "nova" in MODEL_ID.lower() else BedrockModel
    model = model_cls(
        region_name=AWS_REGION,
        model_id=MODEL_ID,
        max_tokens=4096,
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
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

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

                response_body = json.dumps({
                    "response": result_text,
                    "stop_reason": getattr(result, "stop_reason", "end_turn"),
                })

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
 