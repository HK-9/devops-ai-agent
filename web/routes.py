"""Routes for the DevOps AI Agent Web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from flask import Flask, jsonify, render_template, request, session

from src.agent.agent_core import DevOpsAgent
from src.handlers.event_parser import build_agent_prompt_from_alarm, parse_eventbridge_alarm

logger = logging.getLogger(__name__)

_agent: DevOpsAgent | None = None
_incidents: list[dict[str, Any]] = []
_audit_logs: list[dict[str, Any]] = []


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


async def get_agent() -> DevOpsAgent:
    """Get or create the shared agent instance."""
    global _agent
    if _agent is None:
        _agent = DevOpsAgent()
        await _agent.initialise()
    return _agent


def build_stats() -> dict[str, int]:
    """Compute dashboard stats."""
    return {
        "total_incidents": len(_incidents),
        "resolved": sum(1 for i in _incidents if i.get("status") == "resolved"),
        "escalated": sum(1 for i in _incidents if i.get("status") == "escalated"),
        "active": sum(1 for i in _incidents if i.get("status") not in ("resolved", "escalated", "rejected")),
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
            "timestamp": datetime.now(UTC).isoformat(),
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
    agent = await get_agent()

    eventbridge_event = {
        "source": "aws.cloudwatch",
        "account": "demo-account",
        "region": alert_payload.get("region", "us-east-1"),
        "time": alert_payload.get("timestamp", datetime.now(UTC).isoformat()),
        "detail": {
            "alarmName": alert_payload.get("alarm_name", "DemoAlarm"),
            "alarmDescription": f"Simulated alert for {alert_payload.get('alert_type', 'unknown')}",
            "state": {
                "value": "ALARM",
                "reason": (
                    f"{alert_payload.get('metric_name', 'Metric')} breached threshold: "
                    f"{alert_payload.get('metric_value')} > {alert_payload.get('threshold')}"
                ),
                "timestamp": alert_payload.get("timestamp", datetime.now(UTC).isoformat()),
            },
            "previousState": {"value": "OK"},
            "configuration": {
                "metrics": [
                    {
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/EC2",
                                "name": alert_payload.get("metric_name", "CPUUtilization"),
                                "dimensions": {"InstanceId": alert_payload.get("instance_id", "")},
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

    agent_result = await agent.invoke(prompt, session_id=session_id)
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
        "created_at": datetime.now(UTC).isoformat(),
        "resolved_at": datetime.now(UTC).isoformat() if status == "resolved" else None,
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
        phase=("verification" if status == "resolved" else "escalation" if status == "escalated" else "remediation"),
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
        return render_template(
            "dashboard.html",
            incidents=_incidents[-20:],
            stats=build_stats(),
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

            async def _chat():
                agent = await get_agent()
                return await agent.invoke(message, session_id=session["session_id"])

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
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "disk_full": {
                    "instance_id": data.get("instance_id", "i-0def456ghi789"),
                    "alert_type": "disk_full",
                    "metric_name": "disk_used_percent",
                    "metric_value": 94.5,
                    "threshold": 90,
                    "alarm_name": "DiskFull-agent-managed",
                    "region": "us-east-1",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "unreachable": {
                    "instance_id": data.get("instance_id", "i-0ghi789jkl012"),
                    "alert_type": "unreachable",
                    "metric_name": "StatusCheckFailed",
                    "metric_value": 1,
                    "threshold": 0,
                    "alarm_name": "StatusCheckFailed-agent-managed",
                    "region": "us-east-1",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "memory_exhaustion": {
                    "instance_id": data.get("instance_id", "i-0mno345pqr678"),
                    "alert_type": "memory_exhaustion",
                    "metric_name": "mem_used_percent",
                    "metric_value": 96.8,
                    "threshold": 90,
                    "alarm_name": "HighMemory-agent-managed",
                    "region": "us-east-1",
                    "timestamp": datetime.now(UTC).isoformat(),
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
