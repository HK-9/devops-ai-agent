"""Check CloudWatch logs for aws-infra runtime to diagnose the timeout."""
import boto3
import time

REGION = "ap-southeast-2"
logs = boto3.client("logs", region_name=REGION)

# Try common log group patterns for AgentCore runtimes
rt_id = "mcp_server-4kjU5oAHWM"
patterns = [
    f"/aws/bedrock-agentcore/runtime/{rt_id}",
    f"/aws/bedrock-agentcore/{rt_id}",
    f"bedrock-agentcore-{rt_id}",
    f"/bedrock-agentcore/runtime/{rt_id}",
]

# Also list log groups matching bedrock-agentcore
print("Searching for log groups...")
paginator = logs.get_paginator("describe_log_groups")
for page in paginator.paginate(logGroupNamePrefix="/aws/bedrock"):
    for lg in page["logGroups"]:
        print(f"  {lg['logGroupName']}")

for page in paginator.paginate(logGroupNamePrefix="bedrock"):
    for lg in page["logGroups"]:
        print(f"  {lg['logGroupName']}")

# Try getting recent events for each pattern
for pattern in patterns:
    try:
        resp = logs.describe_log_streams(
            logGroupName=pattern,
            orderBy="LastEventTime",
            descending=True,
            limit=3,
        )
        streams = resp.get("logStreams", [])
        if streams:
            print(f"\nFound log group: {pattern}")
            for s in streams:
                name = s["logStreamName"]
                print(f"  Stream: {name}")
                events = logs.get_log_events(
                    logGroupName=pattern,
                    logStreamName=name,
                    limit=20,
                    startFromHead=False,
                )
                for e in events.get("events", []):
                    print(f"    {e['message'][:200]}")
    except logs.exceptions.ResourceNotFoundException:
        pass
    except Exception as e:
        print(f"  {pattern}: {e}")
