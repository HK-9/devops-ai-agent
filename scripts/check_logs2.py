"""Check CloudWatch logs for aws-infra runtime."""
import boto3

REGION = "ap-southeast-2"
logs = boto3.client("logs", region_name=REGION)

log_group = "/aws/bedrock-agentcore/runtimes/mcp_server-4kjU5oAHWM-DEFAULT"

# Get recent streams
resp = logs.describe_log_streams(
    logGroupName=log_group,
    orderBy="LastEventTime",
    descending=True,
    limit=3,
)

for stream in resp.get("logStreams", []):
    name = stream["logStreamName"]
    print(f"\n=== Stream: {name} ===")
    events = logs.get_log_events(
        logGroupName=log_group,
        logStreamName=name,
        limit=50,
        startFromHead=False,
    )
    for e in events.get("events", []):
        msg = e["message"].strip()
        if msg:
            print(msg[:300])
