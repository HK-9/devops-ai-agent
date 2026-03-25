"""Check ALL log streams with full raw messages for aws-infra."""
import boto3
import json

REGION = "ap-southeast-2"
logs = boto3.client("logs", region_name=REGION)

# Check both aws-infra and monitoring log groups
for lg_name in [
    "/aws/bedrock-agentcore/runtimes/mcp_server-4kjU5oAHWM-DEFAULT",
    "/aws/bedrock-agentcore/runtimes/monitoring_server-CI86d62MYP-DEFAULT",
]:
    print(f"\n{'#'*70}")
    print(f"Log Group: {lg_name}")
    print(f"{'#'*70}")

    resp = logs.describe_log_streams(
        logGroupName=lg_name,
        orderBy="LastEventTime",
        descending=True,
        limit=5,
    )

    for stream in resp.get("logStreams", []):
        name = stream["logStreamName"]
        print(f"\n  Stream: {name}")
        events = logs.get_log_events(
            logGroupName=lg_name,
            logStreamName=name,
            limit=10,
            startFromHead=False,
        )
        for e in events.get("events", []):
            msg = e["message"].strip()
            if not msg:
                continue
            try:
                j = json.loads(msg)
                # Look for the actual log body
                body = j.get("body", {})
                if isinstance(body, dict):
                    text = body.get("string_value", "")
                elif isinstance(body, str):
                    text = body
                else:
                    text = ""

                # Also check for error/severity
                severity = j.get("severityText", "")
                if text:
                    print(f"    [{severity}] {text[:200]}")
                else:
                    # Just print first 200 of raw
                    print(f"    [RAW] {msg[:200]}")
            except json.JSONDecodeError:
                print(f"    {msg[:200]}")

# Also filter for ERROR severity
print(f"\n\n{'#'*70}")
print("Searching for ERRORS in aws-infra logs...")
print(f"{'#'*70}")
try:
    resp = logs.filter_log_events(
        logGroupName="/aws/bedrock-agentcore/runtimes/mcp_server-4kjU5oAHWM-DEFAULT",
        filterPattern="ERROR",
        limit=10,
    )
    for e in resp.get("events", []):
        print(f"  {e['message'][:300]}")
    if not resp.get("events"):
        print("  No ERROR events found")
except Exception as ex:
    print(f"  Error: {ex}")

# Search for "aws-infra" or "Starting AWS"
print(f"\n\n{'#'*70}")
print("Searching for 'aws-infra' keyword...")
print(f"{'#'*70}")
try:
    resp = logs.filter_log_events(
        logGroupName="/aws/bedrock-agentcore/runtimes/mcp_server-4kjU5oAHWM-DEFAULT",
        filterPattern="aws-infra",
        limit=10,
    )
    for e in resp.get("events", []):
        print(f"  {e['message'][:300]}")
    if not resp.get("events"):
        print("  No 'aws-infra' events found")
except Exception as ex:
    print(f"  Error: {ex}")
