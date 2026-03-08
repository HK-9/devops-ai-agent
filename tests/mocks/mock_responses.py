"""
Mock MCP server and API responses for testing.

These canned responses mirror real AWS/Teams API responses so unit
tests can run without external dependencies.
"""

from __future__ import annotations

from typing import Any

# ── EC2 Mocks ────────────────────────────────────────────────────────────

MOCK_INSTANCE_RUNNING: dict[str, Any] = {
    "instance_id": "i-0abc123def456789a",
    "instance_type": "t3.medium",
    "state": "running",
    "launch_time": "2026-03-01T08:00:00+00:00",
    "public_ip": "54.123.45.67",
    "private_ip": "10.0.1.42",
    "name": "web-server-prod-1",
    "availability_zone": "us-east-1a",
    "vpc_id": "vpc-0abc123",
    "subnet_id": "subnet-0abc123",
    "security_groups": [
        {"id": "sg-0abc123", "name": "web-server-sg"},
    ],
    "iam_role": "arn:aws:iam::123456789012:instance-profile/WebServerRole",
    "tags": {
        "Name": "web-server-prod-1",
        "Environment": "production",
        "Team": "platform",
    },
    "ebs_volumes": [
        {"device": "/dev/xvda", "volume_id": "vol-0abc123", "status": "attached"},
    ],
}

MOCK_INSTANCE_LIST: dict[str, Any] = {
    "instances": [
        MOCK_INSTANCE_RUNNING,
        {
            "instance_id": "i-0def456abc789012b",
            "instance_type": "t3.small",
            "state": "running",
            "launch_time": "2026-02-28T12:00:00+00:00",
            "public_ip": "54.123.45.68",
            "private_ip": "10.0.1.43",
            "name": "web-server-prod-2",
            "availability_zone": "us-east-1b",
            "tags": {"Name": "web-server-prod-2", "Environment": "production"},
        },
    ],
    "count": 2,
}

MOCK_RESTART_RESPONSE: dict[str, Any] = {
    "instance_id": "i-0abc123def456789a",
    "action": "restart",
    "stop_response": [
        {
            "instance_id": "i-0abc123def456789a",
            "previous_state": "running",
            "current_state": "stopping",
        }
    ],
    "start_response": [
        {
            "instance_id": "i-0abc123def456789a",
            "previous_state": "stopped",
            "current_state": "pending",
        }
    ],
}


# ── CloudWatch Mocks ────────────────────────────────────────────────────

MOCK_CPU_METRICS: dict[str, Any] = {
    "instance_id": "i-0abc123def456789a",
    "metric": "CPUUtilization",
    "period_seconds": 300,
    "time_range_minutes": 60,
    "datapoints": [
        {"timestamp": "2026-03-07T09:00:00+00:00", "average": 42.15, "maximum": 58.30, "minimum": 28.40},
        {"timestamp": "2026-03-07T09:05:00+00:00", "average": 45.80, "maximum": 62.10, "minimum": 30.20},
        {"timestamp": "2026-03-07T09:10:00+00:00", "average": 78.45, "maximum": 89.60, "minimum": 65.30},
        {"timestamp": "2026-03-07T09:15:00+00:00", "average": 92.35, "maximum": 97.80, "minimum": 85.10},
        {"timestamp": "2026-03-07T09:20:00+00:00", "average": 88.70, "maximum": 95.20, "minimum": 80.40},
    ],
    "summary": {
        "average": 69.49,
        "peak": 97.80,
        "latest": 88.70,
        "datapoint_count": 5,
    },
}


# ── Teams Mocks ──────────────────────────────────────────────────────────

MOCK_TEAMS_SUCCESS: dict[str, Any] = {
    "tool": "send_teams_message",
    "ok": True,
    "status_code": 200,
}

MOCK_INCIDENT_NOTIFICATION: dict[str, Any] = {
    "tool": "create_incident_notification",
    "severity": "CRITICAL",
    "ok": True,
    "status_code": 200,
}
