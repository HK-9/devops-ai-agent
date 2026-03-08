"""
EventBridge / CloudWatch alarm event parser.

Converts raw EventBridge JSON events into typed dataclasses that
the Lambda handler and agent can work with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AlarmEvent:
    """Parsed CloudWatch alarm event from EventBridge."""

    alarm_name: str
    alarm_description: str
    state: str  # "ALARM" | "OK" | "INSUFFICIENT_DATA"
    previous_state: str
    reason: str
    timestamp: str
    region: str
    account_id: str
    instance_id: str  # Extracted from alarm dimensions
    metric_name: str
    namespace: str
    threshold: float
    comparison_operator: str
    evaluation_periods: int
    period: int
    raw_event: dict[str, Any] = field(repr=False, default_factory=dict)


def parse_eventbridge_alarm(event: dict[str, Any]) -> AlarmEvent:
    """Parse a CloudWatch alarm state-change event from EventBridge.

    Supports the ``aws.cloudwatch`` source with detail-type
    ``CloudWatch Alarm State Change``.

    Args:
        event: Raw EventBridge event dict.

    Returns:
        An :class:`AlarmEvent` instance.

    Raises:
        ValueError: If the event is not a recognised alarm event.
    """
    source = event.get("source", "")
    if source != "aws.cloudwatch":
        raise ValueError(f"Unsupported event source: {source}")

    detail = event.get("detail", {})
    if not detail:
        raise ValueError("Event has no 'detail' field")

    # The detail itself may be a JSON string in some test payloads
    if isinstance(detail, str):
        detail = json.loads(detail)

    # Extract the alarm configuration
    config = detail.get("configuration", {})
    metrics = config.get("metrics", [{}])
    metric_stat = metrics[0].get("metricStat", {}) if metrics else {}
    metric = metric_stat.get("metric", {})
    dimensions = metric.get("dimensions", {})

    # Try to find the instance ID from dimensions
    instance_id = dimensions.get("InstanceId", "")

    # State info
    state = detail.get("state", {})
    previous_state = detail.get("previousState", {})

    return AlarmEvent(
        alarm_name=detail.get("alarmName", ""),
        alarm_description=detail.get("alarmDescription", ""),
        state=state.get("value", "UNKNOWN"),
        previous_state=previous_state.get("value", "UNKNOWN"),
        reason=state.get("reason", ""),
        timestamp=state.get("timestamp", event.get("time", "")),
        region=event.get("region", ""),
        account_id=event.get("account", ""),
        instance_id=instance_id,
        metric_name=metric.get("name", ""),
        namespace=metric.get("namespace", ""),
        threshold=float(config.get("threshold", 0)),
        comparison_operator=config.get("comparisonOperator", ""),
        evaluation_periods=int(config.get("evaluationPeriods", 0)),
        period=int(metric_stat.get("period", 0)),
        raw_event=event,
    )


def build_agent_prompt_from_alarm(alarm: AlarmEvent) -> str:
    """Convert a parsed alarm event into a natural-language prompt for the agent.

    Args:
        alarm: Parsed :class:`AlarmEvent`.

    Returns:
        A prompt string suitable for AgentCore invocation.
    """
    severity = "CRITICAL" if "GreaterThanThreshold" in alarm.comparison_operator else "WARNING"

    return (
        f"🚨 **CloudWatch Alarm Triggered**\n\n"
        f"- **Alarm**: {alarm.alarm_name}\n"
        f"- **Severity**: {severity}\n"
        f"- **Instance**: {alarm.instance_id}\n"
        f"- **Metric**: {alarm.namespace}/{alarm.metric_name}\n"
        f"- **Reason**: {alarm.reason}\n"
        f"- **State**: {alarm.previous_state} → {alarm.state}\n"
        f"- **Threshold**: {alarm.comparison_operator} {alarm.threshold}\n\n"
        f"Please:\n"
        f"1. Retrieve current {alarm.metric_name} metrics for instance {alarm.instance_id}\n"
        f"2. Describe the instance to check its current state\n"
        f"3. Report findings to the Teams channel with an incident notification\n"
        f"4. If the situation is critical, consider restarting the instance\n"
    )
