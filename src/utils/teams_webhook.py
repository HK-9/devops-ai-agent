"""
Low-level HTTP helper for posting messages to a Microsoft Teams
Incoming Webhook URL.

Supports plain-text messages and Adaptive Cards.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.agent.config import settings
from src.utils.aws_helpers import setup_logging

logger = setup_logging("teams-webhook")

# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 10.0  # seconds


# ── Public API ───────────────────────────────────────────────────────────


async def post_plain_message(text: str, webhook_url: str | None = None) -> dict[str, Any]:
    """Send a plain-text message to Teams via Incoming Webhook.

    Args:
        text: The message body.
        webhook_url: Override URL; falls back to ``settings.teams_webhook_url``.

    Returns:
        dict with ``ok`` bool and optional ``status_code`` / ``error``.
    """
    url = webhook_url or settings.teams_webhook_url
    if not url:
        return {"ok": False, "error": "TEAMS_WEBHOOK_URL is not configured"}

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": text,
                "wrap": True,
            }
        ],
    }
    return await _post(url, card)


async def post_adaptive_card(card_body: list[dict[str, Any]], webhook_url: str | None = None) -> dict[str, Any]:
    """Send an Adaptive Card to Teams.

    Args:
        card_body: List of Adaptive Card body elements.
        webhook_url: Override URL.

    Returns:
        dict with ``ok`` bool.
    """
    url = webhook_url or settings.teams_webhook_url
    if not url:
        return {"ok": False, "error": "TEAMS_WEBHOOK_URL is not configured"}

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": card_body,
    }
    
    return await _post(url, card)


def build_incident_card(
    severity: str,
    instance_id: str,
    alarm_name: str,
    metric_value: str,
    summary: str,
    actions_taken: str = "None yet",
) -> list[dict[str, Any]]:
    """Build an Adaptive Card body for an incident notification.

    Returns a ``body`` list ready to pass to :func:`post_adaptive_card`.
    """
    severity_color_map = {
        "CRITICAL": "attention",
        "WARNING": "warning",
        "INFO": "good",
    }
    color = severity_color_map.get(severity.upper(), "default")

    return [
        {
            "type": "TextBlock",
            "text": f"🚨 Incident — {severity.upper()}",
            "weight": "Bolder",
            "size": "Large",
            "color": color,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Instance", "value": instance_id},
                {"title": "Alarm", "value": alarm_name},
                {"title": "Metric", "value": metric_value},
                {"title": "Actions Taken", "value": actions_taken},
            ],
        },
        {
            "type": "TextBlock",
            "text": summary,
            "wrap": True,
        },
    ]


# ── Internal ─────────────────────────────────────────────────────────────


async def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fire-and-forget HTTP POST with error handling."""
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Teams webhook delivered (status=%s)", resp.status_code)
            return {"ok": True, "status_code": resp.status_code}
    except httpx.HTTPStatusError as exc:
        logger.error("Teams webhook HTTP error: %s", exc)
        return {"ok": False, "status_code": exc.response.status_code, "error": str(exc)}
    except httpx.RequestError as exc:
        logger.error("Teams webhook request error: %s", exc)
        return {"ok": False, "error": str(exc)}
