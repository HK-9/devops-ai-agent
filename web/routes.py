"""Routes for the DevOps AI Agent Web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
import re
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from flask import Flask, jsonify, render_template, request, session

from src.agent.agent_core import DevOpsAgent
from src.handlers.event_parser import build_agent_prompt_from_alarm, parse_eventbridge_alarm

logger = logging.getLogger(__name__)

# CloudWatch alarm names we monitor
MONITORED_ALARMS = [
    "devops-agent-high-cpu",
    "devops-agent-high-memory",
    "devops-agent-high-disk",
]


def get_cloudwatch_alarms() -> dict[str, Any]:
    """Fetch current state of monitored CloudWatch alarms.
    
    Returns dict with 'alarms' list and 'stats' summary.
    """
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        response = cw.describe_alarms(AlarmNames=MONITORED_ALARMS)
        
        alarms = []
        stats = {"total": 0, "alarm": 0, "ok": 0, "insufficient_data": 0}
        
        for alarm in response.get("MetricAlarms", []):
            state = alarm.get("StateValue", "UNKNOWN")
            alarm_data = {
                "name": alarm.get("AlarmName"),
                "state": state,
                "metric": alarm.get("MetricName"),
                "threshold": alarm.get("Threshold"),
                "description": alarm.get("AlarmDescription", ""),
                "state_reason": alarm.get("StateReason", ""),
                "state_updated": alarm.get("StateUpdatedTimestamp", "").isoformat() if alarm.get("StateUpdatedTimestamp") else "",
            }
            alarms.append(alarm_data)
            stats["total"] += 1
            
            if state == "ALARM":
                stats["alarm"] += 1
            elif state == "OK":
                stats["ok"] += 1
            else:
                stats["insufficient_data"] += 1
        
        return {"alarms": alarms, "stats": stats, "error": None}
        
    except NoCredentialsError:
        logger.warning("AWS credentials not configured")
        return {
            "alarms": [],
            "stats": {"total": 0, "alarm": 0, "ok": 0, "insufficient_data": 0},
            "error": "AWS credentials not configured"
        }
    except ClientError as e:
        logger.warning(f"CloudWatch API error: {e}")
        return {
            "alarms": [],
            "stats": {"total": 0, "alarm": 0, "ok": 0, "insufficient_data": 0},
            "error": str(e)
        }

_incidents: list[dict[str, Any]] = []
_audit_logs: list[dict[str, Any]] = []

def is_ec2_list_query(message: str) -> bool:
    """Detect simple EC2 inventory/list queries."""
    text = message.lower()
    return (
        ("ec2" in text or "instance" in text or "instances" in text)
        and any(word in text for word in ["list", "show", "which", "what"])
    )


def infer_ec2_state_filter(message: str) -> str:
    """Infer instance state filter from user message."""
    text = message.lower()

    if "all" in text:
        return "all"
    if "stopped" in text:
        return "stopped"

    return "running"


def format_ec2_instances(tool_result: dict[str, Any], state_filter: str) -> str:
    """Format EC2 list tool output into chat-friendly text."""
    if tool_result.get("error"):
        return tool_result.get("message", "Failed to fetch EC2 instances.")

    instances = tool_result.get("instances", [])
    count = tool_result.get("count", len(instances))

    if not instances:
        return f"No EC2 instances found for state filter: {state_filter}."

    lines = [f"Found {count} EC2 instance(s) with state filter '{state_filter}':", ""]

    for inst in instances:
        lines.append(
            f"- {inst.get('instance_id', 'N/A')} | "
            f"{inst.get('name') or 'Unnamed'} | "
            f"{inst.get('state', 'unknown')} | "
            f"{inst.get('instance_type', 'N/A')} | "
            f"Private IP: {inst.get('private_ip', 'N/A')} | "
            f"Public IP: {inst.get('public_ip', 'N/A')}"
        )

    return "\n".join(lines)

def is_cpu_metrics_query(message: str) -> bool:
    """Detect CPU metric queries for EC2."""
    text = message.lower()
    return (
        "cpu" in text
        and any(word in text for word in ["metric", "metrics", "utilization", "usage", "show", "get"])
    )


def extract_instance_id(message: str) -> str | None:
    """Extract an EC2 instance ID from the user message."""
    match = re.search(r"\b(i-[a-zA-Z0-9]+)\b", message)
    return match.group(1) if match else None


def infer_minutes_window(message: str) -> int:
    """Infer metrics lookback window from user message."""
    text = message.lower()

    if "24h" in text or "24 hour" in text or "24 hours" in text:
        return 1440
    if "6h" in text or "6 hour" in text or "6 hours" in text:
        return 360
    if "2h" in text or "2 hour" in text or "2 hours" in text:
        return 120
    if "30 min" in text or "30 mins" in text or "30 minutes" in text:
        return 30

    return 60


def format_cpu_metrics(tool_result: dict[str, Any], instance_id: str, minutes: int) -> str:
    """Format CPU metrics tool output into chat-friendly text."""
    if tool_result.get("error"):
        return tool_result.get("message", f"Failed to fetch CPU metrics for {instance_id}.")

    summary = tool_result.get("summary", {})
    datapoints = tool_result.get("datapoints", [])

    if not datapoints:
        return f"No CPU metrics found for {instance_id} in the last {minutes} minutes."

    lines = [
        f"CPU metrics for {instance_id} (last {minutes} minutes):",
        "",
        f"Average CPU: {summary.get('average', 'N/A')}%",
        f"Peak CPU: {summary.get('peak', 'N/A')}%",
        f"Latest CPU: {summary.get('latest', 'N/A')}%",
        f"Datapoints: {summary.get('datapoint_count', len(datapoints))}",
        "",
        "Recent datapoints:",
    ]

    for point in datapoints[-10:]:
        lines.append(
            f"- {point.get('timestamp', 'N/A')} | "
            f"avg: {point.get('average', 'N/A')}% | "
            f"max: {point.get('maximum', 'N/A')}% | "
            f"min: {point.get('minimum', 'N/A')}%"
        )

    return "\n".join(lines)

def run_async(coro):
    """Run async code from Flask sync routes."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


async def run_with_agent(callback):
    """Create a fresh agent for one request, then clean it up."""
    agent = DevOpsAgent()
    await agent.initialise()
    try:
        return await callback(agent)
    finally:
        await agent.shutdown()


def build_stats() -> dict[str, Any]:
    """Compute dashboard stats from CloudWatch alarms."""
    cw_data = get_cloudwatch_alarms()
    cw_stats = cw_data.get("stats", {})
    
    return {
        "total_alarms": cw_stats.get("total", 0),
        "active_alarms": cw_stats.get("alarm", 0),
        "ok_alarms": cw_stats.get("ok", 0),
        "insufficient_data": cw_stats.get("insufficient_data", 0),
        "aws_error": cw_data.get("error"),
        # Keep incident stats for simulations
        "total_incidents": len(_incidents),
        "resolved": sum(1 for i in _incidents if i.get("status") == "resolved"),
        "escalated": sum(1 for i in _incidents if i.get("status") == "escalated"),
        "active": sum(
            1
            for i in _incidents
            if i.get("status") not in ("resolved", "escalated", "rejected")
        ),
    }


def append_audit_entry(
    *,
    incident_id: str,
    phase: str,
    result: str,
    action: str,
    instance_id: str = "",
    reasoning: str = "",
    guardrail_check: str | None = None,
) -> None:
    """Append an audit log record."""
    _audit_logs.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "incident_id": incident_id,
            "phase": phase,
            "result": result,
            "action": action,
            "instance_id": instance_id,
            "reasoning": reasoning,
            "guardrail_check": guardrail_check,
        }
    )


def derive_status(agent_result: dict[str, Any]) -> str:
    """Infer dashboard status from agent response text."""
    text = (agent_result.get("response") or "").lower()
    if "escalat" in text:
        return "escalated"
    if "resolved" in text or "remediated" in text or "healthy" in text:
        return "resolved"
    return "active"


async def process_alert_with_agent(alert_payload: dict[str, Any]) -> dict[str, Any]:
    """Run a simulated alert through the real DevOps agent."""


    eventbridge_event = {
        "source": "aws.cloudwatch",
        "account": "demo-account",
        "region": alert_payload.get("region", "us-east-1"),
        "time": alert_payload.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "detail": {
            "alarmName": alert_payload.get("alarm_name", "DemoAlarm"),
            "alarmDescription": f"Simulated alert for {alert_payload.get('alert_type', 'unknown')}",
            "state": {
                "value": "ALARM",
                "reason": (
                    f"{alert_payload.get('metric_name', 'Metric')} breached threshold: "
                    f"{alert_payload.get('metric_value')} > {alert_payload.get('threshold')}"
                ),
                "timestamp": alert_payload.get(
                    "timestamp", datetime.now(timezone.utc).isoformat()
                ),
            },
            "previousState": {"value": "OK"},
            "configuration": {
                "metrics": [
                    {
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/EC2",
                                "name": alert_payload.get("metric_name", "CPUUtilization"),
                                "dimensions": {
                                    "InstanceId": alert_payload.get("instance_id", "")
                                },
                            },
                            "period": 300,
                        }
                    }
                ],
                "threshold": alert_payload.get("threshold", 0),
                "comparisonOperator": "GreaterThanThreshold",
                "evaluationPeriods": 1,
            },
        },
    }

    alarm = parse_eventbridge_alarm(eventbridge_event)
    prompt = build_agent_prompt_from_alarm(alarm)
    session_id = str(uuid.uuid4())

    append_audit_entry(
        incident_id=session_id,
        phase="triage",
        result="SUCCESS",
        action="build_prompt_from_alarm",
        instance_id=alarm.instance_id,
        reasoning=prompt,
    )

    async def _work(agent: DevOpsAgent):
        return await agent.invoke(prompt, session_id=session_id)

    agent_result = await run_with_agent(_work)
    status = derive_status(agent_result)

    tool_calls = agent_result.get("tool_calls", [])
    for tc in tool_calls:
        append_audit_entry(
            incident_id=session_id,
            phase="diagnosis",
            result="SUCCESS",
            action=tc.get("tool_call", "tool_call"),
            instance_id=alarm.instance_id,
            reasoning=json.dumps(tc, default=str)[:1000],
        )

    incident = {
        "incident_id": session_id,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": datetime.now(timezone.utc).isoformat() if status == "resolved" else None,
        "triage": {
            "alert_type": alert_payload.get("alert_type"),
            "instance_id": alert_payload.get("instance_id"),
            "severity": "P1" if alert_payload.get("metric_value", 0) >= 95 else "P2",
            "alarm_name": alert_payload.get("alarm_name"),
            "metric_name": alert_payload.get("metric_name"),
            "metric_value": alert_payload.get("metric_value"),
            "threshold": alert_payload.get("threshold"),
            "region": alert_payload.get("region"),
        },
        "agent_response": agent_result.get("response", ""),
        "tool_calls": tool_calls,
        "turns": agent_result.get("turns", 0),
        "raw_alert": alert_payload,
    }

    append_audit_entry(
        incident_id=session_id,
        phase=(
            "verification"
            if status == "resolved"
            else "escalation"
            if status == "escalated"
            else "remediation"
        ),
        result=status.upper(),
        action="agent_completed",
        instance_id=alarm.instance_id,
        reasoning=agent_result.get("response", ""),
    )

    _incidents.append(incident)
    return incident


def register_routes(app: Flask):
    """Register all routes on the Flask app."""

    @app.route("/")
    def dashboard():
        cw_data = get_cloudwatch_alarms()
        return render_template(
            "dashboard.html",
            incidents=_incidents[-20:],
            stats=build_stats(),
            alarms=cw_data.get("alarms", []),
            aws_error=cw_data.get("error"),
        )

    @app.route("/chat")
    def chat():
        if "session_id" not in session:
            session["session_id"] = str(uuid.uuid4())
        return render_template("chat.html")

    @app.route("/audit")
    def audit_log():
        return render_template("audit_log.html", logs=_audit_logs[-200:])

    @app.route("/api/chat", methods=["POST"])
    def chat_api():
        try:
            data = request.get_json() or {}
            message = data.get("message", "").strip()
            if not message:
                return jsonify({"error": "No message provided"}), 400

            if "session_id" not in session:
                session["session_id"] = str(uuid.uuid4())

            # Direct deterministic path for EC2 inventory queries
            if is_ec2_list_query(message):
                state_filter = infer_ec2_state_filter(message)

                async def _direct_ec2():
                    async def _work(agent: DevOpsAgent):
                        tool_result = await agent.call_tool(
                            "list_ec2_instances",
                            {
                                "state_filter": state_filter,
                                "max_results": 50,
                            },
                        )
                        return {
                            "response": format_ec2_instances(tool_result, state_filter),
                            "tool_calls": [
                                {
                                    "tool_call": "list_ec2_instances",
                                    "arguments": {
                                        "state_filter": state_filter,
                                        "max_results": 50,
                                    },
                                    "result": tool_result,
                                }
                            ],
                            "turns": 1,
                        }

                    return await run_with_agent(_work)

                result = run_async(_direct_ec2())
            elif is_cpu_metrics_query(message):
                instance_id = extract_instance_id(message)
                if not instance_id:
                    return jsonify(
                        {
                            "error": "Please provide an EC2 instance ID, for example: i-0123456789abcdef0"
                        }
                    ), 400

                minutes = infer_minutes_window(message)

                async def _direct_cpu():
                    async def _work(agent: DevOpsAgent):
                        tool_result = await agent.call_tool(
                            "get_cpu_metrics",
                            {
                                "instance_id": instance_id,
                                "period": 300,
                                "minutes": minutes,
                            },
                        )
                        return {
                            "response": format_cpu_metrics(tool_result, instance_id, minutes),
                            "tool_calls": [
                                {
                                    "tool_call": "get_cpu_metrics",
                                    "arguments": {
                                        "instance_id": instance_id,
                                        "period": 300,
                                        "minutes": minutes,
                                    },
                                    "result": tool_result,
                                }
                            ],
                            "turns": 1,
                        }

                    return await run_with_agent(_work)

                result = run_async(_direct_cpu())

            else:
                async def _chat():
                    async def _work(agent: DevOpsAgent):
                        return await agent.invoke(message, session_id=session["session_id"])

                    return await run_with_agent(_work)

                result = run_async(_chat())

            append_audit_entry(
                incident_id=session["session_id"],
                phase="diagnosis",
                result="SUCCESS" if not result.get("error") else "BLOCKED",
                action="chat",
                reasoning=message,
            )

            if result.get("error"):
                return jsonify({"error": result.get("message", "Unknown error")}), 500

            return jsonify(
                {
                    "response": result.get("response", ""),
                    "session_id": session["session_id"],
                    "tool_calls": result.get("tool_calls", []),
                    "turns": result.get("turns", 0),
                }
            )
        except Exception as exc:
            logger.exception("Chat error")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/incidents", methods=["GET"])
    def list_incidents():
        return jsonify({"incidents": _incidents})

    @app.route("/api/alarms", methods=["GET"])
    def list_alarms():
        """Fetch current CloudWatch alarm states."""
        cw_data = get_cloudwatch_alarms()
        return jsonify(cw_data)

    @app.route("/api/incidents/<incident_id>", methods=["GET"])
    def get_incident(incident_id: str):
        for incident in _incidents:
            if incident.get("incident_id") == incident_id:
                return jsonify(incident)
        return jsonify({"error": "Incident not found"}), 404

    @app.route("/api/audit", methods=["GET"])
    def get_audit_logs():
        incident_id = request.args.get("incident_id")
        logs = _audit_logs
        if incident_id:
            logs = [log for log in logs if log.get("incident_id") == incident_id]
        return jsonify({"logs": logs[-200:]})

    @app.route("/api/simulate", methods=["POST"])
    def simulate_alert():
        try:
            data = request.get_json() or {}
            scenario = data.get("scenario", "high_cpu")

            simulated_alerts = {
                "high_cpu": {
                    "instance_id": data.get("instance_id", "i-0abc123def456"),
                    "alert_type": "high_cpu",
                    "metric_name": "CPUUtilization",
                    "metric_value": 98.2,
                    "threshold": 95,
                    "alarm_name": "HighCPU-agent-managed",
                    "region": "us-east-1",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "disk_full": {
                    "instance_id": data.get("instance_id", "i-0def456ghi789"),
                    "alert_type": "disk_full",
                    "metric_name": "disk_used_percent",
                    "metric_value": 94.5,
                    "threshold": 90,
                    "alarm_name": "DiskFull-agent-managed",
                    "region": "us-east-1",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "unreachable": {
                    "instance_id": data.get("instance_id", "i-0ghi789jkl012"),
                    "alert_type": "unreachable",
                    "metric_name": "StatusCheckFailed",
                    "metric_value": 1,
                    "threshold": 0,
                    "alarm_name": "StatusCheckFailed-agent-managed",
                    "region": "us-east-1",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "memory_exhaustion": {
                    "instance_id": data.get("instance_id", "i-0mno345pqr678"),
                    "alert_type": "memory_exhaustion",
                    "metric_name": "mem_used_percent",
                    "metric_value": 96.8,
                    "threshold": 90,
                    "alarm_name": "HighMemory-agent-managed",
                    "region": "us-east-1",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }

            alert = simulated_alerts.get(scenario, simulated_alerts["high_cpu"])
            incident = run_async(process_alert_with_agent(alert))

            return jsonify(
                {
                    "incident_id": incident["incident_id"],
                    "status": incident["status"],
                    "scenario": scenario,
                    "alert": alert,
                    "agent_response": incident["agent_response"],
                    "turns": incident["turns"],
                }
            )
        except Exception as exc:
            logger.exception("Simulation error")
            return jsonify({"error": str(exc)}), 500

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify(
            {
                "status": "healthy",
                "service": "devops-ai-agent-web",
                "version": "0.1.0",
            }
        )
 