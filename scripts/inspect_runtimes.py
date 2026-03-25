"""Inspect all 4 AgentCore Runtimes to discover their MCP endpoint URLs."""
import boto3

client = boto3.client("bedrock-agentcore-control", region_name="ap-southeast-2")

runtimes = [
    "mcp_server-4kjU5oAHWM",
    "monitoring_server-CI86d62MYP",
    "teams_server-hbrhm38Ef3",
    "sns_server-2f8klN8rTF",
]

for rt_id in runtimes:
    try:
        resp = client.get_agent_runtime(agentRuntimeId=rt_id)
        print(f"=== {rt_id} ===")
        print(f"  status: {resp.get('status')}")
        print(f"  agentRuntimeEndpoint: {resp.get('agentRuntimeEndpoint', 'N/A')}")
        # Dump all keys that look like endpoints/URLs
        for k, v in resp.items():
            if "endpoint" in k.lower() or "url" in k.lower() or "uri" in k.lower():
                print(f"  {k}: {v}")
        print()
    except Exception as e:
        print(f"  ERROR for {rt_id}: {e}")
        print()
