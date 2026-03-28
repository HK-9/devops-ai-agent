"""
Agent Runner Stack — Lambda function for DevOps Agent invocation.

Deploys the Lambda that receives EventBridge events and calls
the DevOps AI Agent.  Also includes:
  - DynamoDB table for pending approval requests
  - API Gateway (HTTP) with approve / reject callback endpoints
  - Approval Handler Lambda
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigw_int
from aws_cdk import aws_dynamodb as ddb
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
            "pip", "install",
            "-r", str(req_file),
            "-t", str(bundle),
            "--quiet",
            "--disable-pip-version-check",
            "--platform", "manylinux2014_x86_64",
            "--only-binary=:all:",
            "--implementation", "cp",
            "--python-version", "3.12",
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
            self.alert_topic.add_subscription(
                subs.EmailSubscription(alert_email)
            )
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

        # SSM RunCommand — remote diagnostics & remediation
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ssm:SendCommand",
                    "ssm:GetCommandInvocation",
                    "ssm:ListCommandInvocations",
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

        # ── DynamoDB — pending approval requests ─────────────────────
        self.approvals_table = ddb.Table(
            self,
            "ApprovalsTable",
            table_name="devops-agent-approvals",
            partition_key=ddb.Attribute(name="approval_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",
        )
        # Let the agent Lambda read/write approvals
        self.approvals_table.grant_read_write_data(self.agent_fn)

        # Pass the table name to the agent Lambda
        self.agent_fn.add_environment("APPROVALS_TABLE", self.approvals_table.table_name)

        # ── Approval Handler Lambda ──────────────────────────────────
        # Handles GET /approve/{id} and GET /reject/{id} from emails.
        self.approval_fn = _lambda.Function(
            self,
            "ApprovalHandler",
            function_name="devops-agent-approval-handler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=cdk.Duration.minutes(3),
            memory_size=256,
            log_group=logs.LogGroup(
                self, "ApprovalLogGroup",
                log_group_name="/aws/lambda/devops-agent-approval-handler",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
            environment={
                "APPROVALS_TABLE": self.approvals_table.table_name,
                "SNS_TOPIC_ARN": self.alert_topic.topic_arn,
                "CODE_VERSION": "5",  # Bump this to force Lambda code update
            },
            code=_lambda.InlineCode(_APPROVAL_HANDLER_CODE),
        )

        self.approvals_table.grant_read_write_data(self.approval_fn)
        self.alert_topic.grant_publish(self.approval_fn)

        # Approval handler needs EC2 + SSM to execute the approved action
        self.approval_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:StopInstances",
                    "ec2:StartInstances",
                    "ec2:RebootInstances",
                    "ec2:ModifyInstanceAttribute",
                    "ssm:SendCommand",
                    "ssm:GetCommandInvocation",
                ],
                resources=["*"],
            )
        )

        # Approval handler also needs to delete its own reminder schedule
        self.approval_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["scheduler:DeleteSchedule"],
                resources=["arn:aws:scheduler:*:*:schedule/default/devops-approval-reminder-*"],
            )
        )

        # ── IAM Role for EventBridge Scheduler → Approval Handler ────
        # Scheduler needs a role to assume when invoking the Lambda.
        self.scheduler_role = iam.Role(
            self,
            "SchedulerInvokeRole",
            role_name="devops-agent-scheduler-invoke-role",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            inline_policies={
                "InvokeApprovalHandler": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            effect=iam.Effect.ALLOW,
                            actions=["lambda:InvokeFunction"],
                            resources=[self.approval_fn.function_arn],
                        )
                    ]
                )
            },
        )

        # Agent Lambda needs to create one-time schedules for reminders
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["scheduler:CreateSchedule", "scheduler:DeleteSchedule"],
                resources=["arn:aws:scheduler:*:*:schedule/default/devops-approval-reminder-*"],
            )
        )
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.scheduler_role.role_arn],
            )
        )

        # Pass scheduler config to agent Lambda so request_approval can create schedules
        self.agent_fn.add_environment("SCHEDULER_ROLE_ARN", self.scheduler_role.role_arn)
        self.agent_fn.add_environment("APPROVAL_HANDLER_ARN", self.approval_fn.function_arn)

        # ── API Gateway (HTTP) — approval callbacks ──────────────────
        self.api = apigwv2.HttpApi(
            self,
            "ApprovalApi",
            api_name="devops-agent-approvals",
            description="Approve / reject remediation actions from email links",
        )

        approval_integration = apigw_int.HttpLambdaIntegration(
            "ApprovalIntegration",
            handler=self.approval_fn,
        )

        self.api.add_routes(
            path="/approve/{approval_id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=approval_integration,
        )
        self.api.add_routes(
            path="/reject/{approval_id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=approval_integration,
        )

        # Pass the API URL to the agent Lambda so it can build links
        self.agent_fn.add_environment("APPROVAL_API_URL", self.api.url or "")

        # ── EventBridge → Lambda wiring ──────────────────────────────
        if alarm_rule is not None:
            alarm_rule.add_target(targets.LambdaFunction(self.agent_fn))

        # ── Outputs ──────────────────────────────────────────────────
        cdk.CfnOutput(self, "FunctionArn", value=self.agent_fn.function_arn)
        cdk.CfnOutput(self, "FunctionName", value=self.agent_fn.function_name)
        cdk.CfnOutput(self, "SnsTopicArn", value=self.alert_topic.topic_arn)
        cdk.CfnOutput(self, "ApprovalsTableName", value=self.approvals_table.table_name)
        cdk.CfnOutput(
            self, "ApprovalApiUrl",
            value=self.api.url or "",
            description="Base URL for approve/reject callbacks",
        )


# ── Approval Handler inline Lambda code ──────────────────────────────────

_APPROVAL_HANDLER_CODE = """\
# Approval Handler Lambda - v3 (with reminder support)
import boto3
import json
import os
import time

ddb       = boto3.resource("dynamodb")
table     = ddb.Table(os.environ["APPROVALS_TABLE"])
sns       = boto3.client("sns")
ec2       = boto3.client("ec2")
ssm       = boto3.client("ssm")
scheduler = boto3.client("scheduler")

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")


def handler(event, context):
    print(json.dumps(event))

    # ---- Reminder path (invoked by EventBridge Scheduler) ----
    if event.get("source") == "devops-agent-reminder":
        return handle_reminder(event)

    # ---- Normal approve/reject path (invoked by API Gateway) ----
    path = event.get("rawPath", "")
    path_params = event.get("pathParameters", {}) or {}
    approval_id = path_params.get("approval_id", "")

    if not approval_id:
        return _response(400, "Missing approval_id")

    # Determine action from path
    if "/approve/" in path:
        decision = "approved"
    elif "/reject/" in path:
        decision = "rejected"
    else:
        return _response(400, "Invalid path")

    # Fetch the pending approval
    resp = table.get_item(Key={"approval_id": approval_id})
    item = resp.get("Item")

    if not item:
        return _html(404, "Approval not found", "This approval request does not exist or has expired.")

    if item.get("status") != "pending":
        msg = "This request was already <b>" + str(item.get('status', '')) + "</b> at " + str(item.get('decided_at', 'unknown')) + "."
        return _html(200, "Already processed", msg)

    # Update status
    table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET #s = :s, decided_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": decision,
            ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )

    instance_id = item.get("instance_id", "")
    action_type = item.get("action_type", "")
    details     = item.get("details", "")

    # Clean up the reminder schedule since the approval has been actioned
    cleanup_reminder_schedule(approval_id)

    if decision == "approved":
        result = execute_action(instance_id, action_type, details)
        notify_subject = "APPROVED: " + action_type + " on " + instance_id
        notify_msg = "Action: " + action_type + "\\nInstance: " + instance_id + "\\n\\nResult: " + result
        notify(notify_subject, notify_msg)
        body = "<b>" + action_type + "</b> on <code>" + instance_id + " has been approved and executed. Result: " + result
        return _html(200, "Approved", body)
    else:
        notify_subject = "REJECTED: " + action_type + " on " + instance_id
        notify_msg = "An engineer rejected the proposed " + action_type + " on " + instance_id + "."
        notify(notify_subject, notify_msg)
        body = "<b>" + action_type + "</b> on <code>" + instance_id + "</code> has been rejected. No action taken."
        return _html(200, "Rejected", body)


def execute_action(instance_id, action_type, details):
    try:
        if action_type == "restart":
            ec2.reboot_instances(InstanceIds=[instance_id])
            return "Instance " + instance_id + " reboot initiated."

        elif action_type == "disk_cleanup":
            cmd = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [
                    "sudo find /var/log -name '*.gz' -mtime +7 -delete 2>/dev/null",
                    "sudo find /tmp -type f -mtime +2 -delete 2>/dev/null",
                    "sudo apt-get clean 2>/dev/null || sudo yum clean all 2>/dev/null",
                    "sudo journalctl --vacuum-time=3d 2>/dev/null",
                    "df -h /",
                ]},
                TimeoutSeconds=60,
                Comment="Approved disk cleanup for " + instance_id,
            )
            return "Disk cleanup command sent (CommandId: " + cmd['Command']['CommandId'] + ")"

        elif action_type == "kill_process":
            pid = details
            cmd = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": ["sudo kill -9 " + pid]},
                TimeoutSeconds=15,
                Comment="Approved kill PID " + pid + " on " + instance_id,
            )
            return "Kill PID " + pid + " command sent (CommandId: " + cmd['Command']['CommandId'] + ")"

        elif action_type == "cache_clear":
            cmd = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": ["sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'"]},
                TimeoutSeconds=15,
                Comment="Approved cache clear on " + instance_id,
            )
            return "Cache clear command sent (CommandId: " + cmd['Command']['CommandId'] + ")"

        else:
            return "Unknown action type: " + action_type

    except Exception as e:
        return "Error executing action: " + str(e)


def notify(subject, message):
    if SNS_TOPIC_ARN:
        try:
            sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
        except Exception as e:
            print("Failed to send notification: " + str(e))


def _html(code, title, body):
    if "Approved" in title:
        color = "#16a34a"
    elif "Rejected" in title:
        color = "#dc2626"
    else:
        color = "#333"
    html = (
        "<!DOCTYPE html>"
        "<html><head><title>" + title + "</title>"
        "<style>body{font-family:sans-serif;max-width:600px;margin:60px auto;padding:20px;}"
        "h1{color:" + color + ";}"
        "code{background:#f1f5f9;padding:2px 6px;border-radius:4px;}</style>"
        "</head><body><h1>" + title + "</h1><p>" + body + "</p>"
        '<p style="color:#666;margin-top:40px;">— DevOps AI Agent</p></body></html>'
    )
    return {
        "statusCode": code,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }


def handle_reminder(event):
    approval_id = event.get("approval_id", "")
    if not approval_id:
        print("Reminder event missing approval_id")
        return {"statusCode": 400, "body": "Missing approval_id"}

    # Check if still pending
    resp = table.get_item(Key={"approval_id": approval_id})
    item = resp.get("Item")
    if not item or item.get("status") != "pending":
        print("Approval " + approval_id + " is no longer pending, skipping reminder")
        cleanup_reminder_schedule(approval_id)
        return {"statusCode": 200, "body": "No reminder needed"}

    instance_id = item.get("instance_id", "")
    action_type = item.get("action_type", "")
    reason      = item.get("reason", "")
    created_at  = item.get("created_at", "")
    approve_url = event.get("approve_url", "")
    reject_url  = event.get("reject_url", "")

    subject = "REMINDER: " + action_type.upper() + " on " + instance_id + " still awaiting approval"
    message = (
        "This is a reminder that the following action is still waiting for your approval:\n\n"
        "  Instance:  " + instance_id + "\n"
        "  Action:    " + action_type + "\n"
        "  Reason:    " + reason + "\n"
        "  Requested: " + created_at + "\n\n"
        "This request has been pending for over 20 minutes.\n\n"
        "Click to APPROVE:\n" + approve_url + "\n\n"
        "Click to REJECT:\n" + reject_url + "\n\n"
        "This link expires 24 hours after the original request.\n\n"
        "\u2014 DevOps AI Agent"
    )

    notify(subject, message)
    print("Reminder sent for approval " + approval_id)

    # Mark reminder as sent in DynamoDB
    table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET reminder_sent = :true, reminder_sent_at = :t",
        ExpressionAttributeValues={
            ":true": True,
            ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )

    # Clean up the one-time schedule
    cleanup_reminder_schedule(approval_id)
    return {"statusCode": 200, "body": "Reminder sent"}


def cleanup_reminder_schedule(approval_id):
    schedule_name = "devops-approval-reminder-" + approval_id[:8]
    try:
        scheduler.delete_schedule(Name=schedule_name)
        print("Deleted reminder schedule: " + schedule_name)
    except Exception as e:
        # Schedule may not exist or already deleted
        print("Could not delete schedule " + schedule_name + ": " + str(e))


def _response(code, msg):
    return {"statusCode": code, "body": json.dumps({"message": msg})}
"""
