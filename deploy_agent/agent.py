"""
DevOps AI Agent — Strands Agent with MCP Gateway tools.

Connects to the AgentCore Gateway via streamable-http to access all
MCP server tools (AWS Infra, Monitoring, SNS, Teams).  Uses Bedrock
Nova Lite as the foundation model with Strands' native reasoning loop.
"""

from __future__ import annotations

import logging
import os

from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("devops-agent")

# ── Configuration ────────────────────────────────────────────────────────

GATEWAY_URL = os.environ.get(
    "GATEWAY_URL",
    "https://devopsagentgatewayv2-hvvsllrsvw.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp",
)
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-lite-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "15"))

# ── System prompt ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are **DevOps Agent**, an expert AWS infrastructure assistant.

## Role
You monitor, diagnose, and remediate issues across an AWS fleet of EC2
instances.  You communicate findings and actions to the engineering team
via Microsoft Teams.

## Capabilities (MCP Tools)
You have access to the following tool groups — always prefer the most
specific tool for the task:

### AWS Infrastructure
- `list_ec2_instances_tool`  — List running/stopped EC2 instances with filters.
- `describe_ec2_instance_tool` — Get detailed info (state, type, IPs, tags) for one instance.
- `restart_ec2_instance_tool` — Restart (stop + start) an instance.  **Always
  confirm with the user first** unless the alarm is CRITICAL.

### Monitoring (CloudWatch)
- `get_cpu_metrics_tool`  — CPU utilization for a single instance over a period.
- `get_cpu_metrics_for_instances_tool` — Batch CPU metrics for multiple instances.
- `get_memory_metrics_tool`  — Memory utilization  *(requires CloudWatch Agent)*.
- `get_disk_usage_tool`  — Disk usage metrics  *(requires CloudWatch Agent)*.

### Alerting (with failover)
- `send_alert_with_failover_tool` — **ALWAYS use this for all notifications.**
  Attempts to send via Teams first; if Teams is unavailable, automatically
  fails over to AWS SNS (email).  This guarantees delivery.

### Teams Notifications (only when explicitly asked)
- `send_teams_message_tool` — Plain text to Teams.
- `create_incident_notification_tool` — Structured Teams card.

## Behavioural Guardrails
1. **Read before write** — Always gather metrics and instance state before
   taking any remediation action (restart, terminate, etc.).
2. **Least privilege** — Never attempt actions outside your tool set.
3. **Structured reporting** — When reporting to Teams, always include:
   instance ID, metric values, timestamps, and any action taken.
4. **Escalation** — If CPU > 90% for > 15 minutes, escalate by creating
   an incident notification with severity=CRITICAL.
5. **Memory escalation** — If memory > 90% for > 10 minutes, escalate
   with severity=CRITICAL.
6. **Disk escalation** — If disk usage > 95%, escalate with severity=CRITICAL.
7. **Safety** — Never restart more than 3 instances in a single reasoning
   turn.  Ask for human approval if the batch is larger.

## Remediation Guidance
Every alert notification you send MUST include a **"Recommended Actions"**
section with concrete, numbered remediation steps.

## Response Style
- Be **concise** and **actionable**.
- Use bullet points for multi-item answers.
- Include raw metric values so the team can validate.

For any question about current AWS state, always call the relevant tool first.
Never answer from conversation memory alone.
"""


def create_gateway_mcp_client() -> MCPClient:
    """Create an MCP client connected to the AgentCore Gateway.

    The gateway uses NONE auth. MCP server runtimes behind the gateway
    are protected by Cognito JWT — the gateway handles that authentication
    internally via the registered OAuth credential provider.
    """
    logger.info("Connecting to AgentCore Gateway: %s", GATEWAY_URL)
    return MCPClient(
        lambda: streamablehttp_client(url=GATEWAY_URL)
    )


def create_agent(mcp_client: MCPClient) -> Agent:
    """Build the Strands Agent with Bedrock model and MCP tools."""
    model = BedrockModel(
        region_name=AWS_REGION,
        model_id=MODEL_ID,
        max_tokens=4096,
        streaming=True,
    )

    agent = Agent(
        model=model,
        tools=[mcp_client],
        system_prompt=SYSTEM_PROMPT,
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
    agent = create_agent(mcp_client)
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

                response_body = json.dumps({
                    "response": str(result),
                    "stop_reason": getattr(result, "stop_reason", "end_turn"),
                })

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response_body.encode())

            except Exception as exc:
                logger.error("Agent error: %s", exc, exc_info=True)
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
