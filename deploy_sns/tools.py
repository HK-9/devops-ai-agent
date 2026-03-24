"""
AWS SNS MCP tools.

Provides an alert tool with Teams-to-SNS failover logic.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
import httpx

from aws_helpers import setup_logging

logger = setup_logging("mcp.sns")


# ── Tool Implementations ────────────────────────────────────────────────


async def send_alert_with_failover(subject: str, message: str) -> dict[str, Any]:
    """Send an alert to Teams, failing over to AWS SNS if Teams is unavailable."""
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
