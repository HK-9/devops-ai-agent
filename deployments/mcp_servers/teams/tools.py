"""
Microsoft Teams MCP tools.

Provides notification tools: send plain messages and create
structured incident notifications via Teams Incoming Webhooks.
"""

from __future__ import annotations

from typing import Any

from aws_helpers import setup_logging
from teams_webhook import (
    build_incident_card,
    post_adaptive_card,
    post_plain_message,
)

logger = setup_logging("mcp.teams")


# ── Tool Implementations ────────────────────────────────────────────────


async def send_teams_message(message: str) -> dict[str, Any]:
    """Send a plain-text message to the configured Teams channel."""
    logger.info("Sending Teams message (%d chars)", len(message))
    result = await post_plain_message(message)
    return {"tool": "send_teams_message", **result}


async def create_incident_notification(
    severity: str,
    instance_id: str,
    alarm_name: str,
    metric_value: str,
    summary: str,
    actions_taken: str = "None yet",
) -> dict[str, Any]:
    """Send a structured incident Adaptive Card to Teams."""
    logger.info(
        "Creating incident notification: severity=%s instance=%s",
        severity,
        instance_id,
    )
    card_body = build_incident_card(
        severity=severity,
        instance_id=instance_id,
        alarm_name=alarm_name,
        metric_value=metric_value,
        summary=summary,
        actions_taken=actions_taken,
    )
    result = await post_adaptive_card(card_body)
    return {"tool": "create_incident_notification", "severity": severity, **result}
