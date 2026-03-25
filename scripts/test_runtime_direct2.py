"""Test direct invocation of the aws-infra runtime via data plane API."""
import boto3
import json

REGION = "ap-southeast-2"
RT_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM"
MON_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/monitoring_server-CI86d62MYP"

dp = boto3.client("bedrock-agentcore", region_name=REGION)

# MCP initialize message
init_msg = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
    "id": "1",
}

for label, arn in [("monitoring (working)", MON_ARN), ("aws-infra (failing)", RT_ARN)]:
    print(f"\n=== Testing {label} ===")
    try:
        resp = dp.invoke_agent_runtime(
            agentRuntimeArn=arn,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(init_msg).encode(),
        )
        print(f"Status: {resp['ResponseMetadata']['HTTPStatusCode']}")
        body = resp.get("payload")
        if body:
            content = body.read().decode()
            print(f"Response: {content[:500]}")
        else:
            print("No payload in response")
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
