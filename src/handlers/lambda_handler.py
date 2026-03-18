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
from src.handlers.event_parser import build_agent_prompt_from_alarm, parse_eventbridge_alarm
from src.utils.aws_helpers import setup_logging

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
