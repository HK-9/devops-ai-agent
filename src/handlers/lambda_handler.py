"""
AWS Lambda handler — EventBridge → DevOps Agent.

This is the entry point that AWS Lambda invokes when an EventBridge
rule matches (e.g. a CloudWatch alarm fires).  It parses the event,
constructs a prompt, and invokes the DevOps Agent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from src.agent.agent_core import DevOpsAgent
from src.handlers.event_parser import _detect_alarm_type, build_agent_prompt_from_alarm, parse_eventbridge_alarm
from src.utils.aws_helpers import setup_logging
from src.mcp_servers.sns.tools import send_alert_with_failover

# Emoji + label for each alarm type
_ALARM_LABELS: dict[str, tuple[str, str]] = {
    "cpu":    ("\U0001f6a8", "CPU Alert"),
    "memory": ("\U0001f9e0", "Memory Alert"),
    "disk":   ("\U0001f4be", "Disk Alert"),
}

logger = setup_logging("lambda-handler")

# Re-use the agent across warm Lambda invocations
_agent: DevOpsAgent | None = None


async def _get_agent() -> DevOpsAgent:
    """Lazily initialise the agent (warm-start friendly)."""
    global _agent
    if _agent is None:
        _agent = DevOpsAgent()
        await _agent.initialise()
    return _agent


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point.

    Receives an EventBridge event, parses it, and invokes the
    DevOps Agent with a generated prompt.

    Args:
        event:   EventBridge event dict.
        context: Lambda context object.

    Returns:
        Dict with ``statusCode`` and ``body``.
    """
    logger.info("Lambda invoked — event: %s", json.dumps(event, default=str)[:500])

    try:
        return asyncio.get_event_loop().run_until_complete(_async_handler(event, context))
    except RuntimeError:
        # No running event loop (standard Lambda env)
        return asyncio.run(_async_handler(event, context))


async def _async_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Async implementation of the Lambda handler."""

    # ── Parse the event ──────────────────────────────────────────────
    try:
        alarm = parse_eventbridge_alarm(event)
        prompt = build_agent_prompt_from_alarm(alarm)
        logger.info(
            "Parsed alarm: name=%s instance=%s state=%s",
            alarm.alarm_name,
            alarm.instance_id,
            alarm.state,
        )
    except ValueError as exc:
        logger.error("Failed to parse event: %s", exc)
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Event parse error: {exc}"}),
        }
    
    # ── Guaranteed immediate notification ──────────────────────────
    # Send directly to Teams the moment the alarm fires — this does NOT
    # depend on the LLM reasoning loop, so delivery is guaranteed.
    alarm_type = _detect_alarm_type(alarm)
    emoji, label = _ALARM_LABELS.get(alarm_type, ("\U0001f6a8", "Alert"))
    logger.info("Sending immediate %s notification for alarm: %s", label, alarm.alarm_name)
    await send_alert_with_failover(
        subject=f"{emoji} {label}: {alarm.alarm_name}",
        message=(
            f"Instance: {alarm.instance_id}\n"
            f"Alarm: {alarm.alarm_name}\n"
            f"Type: {alarm_type.upper()}\n"
            f"Reason: {alarm.reason}\n"
            f"Region: {alarm.region}\n"
            f"Time: {alarm.timestamp}\n\n"
            f"The DevOps Agent is now investigating and will follow up with detailed metrics."
        ),
    )

    # ── Invoke the agent ─────────────────────────────────────────────
    agent = await _get_agent()
    session_id = str(uuid.uuid4())

    result = await agent.invoke(prompt, session_id=session_id, is_alarm=True)

    if result.get("error"):
        logger.error("Agent invocation failed: %s", result.get("message"))
        return {
            "statusCode": 500,
            "body": json.dumps({"error": result.get("message")}),
        }

    logger.info(
        "Agent completed: %d tool calls, response length=%d",
        len(result.get("tool_calls", [])),
        len(result.get("response", "")),
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "alarm_name": alarm.alarm_name,
            "instance_id": alarm.instance_id,
            "agent_response": result.get("response", ""),
            "tool_calls_count": len(result.get("tool_calls", [])),
            "session_id": session_id,
        }),
    }
