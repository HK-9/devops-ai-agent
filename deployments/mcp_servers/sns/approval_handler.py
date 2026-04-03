# Approval Handler Lambda — v5 (AgentCore-native)
"""
Handles GET /approve/{id} and GET /reject/{id} from email links.

This is a lightweight Lambda behind API Gateway.  It does NOT execute
remediation actions directly.  Instead, when an approval is granted it
invokes the AgentCore agent, which uses its full MCP tool suite to
execute the approved action intelligently.

Flow:
  1. Engineer clicks APPROVE/REJECT link in email
  2. This Lambda updates DynamoDB status
  3. If approved → invokes the AgentCore agent with full context
  4. Agent handles execution via MCP tools (restart, cleanup, etc.)
  5. Lambda returns immediate HTML confirmation to the engineer
"""
import boto3
import json
import os
import time
import uuid
from urllib import request as urllib_request
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ddb       = boto3.resource("dynamodb")
table     = ddb.Table(os.environ["APPROVALS_TABLE"])
sns       = boto3.client("sns")

AWS_REGION        = os.environ.get("AWS_REGION", "ap-southeast-2")
SNS_TOPIC_ARN     = os.environ.get("SNS_TOPIC_ARN", "")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")

# AgentCore direct invocation URL (no boto3 bedrock-agentcore client needed)
AGENTCORE_INVOKE_URL = (
    f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/"
    + urllib_request.quote(AGENT_RUNTIME_ARN, safe="")
    + "/invocations"
) if AGENT_RUNTIME_ARN else ""


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
        return _html(404, "Approval not found",
                     "This approval request does not exist or has expired.")

    if item.get("status") != "pending":
        msg = (
            f"This request was already <b>{item.get('status', '')}</b> "
            f"at {item.get('decided_at', 'unknown')}."
        )
        return _html(200, "Already processed", msg)

    # Update status in DynamoDB
    decided_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET #s = :s, decided_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": decision, ":t": decided_at},
    )

    instance_id = item.get("instance_id", "")
    action_type = item.get("action_type", "")
    reason      = item.get("reason", "")
    details     = item.get("details", "")

    # Clean up any pending reminder schedule
    cleanup_reminder_schedule(approval_id)

    if decision == "approved":
        # Invoke the AgentCore agent to execute the approved action
        agent_result = invoke_agent_for_execution(
            approval_id, instance_id, action_type, reason, details,
        )
        notify(
            f"APPROVED: {action_type} on {instance_id}",
            f"An engineer approved {action_type} on {instance_id}.\n"
            f"The DevOps Agent has been invoked to execute the action.\n"
            f"Reason: {reason}\nDetails: {details}",
        )
        body = (
            f"<b>{action_type}</b> on <code>{instance_id}</code> has been "
            f"<b>approved</b>. The DevOps Agent has been invoked to execute "
            f"the action. You will receive a notification when complete."
        )
        return _html(200, "Approved", body)
    else:
        notify(
            f"REJECTED: {action_type} on {instance_id}",
            f"An engineer rejected the proposed {action_type} on {instance_id}.\n"
            f"No action will be taken.",
        )
        body = (
            f"<b>{action_type}</b> on <code>{instance_id}</code> has been "
            f"<b>rejected</b>. No action taken."
        )
        return _html(200, "Rejected", body)


# ── Agent Invocation ─────────────────────────────────────────────────────

def invoke_agent_for_execution(approval_id, instance_id, action_type, reason, details):
    """Invoke the AgentCore agent via direct HTTP URL to execute an approved action.

    Uses the AgentCore invocation URL with SigV4 auth — no boto3
    bedrock-agentcore client needed (works in any Lambda runtime).
    """
    if not AGENTCORE_INVOKE_URL:
        print("AGENT_RUNTIME_ARN not set — cannot invoke agent")
        return False

    prompt = (
        f"APPROVED ACTION — EXECUTE IMMEDIATELY\n\n"
        f"An engineer has approved the following remediation action. "
        f"Execute it now using the appropriate tool.\n\n"
        f"  Approval ID: {approval_id}\n"
        f"  Instance:    {instance_id}\n"
        f"  Action:      {action_type}\n"
        f"  Reason:      {reason}\n"
        f"  Details:     {details}\n\n"
        f"Instructions:\n"
        f"1. First call check_approval_status_tool with approval_id='{approval_id}' "
        f"to verify the approval is valid and status is 'approved'.\n"
        f"2. Then execute the action:\n"
        f"   - If action is 'restart': call restart_ec2_instance_tool with instance_id='{instance_id}'\n"
        f"   - If action is 'disk_cleanup': call remediate_disk_full_tool with instance_id='{instance_id}'\n"
        f"   - If action is 'kill_process': call remediate_high_cpu_tool with instance_id='{instance_id}' and pid='{details}'\n"
        f"   - If action is 'cache_clear': call run_ssm_command_tool with instance_id='{instance_id}' "
        f"and command=\"sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'\"\n"
        f"3. After execution, call send_alert_with_failover_tool to notify the team of the result.\n"
        f"4. Finally, call update_approval_status_tool with approval_id='{approval_id}' "
        f"and status='executed' to mark the approval as completed.\n"
    )

    try:
        session_id = f"{uuid.uuid4()}-{uuid.uuid4()}"
        payload = json.dumps({
            "prompt": prompt,
            "runtimeSessionId": session_id,
        }).encode()

        # Sign the request with SigV4
        session = boto3.Session()
        credentials = session.get_credentials().get_frozen_credentials()
        aws_request = AWSRequest(
            method="POST",
            url=AGENTCORE_INVOKE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(credentials, "bedrock-agentcore", AWS_REGION).add_auth(aws_request)

        # Send HTTP request
        req = urllib_request.Request(
            AGENTCORE_INVOKE_URL,
            data=payload,
            headers=dict(aws_request.headers),
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        print(f"Agent invoked via AgentCore URL for approval {approval_id}, session={session_id}")
        print(f"Agent response (truncated): {body[:500]}")
        return True
    except Exception as exc:
        print(f"Failed to invoke agent via URL: {exc}")
        return False


# ── Reminder handling ────────────────────────────────────────────────────

def handle_reminder(event):
    """Handle a reminder event from EventBridge Scheduler."""
    approval_id = event.get("approval_id", "")
    if not approval_id:
        print("Reminder event missing approval_id")
        return {"statusCode": 400, "body": "Missing approval_id"}

    resp = table.get_item(Key={"approval_id": approval_id})
    item = resp.get("Item")
    if not item or item.get("status") != "pending":
        print(f"Approval {approval_id} is no longer pending, skipping reminder")
        cleanup_reminder_schedule(approval_id)
        return {"statusCode": 200, "body": "No reminder needed"}

    instance_id = item.get("instance_id", "")
    action_type = item.get("action_type", "")
    reason      = item.get("reason", "")
    created_at  = item.get("created_at", "")
    approve_url = event.get("approve_url", "")
    reject_url  = event.get("reject_url", "")

    subject = f"REMINDER: {action_type.upper()} on {instance_id} still awaiting approval"
    message = (
        "This is a reminder that the following action is still waiting for your approval:\n\n"
        f"  Instance:  {instance_id}\n"
        f"  Action:    {action_type}\n"
        f"  Reason:    {reason}\n"
        f"  Requested: {created_at}\n\n"
        "This request has been pending for over 20 minutes.\n\n"
        f"Click to APPROVE:\n{approve_url}\n\n"
        f"Click to REJECT:\n{reject_url}\n\n"
        "This link expires 24 hours after the original request.\n\n"
        "\u2014 DevOps AI Agent"
    )

    notify(subject, message)
    print(f"Reminder sent for approval {approval_id}")

    table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET reminder_sent = :true, reminder_sent_at = :t",
        ExpressionAttributeValues={
            ":true": True,
            ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )

    cleanup_reminder_schedule(approval_id)
    return {"statusCode": 200, "body": "Reminder sent"}


# ── Helpers ──────────────────────────────────────────────────────────────

def cleanup_reminder_schedule(approval_id):
    schedule_name = f"devops-approval-reminder-{approval_id[:8]}"
    try:
        scheduler = boto3.client("scheduler", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
        scheduler.delete_schedule(Name=schedule_name)
        print(f"Deleted reminder schedule: {schedule_name}")
    except Exception as e:
        print(f"Could not delete schedule {schedule_name}: {e}")


def notify(subject, message):
    if SNS_TOPIC_ARN:
        try:
            sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
        except Exception as e:
            print(f"Failed to send notification: {e}")


def _html(code, title, body):
    if "Approved" in title:
        color = "#16a34a"
    elif "Rejected" in title:
        color = "#dc2626"
    else:
        color = "#333"
    html = (
        "<!DOCTYPE html>"
        f"<html><head><title>{title}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:600px;"
        "margin:60px auto;padding:20px;color:#1e293b;}"
        f"h1{{color:{color};}}"
        "code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:0.9em;}"
        ".badge{display:inline-block;padding:4px 12px;border-radius:12px;"
        f"background:{color};color:white;font-weight:600;font-size:0.85em;}}"
        "</style>"
        f"</head><body>"
        f"<h1>{title}</h1>"
        f"<p>{body}</p>"
        '<p style="color:#94a3b8;margin-top:40px;font-size:0.85em;">'
        "DevOps AI Agent</p>"
        "</body></html>"
    )
    return {
        "statusCode": code,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }


def _response(code, msg):
    return {"statusCode": code, "body": json.dumps({"message": msg})}
