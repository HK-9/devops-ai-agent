"""Check all log streams for aws-infra runtime."""
import boto3
import json

REGION = "ap-southeast-2"
logs = boto3.client("logs", region_name=REGION)

log_group = "/aws/bedrock-agentcore/runtimes/mcp_server-4kjU5oAHWM-DEFAULT"

# List ALL streams
resp = logs.describe_log_streams(
    logGroupName=log_group,
    orderBy="LastEventTime",
    descending=True,
    limit=10,
)

for stream in resp.get("logStreams", []):
    name = stream["logStreamName"]
    print(f"\n{'='*60}")
    print(f"Stream: {name}")
    print(f"{'='*60}")
    events = logs.get_log_events(
        logGroupName=log_group,
        logStreamName=name,
        limit=30,
        startFromHead=False,
    )
    for e in events.get("events", []):
        msg = e["message"].strip()
        if not msg:
            continue
        # Try to parse JSON and extract just the message body
        try:
            j = json.loads(msg)
            body = j.get("body", {})
            if isinstance(body, dict):
                text = body.get("string_value") or body.get("message", "")
            elif isinstance(body, str):
                text = body
            else:
                text = msg[:200]
            if text:
                print(f"  {text[:300]}")
        except json.JSONDecodeError:
            print(f"  {msg[:300]}")
