"""Routes for the DevOps AI Agent Web UI.

All agent interactions go through the Strands Agent (web.agent) which
connects to the AgentCore MCP Gateway.  No dependency on ``src/``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from flask import Flask, jsonify, render_template, request, session

from web.agent import invoke as invoke_agent

logger = logging.getLogger(__name__)

# Alarm name prefix — the provisioner Lambda creates per-instance alarms
# with names like devops-agent-high-cpu-31d3b38f, so we discover them
# dynamically via AlarmNamePrefix.
ALARM_NAME_PREFIX = "devops-agent-high-"


def get_cloudwatch_alarms() -> dict[str, Any]:
    """Fetch current state of monitored CloudWatch alarms (dynamic discovery).

    Returns dict with 'alarms' list and 'stats' summary.
    """
    region = os.environ.get("AWS_REGION", "ap-southeast-2")

    try:
        cw = boto3.client("cloudwatch", region_name=region)
        response = cw.describe_alarms(AlarmNamePrefix=ALARM_NAME_PREFIX)

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
                "state_updated": (
                    alarm.get("StateUpdatedTimestamp", "").isoformat()
                    if alarm.get("StateUpdatedTimestamp")
                    else ""
                ),
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
            "error": "AWS credentials not configured",
        }
    except ClientError as e:
        logger.warning("CloudWatch API error: %s", e)
        return {
            "alarms": [],
            "stats": {"total": 0, "alarm": 0, "ok": 0, "insufficient_data": 0},
            "error": str(e),
        }

_incidents: list[dict[str, Any]] = []
_audit_logs: list[dict[str, Any]] = []


# ── Query detection helpers ──────────────────────────────────────────
# These detect the user's intent so we can craft a targeted prompt that
# nudges the Strands Agent to the right MCP tool on the first turn.

def extract_instance_id(message: str) -> str | None:
    """Extract an EC2 instance ID from the user message."""
    match = re.search(r"\b(i-[a-zA-Z0-9]+)\b", message)
    return match.group(1) if match else None


def infer_minutes_window(message: str) -> int:
    """Infer metrics lookback window from user message."""
    text = message.lower()
    if "24h" in text or "24 hour" in text:
        return 1440
    if "6h" in text or "6 hour" in text:
        return 360
    if "2h" in text or "2 hour" in text:
        return 120
    if "30 min" in text:
        return 30
    return 60


def _is_ec2_list_query(msg: str) -> bool:
    t = msg.lower()
    return (
        ("ec2" in t or "instance" in t)
        and any(w in t for w in ["list", "show", "which", "what", "all"])
    )


def _is_cpu_query(msg: str) -> bool:
    t = msg.lower()
    return "cpu" in t and any(w in t for w in ["metric", "usage", "utilization", "show", "get", "check"])


def _is_memory_query(msg: str) -> bool:
    t = msg.lower()
    return ("memory" in t or "mem" in t) and any(w in t for w in ["metric", "usage", "utilization", "show", "get", "check"])


def _is_disk_query(msg: str) -> bool:
    t = msg.lower()
    return "disk" in t and any(w in t for w in ["metric", "usage", "space", "show", "get", "check"])


def _is_describe_query(msg: str) -> bool:
    t = msg.lower()
    return any(w in t for w in ["describe", "detail", "info about", "information"]) and (
        "instance" in t or "ec2" in t or extract_instance_id(msg) is not None
    )


def _is_diagnose_query(msg: str) -> bool:
    t = msg.lower()
    return any(w in t for w in ["diagnose", "diagnostic", "health check", "troubleshoot"])


def _is_send_alert_query(msg: str) -> bool:
    t = msg.lower()
    return any(w in t for w in ["send alert", "send notification", "notify team", "alert team", "send email"])


def _is_remediate_query(msg: str) -> bool:
    t = msg.lower()
    return any(w in t for w in ["remediate", "fix", "kill process", "cleanup", "clean up", "free disk"])


def _is_restart_query(msg: str) -> bool:
    t = msg.lower()
    return any(w in t for w in ["restart", "reboot", "stop and start"])


def _is_ssm_query(msg: str) -> bool:
    t = msg.lower()
    return any(w in t for w in ["run command", "ssm", "execute", "shell command"]) and (
        "instance" in t or extract_instance_id(msg) is not None
    )


def _is_approval_query(msg: str) -> bool:
    t = msg.lower()
    return any(w in t for w in ["request approval", "approval request", "approve", "need approval"])


def _is_teams_query(msg: str) -> bool:
    t = msg.lower()
    return "teams" in t and any(w in t for w in ["send", "message", "notify", "post", "incident"])


def _build_targeted_prompt(message: str) -> str:
    """Optionally augment the user message with tool hints.

    If we can detect the intent, we add a brief instruction so the agent
    picks the right MCP tool on the first turn.  Otherwise we return the
    raw message and let the agent reason freely.
    """
    iid = extract_instance_id(message)
    minutes = infer_minutes_window(message)

    if _is_ec2_list_query(message):
        state = "all"
        t = message.lower()
        if "running" in t:
            state = "running"
        elif "stopped" in t:
            state = "stopped"
        return (
            f"{message}\n\n[Hint: use list_ec2_instances_tool with "
            f"state_filter=\"{state}\"]"
        )

    if _is_cpu_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use get_cpu_metrics_tool with "
            f"instance_id=\"{iid}\", minutes={minutes}]"
        )

    if _is_memory_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use get_memory_metrics_tool with "
            f"instance_id=\"{iid}\", minutes={minutes}]"
        )

    if _is_disk_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use get_disk_usage_tool with "
            f"instance_id=\"{iid}\", minutes={minutes}]"
        )

    if _is_describe_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use describe_ec2_instance_tool with "
            f"instance_id=\"{iid}\"]"
        )

    if _is_diagnose_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use diagnose_instance_tool with "
            f"instance_id=\"{iid}\"]"
        )

    if _is_send_alert_query(message):
        return (
            f"{message}\n\n[Hint: use send_alert_with_failover_tool]"
        )

    if _is_remediate_query(message) and iid:
        t = message.lower()
        if "cpu" in t:
            return f"{message}\n\n[Hint: use remediate_high_cpu_tool with instance_id=\"{iid}\"]"
        if "disk" in t:
            return f"{message}\n\n[Hint: use remediate_disk_full_tool with instance_id=\"{iid}\"]"
        if "memory" in t or "mem" in t:
            return f"{message}\n\n[Hint: use remediate_high_memory_tool with instance_id=\"{iid}\"]"

    if _is_restart_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use request_approval_tool with "
            f"instance_id=\"{iid}\", action_type=\"restart\"]"
        )

    if _is_ssm_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use run_ssm_command_tool with "
            f"instance_id=\"{iid}\"]"
        )

    if _is_approval_query(message) and iid:
        return (
            f"{message}\n\n[Hint: use request_approval_tool with "
            f"instance_id=\"{iid}\"]"
        )

    if _is_teams_query(message):
        if "incident" in message.lower():
            return f"{message}\n\n[Hint: use create_incident_notification_tool]"
        return f"{message}\n\n[Hint: use send_teams_message_tool]"

    # No specific detection — let the agent reason freely
    return message


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


def _detect_alarm_type(alarm_name: str, metric_name: str) -> str:
    """Classify alarm as 'cpu', 'memory', or 'disk'."""
    combined = (alarm_name + " " + metric_name).lower()
    if any(ind in combined for ind in ("mem_used_percent", "memory", "mem")):
        return "memory"
    if any(ind in combined for ind in ("disk_used_percent", "disk", "diskspace")):
        return "disk"
    return "cpu"


def _build_alarm_prompt(
    *, instance_id: str, alarm_name: str, metric_name: str, reason: str, threshold: float
) -> str:
    """Build a natural-language prompt from a simulated alarm event."""
    severity = "CRITICAL"
    alarm_type = _detect_alarm_type(alarm_name, metric_name)

    header = (
        f"A CloudWatch alarm fired for instance {instance_id}. "
        f"Alarm: {alarm_name}. Metric: {metric_name}. "
        f"Reason: {reason}. Severity: {severity}.\n\n"
        f"You MUST follow these steps IN ORDER. "
        f"Do NOT call list_ec2_instances.\n"
        f"IMPORTANT: For MAJOR issues you MUST call the request_approval tool.\n\n"
    )

    if alarm_type == "memory":
        return header + (
            f"1. Call diagnose_instance with instance_id=\"{instance_id}\"\n"
            f"2. Call get_memory_metrics with instance_id=\"{instance_id}\" and minutes=30\n"
            f"3. Call describe_ec2_instance with instance_id=\"{instance_id}\"\n"
            f"4. Classify as MINOR or MAJOR\n"
            f"5a. MINOR: remediate_high_memory  5b. MAJOR: request_approval\n"
            f"6. Call send_alert_with_failover with full details\n"
        )
    if alarm_type == "disk":
        return header + (
            f"1. Call diagnose_instance with instance_id=\"{instance_id}\"\n"
            f"2. Call get_disk_usage with instance_id=\"{instance_id}\" and minutes=30\n"
            f"3. Call describe_ec2_instance with instance_id=\"{instance_id}\"\n"
            f"4. Classify as MINOR or MAJOR\n"
            f"5a. MINOR: remediate_disk_full  5b. MAJOR: request_approval\n"
            f"6. Call send_alert_with_failover with full details\n"
        )
    # CPU
    return header + (
        f"1. Call diagnose_instance with instance_id=\"{instance_id}\"\n"
        f"2. Call get_cpu_metrics with instance_id=\"{instance_id}\" and minutes=30\n"
        f"3. Call describe_ec2_instance with instance_id=\"{instance_id}\"\n"
        f"4. Classify as MINOR or MAJOR\n"
        f"5a. MINOR: remediate_high_cpu  5b. MAJOR: request_approval\n"
        f"6. Call send_alert_with_failover with full details\n"
    )


def process_alert_with_agent(alert_payload: dict[str, Any]) -> dict[str, Any]:
    """Run a simulated alert through the Strands Agent."""
    session_id = str(uuid.uuid4())
    instance_id = alert_payload.get("instance_id", "")
    metric_name = alert_payload.get("metric_name", "CPUUtilization")
    alarm_name = alert_payload.get("alarm_name", "DemoAlarm")
    threshold = alert_payload.get("threshold", 0)
    metric_value = alert_payload.get("metric_value", 0)
    reason = (
        f"{metric_name} breached threshold: {metric_value} > {threshold}"
    )

    prompt = _build_alarm_prompt(
        instance_id=instance_id,
        alarm_name=alarm_name,
        metric_name=metric_name,
        reason=reason,
        threshold=threshold,
    )

    append_audit_entry(
        incident_id=session_id,
        phase="triage",
        result="SUCCESS",
        action="build_prompt_from_alarm",
        instance_id=instance_id,
        reasoning=prompt[:1000],
    )

    result = invoke_agent(prompt)
    response_text = result.get("response", "")
    status = derive_status(result)

    append_audit_entry(
        incident_id=session_id,
        phase=(
            "verification" if status == "resolved"
            else "escalation" if status == "escalated"
            else "remediation"
        ),
        result=status.upper(),
        action="agent_completed",
        instance_id=instance_id,
        reasoning=response_text[:1000],
    )

    incident = {
        "incident_id": session_id,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": (
            datetime.now(timezone.utc).isoformat() if status == "resolved" else None
        ),
        "triage": {
            "alert_type": alert_payload.get("alert_type"),
            "instance_id": instance_id,
            "severity": "P1" if metric_value >= 95 else "P2",
            "alarm_name": alarm_name,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "threshold": threshold,
            "region": alert_payload.get("region"),
        },
        "agent_response": response_text,
        "tool_calls": [],
        "turns": 0,
        "raw_alert": alert_payload,
    }

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

            # Build a targeted prompt (adds tool hints when intent is clear)
            prompt = _build_targeted_prompt(message)

            # Invoke the Strands Agent (synchronous, thread-safe)
            result = invoke_agent(prompt)

            append_audit_entry(
                incident_id=session["session_id"],
                phase="diagnosis",
                result="SUCCESS" if not result.get("error") else "BLOCKED",
                action="chat",
                reasoning=message,
            )

            if result.get("error"):
                return jsonify({"error": result["error"]}), 500

            return jsonify(
                {
                    "response": result.get("response", ""),
                    "session_id": session["session_id"],
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
            incident = process_alert_with_agent(alert)

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
 