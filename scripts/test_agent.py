"""Test agent invocation to verify tool visibility."""
import boto3
import json
import uuid

ac = boto3.client("bedrock-agentcore", region_name="ap-southeast-2")
AGENT_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy"

session_id = str(uuid.uuid4()) + "-" + str(uuid.uuid4())

# Ask the agent to list its tools
resp = ac.invoke_agent_runtime(
    agentRuntimeArn=AGENT_ARN,
    runtimeSessionId=session_id,
    payload=json.dumps({
        "query": "What tools do you have available? Please list every single tool name."
    }).encode(),
)

# Read response
body = resp.get("response")
if hasattr(body, "read"):
    data = body.read().decode()
else:
    data = str(body)

print("Response:")
print(data)
print()
print(f"Status code: {resp.get('statusCode')}")
