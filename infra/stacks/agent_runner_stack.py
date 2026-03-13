"""
Agent Runner Stack — Lambda function for DevOps Agent invocation.

Deploys the Lambda that receives EventBridge events and calls
the DevOps AI Agent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

# ── Pre-build the Lambda bundle at synth time ────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BUNDLE_DIR = _PROJECT_ROOT / ".lambda-bundle"


def _build_lambda_bundle() -> str:
    """Install pip deps + copy src/ into a staging directory.

    Returns the absolute path to the bundle directory.
    """
    bundle = _BUNDLE_DIR
    req_file = _PROJECT_ROOT / "requirements-lambda.txt"
    src_dir = _PROJECT_ROOT / "src"

    # Clean previous bundle
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    # pip install dependencies (targeting Linux x86_64 for Lambda)
    subprocess.check_call(
        [
            "pip",
            "install",
            "-r",
            str(req_file),
            "-t",
            str(bundle),
            "--quiet",
            "--disable-pip-version-check",
            "--platform",
            "manylinux2014_x86_64",
            "--only-binary=:all:",
            "--implementation",
            "cp",
            "--python-version",
            "3.12",
        ],
    )

    # Copy src/ package
    shutil.copytree(
        str(src_dir),
        str(bundle / "src"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    return str(bundle)


class AgentRunnerStack(cdk.Stack):
    """Lambda function that hosts the DevOps Agent entry point."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alarm_rule: events.Rule | None = None,
        alert_email: str = "",
        **kwargs,
    ) -> None:  # type: ignore[no-untyped-def]
        super().__init__(scope, construct_id, **kwargs)

        # ── Build the Lambda bundle (pip deps + src/) ────────────────
        bundle_path = _build_lambda_bundle()

        # ── Log Group ─────────────────────────────────────────────────
        log_group = logs.LogGroup(
            self,
            "AgentLogGroup",
            log_group_name="/aws/lambda/devops-ai-agent-handler",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        # ── SNS Topic (failover alerting) ────────────────────────────
        self.alert_topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name="devops-agent-alerts",
            display_name="DevOps Agent Alerts",
        )

        if alert_email:
            self.alert_topic.add_subscription(subs.EmailSubscription(alert_email))
        # ── Lambda function ──────────────────────────────────────────
        self.agent_fn = _lambda.Function(
            self,
            "AgentHandler",
            function_name="devops-ai-agent-handler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="src.handlers.lambda_handler.handler",
            code=_lambda.Code.from_asset(bundle_path),
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            environment={
                "LOG_LEVEL": "INFO",
                "LOG_FORMAT": "json",
                # Note: AWS_REGION is set automatically by Lambda runtime
                # AGENT_ID and AGENT_ALIAS_ID left empty → uses invoke_inline_agent
                "BEDROCK_MODEL_ID": "amazon.nova-lite-v1:0",
                # "TEAMS_WEBHOOK_URL": "...", # Uncomment and add URL when ready
                "SNS_TOPIC_ARN": self.alert_topic.topic_arn,
            },
            log_group=log_group,
        )

        # ── IAM Permissions ──────────────────────────────────────────

        # EC2 read + restart
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:StopInstances",
                    "ec2:StartInstances",
                ],
                resources=["*"],  # Scope down in production
            )
        )

        # CloudWatch metrics read
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:ListMetrics",
                ],
                resources=["*"],
            )
        )

        # Bedrock AgentCore invoke
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:*"],
                resources=["*"],
            )
        )

        # SNS publish (for alert failover)
        self.alert_topic.grant_publish(self.agent_fn)

        # ── EventBridge → Lambda wiring ──────────────────────────────
        if alarm_rule is not None:
            alarm_rule.add_target(targets.LambdaFunction(self.agent_fn))

        # ── Outputs ──────────────────────────────────────────────────
        cdk.CfnOutput(self, "FunctionArn", value=self.agent_fn.function_arn)
        cdk.CfnOutput(self, "FunctionName", value=self.agent_fn.function_name)
        cdk.CfnOutput(self, "SnsTopicArn", value=self.alert_topic.topic_arn)
