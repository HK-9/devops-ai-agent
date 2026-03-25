"""Test direct invocation of the aws-infra runtime to diagnose the timeout."""
import boto3
import json
import urllib.parse

REGION = "ap-southeast-2"
RT_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM"

# Try data plane invocation
dp = boto3.client("bedrock-agentcore", region_name=REGION)

# Simple MCP initialize request
mcp_request = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
    "id": "1",
}

print("Testing direct invocation of aws-infra runtime...")
try:
    resp = dp.invoke_runtime(
        agentRuntimeArn=RT_ARN,
        payload=json.dumps(mcp_request),
    )
    print(f"Status: {resp['ResponseMetadata']['HTTPStatusCode']}")
    body = resp.get("body")
    if body:
        content = body.read().decode()
        print(f"Response: {content[:500]}")
    else:
        print("No body")
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")

# Also try tools/list
print("\n\nTesting tools/list...")
mcp_tools = {
    "jsonrpc": "2.0",
    "method": "tools/list",
    "params": {},
    "id": "2",
}
try:
    resp = dp.invoke_runtime(
        agentRuntimeArn=RT_ARN,
        payload=json.dumps(mcp_tools),
    )
    print(f"Status: {resp['ResponseMetadata']['HTTPStatusCode']}")
    body = resp.get("body")
    if body:
        content = body.read().decode()
        print(f"Response: {content[:1000]}")
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")
