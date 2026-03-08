"""
CDK App entry point.

Instantiates all CloudFormation stacks for the DevOps AI Agent
infrastructure.
"""

from __future__ import annotations

import aws_cdk as cdk

from infra.stacks.agent_runner_stack import AgentRunnerStack
from infra.stacks.monitoring_stack import MonitoringStack
from infra.stacks.networking_stack import NetworkingStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or "us-east-1",
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
    alarm_rule=monitoring.alarm_rule,  # CDK auto-infers the dependency
)

app.synth()
