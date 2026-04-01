
"""
AWS Lambda handler — EventBridge to DevOps Agent via AgentCore Runtime.
"""
import json
import os
from urllib.parse import quote
import urllib.request
import urllib.error

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
AGENT_RUNTIME_ARN = os.environ.get(
    "AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy"
)
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

_encoded_arn = quote(AGENT_RUNTIME_ARN, safe="")
AGENTCORE_INVOKE_URL = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{_encoded_arn}/invocations"

_session = boto3.Session(region_name=REGION)
_credentials = _session.get_credentials()
_sns = boto3.client("sns", region_name=REGION)


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
    if "cpu" in name_lower: return "cpu"
    elif "mem" in name_lower: return "memory"
    elif "disk" in name_lower: return "disk"
    return "unknown"


def _send_sns(subject, message):
    if SNS_TOPIC_ARN:
        try:
            _sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
            print(f"SNS alert sent: {subject}")
        except Exception as e:
            print(f"SNS failed: {e}")


def _invoke_agentcore(prompt):
    payload = json.dumps({"prompt": prompt}).encode("utf-8")
    request = AWSRequest(method="POST", url=AGENTCORE_INVOKE_URL, data=payload)
    request.headers["Content-Type"] = "application/json"
    SigV4Auth(_credentials, "bedrock-agentcore", REGION).add_auth(request)

    http_request = urllib.request.Request(
        url=AGENTCORE_INVOKE_URL, data=payload,
        headers=dict(request.headers), method="POST",
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


def handler(event, context):
    print(f"Lambda invoked: {json.dumps(event, default=str)[:500]}")

    alarm = _parse_alarm(event)
    print(f"Parsed: name={alarm['alarm_name']} instance={alarm['instance_id']} state={alarm['state']}")

    alarm_type = _detect_alarm_type(alarm["alarm_name"])
    _send_sns(
        subject=f"CPU Alert: {alarm['alarm_name']}",
        message=f"Instance: {alarm['instance_id']}\nAlarm: {alarm['alarm_name']}\nReason: {alarm['reason']}\n\nDevOps Agent is investigating..."
    )

    prompt = f"""A CloudWatch alarm fired for instance {alarm['instance_id']}. 
Alarm: {alarm['alarm_name']}. Reason: {alarm['reason']}. State: {alarm['state']}. Timestamp: {alarm['timestamp']}.
Investigate this alarm and take appropriate action following your runbook."""

    print(f"Invoking AgentCore: {AGENTCORE_INVOKE_URL[:80]}")
    result = _invoke_agentcore(prompt)

    if result.get("error"):
        return {"statusCode": result.get("status", 500), "body": json.dumps({"error": result.get("message")})}

    return {"statusCode": 200, "body": json.dumps({"alarm_name": alarm["alarm_name"], "agent_response": result.get("response", "")[:2000]})}
