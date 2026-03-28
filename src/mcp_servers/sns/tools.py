"""
AWS SNS MCP tools.

Provides an alert tool with Teams-to-SNS failover logic,
and a request_approval tool for the human-in-the-loop workflow.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import boto3
import httpx

from src.utils.aws_helpers import setup_logging

logger = setup_logging("mcp.sns")


# ── Tool Implementations ────────────────────────────────────────────────


async def send_alert_with_failover(subject: str, message: str) -> dict[str, Any]:
    """Send an alert to Teams, failing over to AWS SNS if Teams is unavailable.

    Primary: POST to the Microsoft Teams Webhook URL from the
    ``TEAMS_WEBHOOK_URL`` environment variable.

    Failover: If the webhook URL is missing or the Teams API returns an
    error, publish the same *subject* and *message* to the AWS SNS topic
    whose ARN is in the ``SNS_TOPIC_ARN`` environment variable.

    Args:
        subject: Alert subject line.
        message: Alert body text.

    Returns:
        Dict with ``tool``, ``channel`` ("teams" | "sns" | "none"),
        and a human-readable ``status`` string.
    """
    teams_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()

    # ── Primary: Teams ───────────────────────────────────────────────
    if teams_url:
        try:
            card = {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": f"**{subject}**", "weight": "Bolder", "size": "Medium"},
                    {"type": "TextBlock", "text": message, "wrap": True},
                ],
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(teams_url, json=card)
                resp.raise_for_status()
            logger.info("Alert delivered to Teams: %s", subject)
            return {
                "tool": "send_alert_with_failover",
                "channel": "teams",
                "status": "Alert sent to Teams successfully.",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Teams delivery failed (%s), attempting SNS failover …", exc)
    else:
        logger.warning("TEAMS_WEBHOOK_URL not set, attempting SNS failover …")

    # ── Failover: SNS ────────────────────────────────────────────────
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN", "").strip()
    if not sns_topic_arn:
        logger.error("SNS_TOPIC_ARN not set – both channels unavailable")
        return {
            "tool": "send_alert_with_failover",
            "channel": "none",
            "status": "Both Teams and SNS failed: TEAMS_WEBHOOK_URL not set and SNS_TOPIC_ARN not configured.",
        }

    try:
        sns_client = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
        sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message,
        )
        logger.info("Alert delivered via SNS failover: %s", subject)
        return {
            "tool": "send_alert_with_failover",
            "channel": "sns",
            "status": "Teams was unavailable; alert sent to SNS successfully.",
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("SNS delivery also failed: %s", exc)
        return {
            "tool": "send_alert_with_failover",
            "channel": "none",
            "status": f"Both Teams and SNS failed. SNS error: {exc}",
        }


async def request_approval(
    instance_id: str,
    action_type: str,
    reason: str,
    details: str = "",
) -> dict[str, Any]:
    """Store a proposed remediation action and send an email with approve/reject links.

    The engineer clicks the link to approve or reject; the Approval Handler
    Lambda at the other end of the API Gateway executes the action.

    Supported ``action_type`` values:  restart, disk_cleanup, kill_process,
    cache_clear.

    Args:
        instance_id:  Target EC2 instance.
        action_type:  Kind of remediation ("restart", "disk_cleanup", etc.).
        reason:       Why this action is proposed.
        details:      Extra context (e.g. PID to kill).

    Returns:
        Dict with ``approval_id`` and ``status``.
    """
    table_name = os.environ.get("APPROVALS_TABLE", "").strip()
    api_url    = os.environ.get("APPROVAL_API_URL", "").strip().rstrip("/")

    if not table_name or not api_url:
        logger.error("APPROVALS_TABLE or APPROVAL_API_URL not set")
        return {
            "tool": "request_approval",
            "error": True,
            "message": "Approval infrastructure not configured (missing APPROVALS_TABLE or APPROVAL_API_URL).",
        }

    approval_id = str(uuid.uuid4())
    ttl = int(time.time()) + 86400  # 24-hour expiry

    # ── Store in DynamoDB ────────────────────────────────────────────
    ddb = boto3.resource("dynamodb", region_name="ap-southeast-2")
    table = ddb.Table(table_name)
    table.put_item(Item={
        "approval_id": approval_id,
        "instance_id": instance_id,
        "action_type": action_type,
        "reason": reason,
        "details": details,
        "status": "pending",
        "reminder_sent": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": ttl,
    })

    approve_url = f"{api_url}/approve/{approval_id}"
    reject_url  = f"{api_url}/reject/{approval_id}"

    # ── Send notification with approve/reject links ──────────────────
    subject = f"ACTION REQUIRED: {action_type.upper()} on {instance_id}"
    message = (
        f"The DevOps AI Agent proposes the following action:\n\n"
        f"  Instance:  {instance_id}\n"
        f"  Action:    {action_type}\n"
        f"  Reason:    {reason}\n"
        f"  Details:   {details}\n\n"
        f"Click to APPROVE:\n{approve_url}\n\n"
        f"Click to REJECT:\n{reject_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"— DevOps AI Agent"
    )

    await send_alert_with_failover(subject=subject, message=message)

    # ── Schedule a one-time reminder 20 minutes from now ─────────────
    _schedule_reminder(approval_id, approve_url, reject_url)

    logger.info("Approval request %s created for %s on %s", approval_id, action_type, instance_id)
    return {
        "tool": "request_approval",
        "approval_id": approval_id,
        "status": f"Approval request sent. Waiting for human decision. Approve: {approve_url}",
    }


def _schedule_reminder(approval_id: str, approve_url: str, reject_url: str) -> None:
    """Create a one-time EventBridge schedule that fires 20 min from now.

    The schedule invokes the Approval Handler Lambda with a reminder
    payload.  The handler checks if the approval is still pending and
    resends the email.  The schedule auto-deletes after firing.
    """
    scheduler_role_arn    = os.environ.get("SCHEDULER_ROLE_ARN", "").strip()
    approval_handler_arn  = os.environ.get("APPROVAL_HANDLER_ARN", "").strip()

    if not scheduler_role_arn or not approval_handler_arn:
        logger.warning("SCHEDULER_ROLE_ARN or APPROVAL_HANDLER_ARN not set — skipping reminder schedule")
        return

    import json as _json
    from datetime import datetime, timezone, timedelta

    fire_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    schedule_expression = f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})"
    # Schedule names must be unique; use first 8 chars of UUID
    schedule_name = f"devops-approval-reminder-{approval_id[:8]}"

    payload = _json.dumps({
        "source": "devops-agent-reminder",
        "approval_id": approval_id,
        "approve_url": approve_url,
        "reject_url": reject_url,
    })

    try:
        scheduler_client = boto3.client("scheduler", region_name="ap-southeast-2")
        scheduler_client.create_schedule(
            Name=schedule_name,
            ScheduleExpression=schedule_expression,
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": approval_handler_arn,
                "RoleArn": scheduler_role_arn,
                "Input": payload,
            },
            ActionAfterCompletion="DELETE",  # Auto-clean after firing
        )
        logger.info("Reminder schedule '%s' created for %s at %s", schedule_name, approval_id, fire_at.isoformat())
    except Exception as exc:
        logger.error("Failed to create reminder schedule: %s", exc)
 