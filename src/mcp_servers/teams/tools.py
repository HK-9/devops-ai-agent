"""
Microsoft Teams MCP tools.

Provides notification tools: send plain messages and create
structured incident notifications via Teams Incoming Webhooks.
"""

from __future__ import annotations

from typing import Any

from src.utils.aws_helpers import setup_logging
from src.utils.teams_webhook import (
    build_incident_card,
    post_adaptive_card,
    post_plain_message,
)

logger = setup_logging("mcp.teams")


# ── Tool Implementations ────────────────────────────────────────────────


async def send_teams_message(message: str) -> dict[str, Any]:
    """Send a plain-text message to the configured Teams channel.

    Args:
        message: The text body to send.

    Returns:
        Dict with ``ok`` bool and delivery status.
    """
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
    """Send a structured incident Adaptive Card to Teams.

    Args:
        severity:      "CRITICAL", "WARNING", or "INFO".
        instance_id:   The affected EC2 instance.
        alarm_name:    CloudWatch alarm name.
        metric_value:  Human-readable metric snapshot (e.g. "CPU 92.3%").
        summary:       Narrative summary of the incident.
        actions_taken: What actions the agent has taken so far.

    Returns:
        Dict with ``ok`` bool and delivery status.
    """
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
