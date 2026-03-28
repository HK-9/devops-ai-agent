"""
AWS client helpers — AgentCore control plane, ECR login, CloudWatch logs.
"""

from __future__ import annotations

import json
import subprocess
import time

import boto3

from .config import REGION, ACCOUNT
from .console import log, Colors


def ac_client():
    """Return a bedrock-agentcore-control client."""
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def ecr_login() -> None:
    """Authenticate Docker to ECR."""
    log("\n[ECR] Authenticating Docker...", Colors.BOLD)
    pw = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", REGION],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin",
         f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"],
        input=pw.stdout, text=True, check=True,
        capture_output=True,
    )
    log("  Authenticated.", Colors.GREEN)


def tail_logs(runtime_id: str, minutes: int = 5) -> None:
    """Tail CloudWatch logs for a runtime, skipping health-check noise."""
    from .config import log_group as _lg
    logs_client = boto3.client("logs", region_name=REGION)
    group = _lg(runtime_id)
    end_time = int(time.time() * 1000)
    start_time = end_time - (minutes * 60 * 1000)

    log(f"\nTailing logs ({group}, last {minutes} min)\n", Colors.BOLD)

    try:
        events = logs_client.filter_log_events(
            logGroupName=group,
            startTime=start_time,
            endTime=end_time,
            limit=100,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        log("Log group not found. The runtime may not have started yet.", Colors.YELLOW)
        return

    count = 0
    for evt in events.get("events", []):
        msg = evt["message"]
        try:
            data = json.loads(msg)
            body = data.get("body", "")
            sev = data.get("severityText", "")
        except (json.JSONDecodeError, TypeError):
            body = msg
            sev = ""

        body = str(body)
        if "GET /ping" in body:
            continue

        ts = time.strftime("%H:%M:%S", time.localtime(evt["timestamp"] / 1000))
        print(f"[{ts}] {sev:5s} {body[:500]}")
        count += 1

    if count == 0:
        log("(no non-healthcheck log entries found)", Colors.YELLOW)
