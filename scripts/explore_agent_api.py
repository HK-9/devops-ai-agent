"""Explore all Bedrock Agent APIs for MCP gateway integration."""
import boto3

ba = boto3.client("bedrock-agent", region_name="ap-southeast-2")

# List existing agents
resp = ba.list_agents(maxResults=10)
for agent in resp.get("agentSummaries", []):
    name = agent["agentName"]
    aid = agent["agentId"]
    status = agent["agentStatus"]
    print(f"  {name}: {aid} status={status}")
if not resp.get("agentSummaries"):
    print("No managed agents found")

# Check if there's an UpdateAgent with MCP gateway support
model = ba._service_model.operation_model("UpdateAgent")
print("\nUpdateAgent params:")
for name, shape in model.input_shape.members.items():
    print(f"  {name}: {shape.type_name}")

# Look at the hosted agent we deployed earlier
ac = boto3.client("bedrock-agentcore-control", region_name="ap-southeast-2")
resp = ac.get_agent_runtime(agentRuntimeId="hosted_agent_0sl4v-9BnXYZC6E7")
print("\n\nExisting hosted_agent runtime:")
for k in ["agentRuntimeName", "agentRuntimeId", "status", "protocolConfiguration"]:
    print(f"  {k}: {resp.get(k)}")
print(f"  artifact: {resp.get('agentRuntimeArtifact', {})}")
