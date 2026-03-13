"""
CDK App entry point.

Instantiates all CloudFormation stacks for the DevOps AI Agent
infrastructure.
"""

from __future__ import annotations

import os
from pathlib import Path

import aws_cdk as cdk
from infra.stacks.agent_runner_stack import AgentRunnerStack
from infra.stacks.monitoring_stack import MonitoringStack
from infra.stacks.networking_stack import NetworkingStack

# ── Load .env file (if present) ──────────────────────────────────────────
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or os.environ.get("AWS_REGION", "us-east-1"),
)

# ── Stacks ───────────────────────────────────────────────────────────
networking = NetworkingStack(app, "DevOpsAgent-Networking", env=env)

monitoring = MonitoringStack(
    app,
    "DevOpsAgent-Monitoring",
    env=env,
    description="CloudWatch alarms and EventBridge rules for the DevOps AI Agent",
)

agent_runner = AgentRunnerStack(
    app,
    "DevOpsAgent-Runner",
    env=env,
    description="Lambda function for DevOps AI Agent invocation",
    alarm_rule=monitoring.alarm_rule,
    alert_email=app.node.try_get_context("alert_email") or os.environ.get("ALERT_EMAIL", ""),
)

app.synth()
