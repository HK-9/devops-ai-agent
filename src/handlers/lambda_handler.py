"""
AWS Lambda handler — EventBridge to DevOps Agent via AgentCore Runtime.

Includes DynamoDB-based deduplication to prevent the same alarm from
triggering multiple agent invocations (and multiple emails).
"""

import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
AGENT_RUNTIME_ARN = os.environ.get(
    "AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy"
)
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
APPROVALS_TABLE = os.environ.get("APPROVALS_TABLE", "devops-agent-approvals")

# Deduplication window — skip if the same alarm was processed within this period
DEDUP_WINDOW = int(os.environ.get("DEDUP_WINDOW_SECONDS", "600"))  # 10 minutes

# Computed once at cold-start from AGENT_RUNTIME_ARN env var.
# If the ARN changes (e.g. CDK redeploy), a new Lambda cold start will pick it up.
_encoded_arn = quote(AGENT_RUNTIME_ARN, safe="")
AGENTCORE_INVOKE_URL = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{_encoded_arn}/invocations"

_sns = boto3.client("sns", region_name=REGION)
_ddb = boto3.resource("dynamodb", region_name=REGION)
_dedup_table = _ddb.Table(APPROVALS_TABLE)


# ── Alarm parsing ────────────────────────────────────────────────────────


def _parse_alarm(event):
    detail = event.get("detail", {})
    config = detail.get("configuration", {})
    metrics = config.get("metrics", [{}])
    metric_stat = metrics[0].get("metricStat", {}) if metrics else {}
    metric = metric_stat.get("metric", {})
    dimensions = metric.get("dimensions", {})
    state = detail.get("state", {})
    return {
        "alarm_name": detail.get("alarmName", ""),
        "state": state.get("value", "UNKNOWN"),
        "reason": state.get("reason", ""),
        "timestamp": state.get("timestamp", event.get("time", "")),
        "region": event.get("region", ""),
        "instance_id": dimensions.get("InstanceId", ""),
    }


def _detect_alarm_type(alarm_name):
    name_lower = alarm_name.lower()
    if "cpu" in name_lower:
        return "cpu"
    elif "mem" in name_lower:
        return "memory"
    elif "disk" in name_lower:
        return "disk"
    return "unknown"


# ── Deduplication ────────────────────────────────────────────────────────


def _is_duplicate_alarm(alarm_name):
    """Check if this alarm was already processed within DEDUP_WINDOW seconds.

    Uses the existing approvals DynamoDB table with a key prefix of
    ``alarm_dedup:`` to avoid collisions with real approval records.
    """
    dedup_key = f"alarm_dedup:{alarm_name}"
    try:
        resp = _dedup_table.get_item(Key={"approval_id": dedup_key})
        item = resp.get("Item")
        if item:
            processed_at = int(item.get("processed_at", 0))
            age = int(time.time()) - processed_at
            if age < DEDUP_WINDOW:
                print(f"DEDUP: {alarm_name} was processed {age}s ago (window={DEDUP_WINDOW}s), skipping")
                return True
    except Exception as e:
        # If dedup check fails, proceed — better to process than miss an alarm
        print(f"DEDUP check failed (proceeding anyway): {e}")
    return False


def _mark_alarm_processed(alarm_name):
    """Record that this alarm has been processed.

    The TTL attribute auto-deletes the record after 1 hour so the table
    doesn't accumulate stale dedup entries.
    """
    dedup_key = f"alarm_dedup:{alarm_name}"
    try:
        _dedup_table.put_item(
            Item={
                "approval_id": dedup_key,
                "processed_at": int(time.time()),
                "ttl": int(time.time()) + 3600,  # Auto-delete after 1 hour
            }
        )
    except Exception as e:
        print(f"DEDUP mark failed: {e}")


# ── Agent invocation ─────────────────────────────────────────────────────


def _invoke_agentcore(prompt):
    # Fetch fresh credentials on every invocation to avoid expiry in warm Lambdas
    credentials = boto3.Session(region_name=REGION).get_credentials().get_frozen_credentials()

    payload = json.dumps({"prompt": prompt}).encode("utf-8")
    request = AWSRequest(method="POST", url=AGENTCORE_INVOKE_URL, data=payload)
    request.headers["Content-Type"] = "application/json"
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(request)

    http_request = urllib.request.Request(
        url=AGENTCORE_INVOKE_URL,
        data=payload,
        headers=dict(request.headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=300) as resp:
            body = resp.read().decode("utf-8")
            print(f"AgentCore response: status={resp.status}, len={len(body)}")
            return {"status": resp.status, "response": body, "error": False}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8") if e.fp else str(e)
        print(f"AgentCore HTTP error: {e.code} - {err[:500]}")
        return {"status": e.code, "response": err, "error": True, "message": err}
    except Exception as e:
        print(f"AgentCore failed: {e}")
        return {"status": 500, "response": "", "error": True, "message": str(e)}


# ── Handler ──────────────────────────────────────────────────────────────


def handler(event, context):
    print(f"Lambda invoked: {json.dumps(event, default=str)[:500]}")

    alarm = _parse_alarm(event)
    print(f"Parsed: name={alarm['alarm_name']} instance={alarm['instance_id']} state={alarm['state']}")

    # Only process ALARM state — ignore OK / INSUFFICIENT_DATA transitions
    if alarm["state"] != "ALARM":
        print(f"Ignoring non-ALARM state: {alarm['state']}")
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "ignored", "state": alarm["state"]}),
        }

    # Deduplication — skip if this alarm was already processed recently
    if _is_duplicate_alarm(alarm["alarm_name"]):
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "deduplicated", "alarm": alarm["alarm_name"]}),
        }

    # Mark as processing BEFORE invoking agent to prevent parallel races
    _mark_alarm_processed(alarm["alarm_name"])

    alarm_type = _detect_alarm_type(alarm["alarm_name"])

    prompt = (
        f"A CloudWatch alarm fired for instance {alarm['instance_id']}.\n"
        f"Alarm: {alarm['alarm_name']}. Reason: {alarm['reason']}. "
        f"State: {alarm['state']}. Timestamp: {alarm['timestamp']}.\n"
        f"Investigate this alarm and take appropriate action following your runbook."
    )

    print(f"Invoking AgentCore: {AGENTCORE_INVOKE_URL[:80]}")
    result = _invoke_agentcore(prompt)

    if result.get("error"):
        return {
            "statusCode": result.get("status", 500),
            "body": json.dumps({"error": result.get("message")}),
        }

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "alarm_name": alarm["alarm_name"],
                "agent_response": result.get("response", "")[:2000],
            }
        ),
    }
