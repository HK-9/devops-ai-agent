"""Quick check: verify all runtimes have the updated auth config."""
import boto3

ac = boto3.client("bedrock-agentcore-control", region_name="ap-southeast-2")
for rt in ["mcp_server-4kjU5oAHWM", "monitoring_server-CI86d62MYP", "teams_server-hbrhm38Ef3", "sns_server-2f8klN8rTF"]:
    r = ac.get_agent_runtime(agentRuntimeId=rt)
    jwt = r.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {})
    print(f"{rt}:")
    print(f"  status: {r['status']}")
    print(f"  allowedClients: {jwt.get('allowedClients', [])}")
    print(f"  allowedAudience: {jwt.get('allowedAudience', [])}")

    # Also check token: get the actual token being used by the OAuth provider
    print()
