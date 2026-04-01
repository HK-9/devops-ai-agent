#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║              DevOps AI Agent — Interactive Demo & Helper            ║
╚══════════════════════════════════════════════════════════════════════╝

This script demonstrates every module in the project with runnable
examples.  It is designed to work **without** real AWS credentials
or a Teams webhook — all calls are simulated using canned data.

Run it:
    python demo.py              # full walkthrough
    python demo.py --section 3  # jump to a specific section
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import textwrap
from typing import Any

# ── Colour helpers (works on Windows Terminal / modern terminals) ─────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
RESET = "\033[0m"


def banner(title: str) -> None:
    width = 68
    print(f"\n{CYAN}{'═' * width}")
    print(f"  {BOLD}{title}{RESET}{CYAN}")
    print(f"{'═' * width}{RESET}\n")


def section(num: int, title: str) -> None:
    print(f"\n{MAGENTA}{'─' * 60}")
    print(f"  {BOLD}Section {num}: {title}{RESET}")
    print(f"{MAGENTA}{'─' * 60}{RESET}\n")


def code(label: str, obj: Any) -> None:
    print(f"  {DIM}{label}:{RESET}")
    if isinstance(obj, (dict, list)):
        formatted = json.dumps(obj, indent=4, default=str)
        for line in formatted.split("\n"):
            print(f"    {GREEN}{line}{RESET}")
    else:
        for line in str(obj).split("\n"):
            print(f"    {GREEN}{line}{RESET}")
    print()


def info(msg: str) -> None:
    print(f"  {YELLOW}→{RESET} {msg}")


def success(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def error(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


# ════════════════════════════════════════════════════════════════════════
# SECTION 1: Configuration
# ════════════════════════════════════════════════════════════════════════


def demo_configuration() -> None:
    section(1, "Configuration (src/agent/config.py)")
    info("The Settings class uses pydantic-settings to load env vars.\n")

    from src.agent.config import Settings

    # Show default values (no .env file needed)
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        aws_region="us-east-1",
        teams_webhook_url="https://outlook.office.com/webhook/demo",
    )

    code("Loaded settings", {
        "aws_region": s.aws_region,
        "bedrock_model_id": s.bedrock_model_id,
        "mcp_transport": s.mcp_transport,
        "teams_webhook_url": s.teams_webhook_url,
        "tool_timeout_seconds": s.tool_timeout_seconds,
        "log_level": s.log_level,
    })

    info("Access the singleton anywhere:  from src.agent.config import settings")
    success("Configuration loaded successfully\n")


# ════════════════════════════════════════════════════════════════════════
# SECTION 2: System Prompt & Prompt Builders
# ════════════════════════════════════════════════════════════════════════


def demo_system_prompt() -> None:
    section(2, "System Prompt (src/agent/system_prompt.py)")

    from src.agent.system_prompt import SYSTEM_PROMPT, build_adhoc_prompt, build_alarm_prompt

    info(f"System prompt length: {len(SYSTEM_PROMPT)} characters")
    info("First 300 chars:\n")
    print(f"    {DIM}{SYSTEM_PROMPT[:300]}…{RESET}\n")

    info("Building an alarm prompt:")
    alarm_prompt = build_alarm_prompt(
        instance_id="i-0abc123def456789a",
        alarm_name="devops-agent-high-cpu",
        reason="CPU exceeded 80% for 10 minutes",
    )
    code("Alarm prompt", alarm_prompt)

    info("Building an ad-hoc prompt:")
    adhoc_prompt = build_adhoc_prompt("What's the CPU usage on our production servers?")
    code("Ad-hoc prompt", adhoc_prompt)

    success("Prompt builders work correctly\n")


# ════════════════════════════════════════════════════════════════════════
# SECTION 3: Event Parser (EventBridge → Agent Prompt)
# ════════════════════════════════════════════════════════════════════════


def demo_event_parser() -> None:
    section(3, "Event Parser (src/handlers/event_parser.py)")

    from src.handlers.event_parser import build_agent_prompt_from_alarm, parse_eventbridge_alarm

    # Simulate a realistic EventBridge event
    event = {
        "version": "0",
        "id": "12345678-abcd-efgh-ijkl-123456789012",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": "123456789012",
        "time": "2026-03-07T10:00:00Z",
        "region": "us-east-1",
        "resources": ["arn:aws:cloudwatch:us-east-1:123456789012:alarm:devops-agent-high-cpu"],
        "detail": {
            "alarmName": "devops-agent-high-cpu",
            "alarmDescription": "CPU > 80% on production instance",
            "state": {
                "value": "ALARM",
                "reason": "Threshold Crossed: 1 out of 2 datapoints [92.35] was greater than the threshold (80.0).",
                "timestamp": "2026-03-07T10:00:00.000+0000",
            },
            "previousState": {
                "value": "OK",
                "reason": "All datapoints within threshold.",
                "timestamp": "2026-03-07T09:45:00.000+0000",
            },
            "configuration": {
                "threshold": 80.0,
                "comparisonOperator": "GreaterThanThreshold",
                "evaluationPeriods": 2,
                "metrics": [{
                    "id": "cpu",
                    "metricStat": {
                        "metric": {
                            "namespace": "AWS/EC2",
                            "name": "CPUUtilization",
                            "dimensions": {"InstanceId": "i-0abc123def456789a"},
                        },
                        "period": 300,
                        "stat": "Average",
                    },
                }],
            },
        },
    }

    info("Parsing an EventBridge CloudWatch alarm event…")
    alarm = parse_eventbridge_alarm(event)

    code("Parsed Alarm", {
        "alarm_name": alarm.alarm_name,
        "state": f"{alarm.previous_state} → {alarm.state}",
        "instance_id": alarm.instance_id,
        "metric": f"{alarm.namespace}/{alarm.metric_name}",
        "threshold": f"{alarm.comparison_operator} {alarm.threshold}",
        "reason": alarm.reason[:80] + "…",
    })

    info("Generating agent prompt from alarm event:")
    prompt = build_agent_prompt_from_alarm(alarm)
    code("Agent Prompt", prompt)

    success("Event parser works correctly\n")


# ════════════════════════════════════════════════════════════════════════
# SECTION 4: MCP Tool Definitions
# ════════════════════════════════════════════════════════════════════════


def demo_mcp_tools() -> None:
    section(4, "MCP Tool Definitions (src/mcp_servers/*/server.py)")

    info("Each MCP server registers tools with JSON schemas.")
    info("Here are all available tools across the three servers:\n")

    # Import the tool manifests
    from src.mcp_servers.aws_infra.server import TOOLS as aws_tools
    from src.mcp_servers.monitoring.server import TOOLS as mon_tools
    from src.mcp_servers.teams.server import TOOLS as teams_tools

    all_servers = [
        ("AWS Infra Server", aws_tools),
        ("Monitoring Server", mon_tools),
        ("Teams Server", teams_tools),
    ]

    for server_name, tools in all_servers:
        print(f"  {BOLD}{server_name}{RESET} ({len(tools)} tools)")
        for tool in tools:
            required = tool.inputSchema.get("required", [])
            params = list(tool.inputSchema.get("properties", {}).keys())
            print(f"    {GREEN}• {tool.name}{RESET}")
            print(f"      {DIM}{tool.description[:80]}{RESET}")
            print(f"      params: {params}  required: {required}")
        print()

    total = sum(len(t) for _, t in all_servers)
    success(f"Total: {total} tools across {len(all_servers)} MCP servers\n")


# ════════════════════════════════════════════════════════════════════════
# SECTION 5: Simulated MCP Tool Invocation
# ════════════════════════════════════════════════════════════════════════


def demo_mock_tool_calls() -> None:
    section(5, "Simulated Tool Calls (tests/mocks/mock_responses.py)")

    from tests.mocks.mock_responses import (
        MOCK_CPU_METRICS,
        MOCK_INCIDENT_NOTIFICATION,
        MOCK_INSTANCE_LIST,
        MOCK_INSTANCE_RUNNING,
        MOCK_RESTART_RESPONSE,
        MOCK_TEAMS_SUCCESS,
    )

    info("Simulating: list_ec2_instances(state_filter='running')")
    code("Result", MOCK_INSTANCE_LIST)

    info("Simulating: describe_ec2_instance(instance_id='i-0abc123def456789a')")
    code("Result", MOCK_INSTANCE_RUNNING)

    info("Simulating: get_cpu_metrics(instance_id='i-0abc123def456789a')")
    code("Result", MOCK_CPU_METRICS)

    info("Simulating: send_teams_message('Server CPU elevated')")
    code("Result", MOCK_TEAMS_SUCCESS)

    info("Simulating: create_incident_notification(severity='CRITICAL', …)")
    code("Result", MOCK_INCIDENT_NOTIFICATION)

    info("Simulating: restart_ec2_instance(instance_id='i-0abc123def456789a')")
    code("Result", MOCK_RESTART_RESPONSE)

    success("All mock tool calls demonstrated\n")


# ════════════════════════════════════════════════════════════════════════
# SECTION 6: Teams Webhook Helper
# ════════════════════════════════════════════════════════════════════════


def demo_teams_webhook() -> None:
    section(6, "Teams Webhook Helper (src/utils/teams_webhook.py)")

    from src.utils.teams_webhook import build_incident_card

    info("Building a Teams Adaptive Card for an incident notification:")

    card = build_incident_card(
        severity="CRITICAL",
        instance_id="i-0abc123def456789a",
        alarm_name="devops-agent-high-cpu",
        metric_value="CPU 92.3% (peak 97.8%)",
        summary="Production web server experiencing sustained CPU spike. "
                "Auto-scaling may be needed if the trend continues.",
        actions_taken="Retrieved metrics, instance described, investigating root cause.",
    )

    code("Adaptive Card body", card)

    info("This card body is passed to post_adaptive_card() for delivery.")
    info("The full webhook payload wraps this in an Adaptive Card attachment.\n")
    success("Incident card builder works correctly\n")


# ════════════════════════════════════════════════════════════════════════
# SECTION 7: Full Agent Flow (End-to-End Simulation)
# ════════════════════════════════════════════════════════════════════════


def demo_full_flow() -> None:
    section(7, "Full Agent Flow — End-to-End Simulation")

    info("Simulating the complete event → agent → action → notify pipeline:\n")

    from src.handlers.event_parser import build_agent_prompt_from_alarm, parse_eventbridge_alarm
    from src.utils.teams_webhook import build_incident_card
    from tests.mocks.mock_responses import MOCK_CPU_METRICS, MOCK_INSTANCE_RUNNING

    # Step 1: CloudWatch alarm fires → EventBridge
    print(f"  {BOLD}Step 1:{RESET} CloudWatch alarm fires →  EventBridge delivers event")
    event = {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "region": "us-east-1",
        "account": "123456789012",
        "time": "2026-03-07T10:00:00Z",
        "detail": {
            "alarmName": "devops-agent-high-cpu",
            "alarmDescription": "CPU > 80%",
            "state": {"value": "ALARM", "reason": "CPU at 92.35%", "timestamp": "2026-03-07T10:00:00Z"},
            "previousState": {"value": "OK", "reason": "Normal", "timestamp": "2026-03-07T09:45:00Z"},
            "configuration": {
                "threshold": 80.0,
                "comparisonOperator": "GreaterThanThreshold",
                "evaluationPeriods": 2,
                "metrics": [{"metricStat": {"metric": {"namespace": "AWS/EC2", "name": "CPUUtilization", "dimensions": {"InstanceId": "i-0abc123def456789a"}}, "period": 300}}],
            },
        },
    }
    alarm = parse_eventbridge_alarm(event)
    success(f"Alarm parsed: {alarm.alarm_name} → {alarm.state}")

    # Step 2: Lambda handler parses event → generates prompt
    print(f"\n  {BOLD}Step 2:{RESET} Lambda handler generates agent prompt")
    prompt = build_agent_prompt_from_alarm(alarm)
    success(f"Prompt generated ({len(prompt)} chars)")

    # Step 3: Agent reasons — calls MCP tools
    print(f"\n  {BOLD}Step 3:{RESET} Agent reasoning loop — calls MCP tools")
    tool_calls = [
        {"tool": "describe_ec2_instance", "args": {"instance_id": "i-0abc123def456789a"}, "result": MOCK_INSTANCE_RUNNING},
        {"tool": "get_cpu_metrics", "args": {"instance_id": "i-0abc123def456789a"}, "result": MOCK_CPU_METRICS},
    ]
    for tc in tool_calls:
        success(f"Called {tc['tool']}({json.dumps(tc['args'])})")
        info(f"  → state: {tc['result'].get('state', 'N/A')}, "
             f"peak CPU: {tc['result'].get('summary', {}).get('peak', 'N/A')}%")

    # Step 4: Agent creates incident notification
    print(f"\n  {BOLD}Step 4:{RESET} Agent sends incident notification to Teams")
    card = build_incident_card(
        severity="CRITICAL",
        instance_id="i-0abc123def456789a",
        alarm_name="devops-agent-high-cpu",
        metric_value=f"CPU avg {MOCK_CPU_METRICS['summary']['average']}%, peak {MOCK_CPU_METRICS['summary']['peak']}%",
        summary="Production web server CPU spiked above 80%. Agent investigated and found sustained high utilisation.",
        actions_taken="Retrieved metrics and instance details. Notified team. Monitoring for resolution.",
    )
    success(f"Incident card created ({len(card)} body elements)")

    # Step 5: Result
    print(f"\n  {BOLD}Step 5:{RESET} Agent returns final response to Lambda")
    final_response = {
        "status": "completed",
        "tool_calls": len(tool_calls),
        "notification_sent": True,
        "severity": "CRITICAL",
        "recommendation": "Monitor for 15 more minutes. If CPU remains >95%, auto-restart will be triggered.",
    }
    code("Agent Response", final_response)

    success("End-to-end flow simulation complete! 🎉\n")


# ════════════════════════════════════════════════════════════════════════
# SECTION 8: Project Structure Overview
# ════════════════════════════════════════════════════════════════════════


def demo_project_structure() -> None:
    section(8, "Project Structure Overview")

    tree = textwrap.dedent("""\
        devops-ai-agent/
        ├── infra/                            # CDK Infrastructure-as-Code
        │   ├── app.py                        # CDK app entry point
        │   └── stacks/
        │       ├── networking_stack.py        # VPC, subnets, security groups
        │       ├── monitoring_stack.py        # CloudWatch alarms, EventBridge rules
        │       └── agent_runner_stack.py      # Lambda for agent invocation
        │
        ├── src/
        │   ├── agent/                        # AgentCore integration
        │   │   ├── agent_core.py             # Bedrock AgentCore ↔ MCP bridge
        │   │   ├── system_prompt.py          # Agent persona & guardrails
        │   │   └── config.py                 # Pydantic env-based settings
        │   │
        │   ├── mcp_servers/                  # MCP tool servers (one per domain)
        │   │   ├── aws_infra/
        │   │   │   ├── server.py             # MCP server (stdio transport)
        │   │   │   └── tools.py              # list/describe/restart EC2
        │   │   ├── monitoring/
        │   │   │   ├── server.py             # MCP server
        │   │   │   └── tools.py              # CPU/memory/disk metrics
        │   │   └── teams/
        │   │       ├── server.py             # MCP server
        │   │       └── tools.py              # send_message / incident_notification
        │   │
        │   ├── mcp_client/
        │   │   └── client.py                 # Unified client: discover + call tools
        │   │
        │   ├── handlers/
        │   │   ├── lambda_handler.py         # EventBridge → Agent invocation
        │   │   └── event_parser.py           # Alarm event → typed dataclass
        │   │
        │   └── utils/
        │       ├── aws_helpers.py            # boto3 factory, retries, JSON logging
        │       └── teams_webhook.py          # HTTP POST for Teams webhooks
        │
        ├── tests/
        │   ├── conftest.py                   # Shared fixtures
        │   ├── unit/                         # Moto-mocked unit tests
        │   ├── integration/                  # MCP round-trip tests
        │   └── mocks/                        # Canned API responses
        │
        ├── demo.py                           # ← THIS FILE
        ├── pyproject.toml                    # Dependencies & tool config
        ├── Makefile                          # Dev lifecycle commands
        └── README.md                         # Project overview
    """)

    print(f"{GREEN}{tree}{RESET}")

    print(f"  {BOLD}Key Makefile Targets:{RESET}")
    targets = [
        ("make install", "Install all dependencies (dev + infra)"),
        ("make test", "Run all pytest tests"),
        ("make test-unit", "Run unit tests only"),
        ("make lint", "Run ruff linter"),
        ("make typecheck", "Run mypy type checker"),
        ("make run-mcp-aws", "Start AWS Infra MCP server locally"),
    ]
    for cmd, desc in targets:
        print(f"    {CYAN}{cmd:25s}{RESET} {desc}")

    print()
    success("Project structure overview complete\n")


# ════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════

SECTIONS = {
    1: ("Configuration", demo_configuration),
    2: ("System Prompt & Prompt Builders", demo_system_prompt),
    3: ("Event Parser", demo_event_parser),
    4: ("MCP Tool Definitions", demo_mcp_tools),
    5: ("Simulated Tool Calls", demo_mock_tool_calls),
    6: ("Teams Webhook Helper", demo_teams_webhook),
    7: ("Full Agent Flow (E2E)", demo_full_flow),
    8: ("Project Structure", demo_project_structure),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="DevOps AI Agent — Interactive Demo")
    parser.add_argument(
        "--section", "-s",
        type=int,
        choices=list(SECTIONS.keys()),
        help="Run a specific section only (1-8)",
    )
    args = parser.parse_args()

    banner("DevOps AI Agent — Interactive Demo & Helper")

    print(f"  {DIM}This demo walks through every module in the project.{RESET}")
    print(f"  {DIM}No AWS credentials or Teams webhook needed — all mocked.{RESET}\n")

    if args.section:
        title, fn = SECTIONS[args.section]
        fn()
    else:
        for num in sorted(SECTIONS.keys()):
            title, fn = SECTIONS[num]
            try:
                fn()
            except Exception as exc:
                error(f"Section {num} failed: {exc}")
                print(f"    {DIM}(This may be due to missing dependencies — run 'make install' first){RESET}\n")

    banner("Demo Complete!")
    print(f"  {BOLD}Next steps:{RESET}")
    print(f"    1. Run {CYAN}make install{RESET} to install all dependencies")
    print(f"    2. Run {CYAN}make test{RESET} to execute unit tests")
    print(f"    3. Run {CYAN}python demo.py{RESET} again with deps installed for full output")
    print(f"    4. Set {CYAN}TEAMS_WEBHOOK_URL{RESET} env var to test real Teams delivery")
    print(f"    5. Deploy to AWS with {CYAN}cdk deploy --all{RESET}")
    print()


if __name__ == "__main__":
    main()
