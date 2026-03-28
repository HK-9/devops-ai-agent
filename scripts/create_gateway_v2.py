"""
Create a new AgentCore Gateway with NONE auth and register existing MCP runtime targets.

Cognito CUSTOM_JWT is incompatible with the MCP protocol's OAuth flow (RFC 8707).
Using NONE auth for the gateway while MCP server runtimes retain their own JWT auth.
"""

import boto3
import time
import json

REGION = "ap-southeast-2"
ACCOUNT = "650251690796"
GATEWAY_NAME = "DevOpsAgentGatewayV2"
ROLE_ARN = "arn:aws:iam::650251690796:role/AgentCoreGatewayExecutionRole"
OAUTH_PROVIDER = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"

# Existing runtime targets
TARGETS = {
    "aws-infra-target": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM",
    "monitoring-target": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/monitoring_server-CI86d62MYP",
    "sns-target": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/sns_server-2f8klN8rTF",
    "teams-target": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/teams_server-hbrhm38Ef3",
}

ac = boto3.client("bedrock-agentcore-control", region_name=REGION)


def wait_for_gateway(gw_id, target_status="READY", timeout=300):
    """Wait for gateway to reach target status."""
    start = time.time()
    while time.time() - start < timeout:
        gw = ac.get_gateway(gatewayIdentifier=gw_id)
        status = gw["status"]
        print(f"  Gateway status: {status}")
        if status == target_status:
            return gw
        if status == "FAILED":
            raise RuntimeError(f"Gateway entered FAILED state")
        time.sleep(10)
    raise TimeoutError(f"Gateway did not reach {target_status} within {timeout}s")


def main():
    # Step 1: Create gateway with NONE auth
    print(f"Creating gateway '{GATEWAY_NAME}' with NONE auth...")
    resp = ac.create_gateway(
        name=GATEWAY_NAME,
        roleArn=ROLE_ARN,
        protocolType="MCP",
        authorizerType="NONE",
    )
    gw_id = resp["gatewayId"]
    print(f"Gateway ID: {gw_id}")

    print("Waiting for gateway to be READY...")
    wait_for_gateway(gw_id)

    # Step 2: Register targets
    for name, runtime_arn in TARGETS.items():
        print(f"\nRegistering target: {name}")
        try:
            ac.create_gateway_target(
                gatewayIdentifier=gw_id,
                name=name,
                targetConfiguration={
                    "mcpServer": {
                        "mcpServerRuntimeArn": runtime_arn,
                        "oauthCredentialProviderArn": OAUTH_PROVIDER,
                    }
                },
            )
            print(f"  Registered {name}")
        except Exception as e:
            print(f"  Error registering {name}: {e}")
        time.sleep(2)

    # Step 3: Sync targets
    print("\nSyncing gateway targets...")
    time.sleep(5)
    targets = ac.list_gateway_targets(gatewayIdentifier=gw_id)
    for t in targets.get("items", []):
        tid = t["targetId"]
        tname = t["name"]
        print(f"  Syncing {tname} ({tid})...")
        ac.synchronize_gateway_targets(gatewayIdentifier=gw_id, targetIdList=[tid])
        time.sleep(3)

    # Step 4: Wait for final state
    print("\nWaiting for gateway to settle...")
    time.sleep(15)
    gw = ac.get_gateway(gatewayIdentifier=gw_id)
    gateway_url = f"https://{gw_id}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"

    print(f"\n{'='*60}")
    print(f"Gateway ID:  {gw_id}")
    print(f"Gateway URL: {gateway_url}")
    print(f"Status:      {gw['status']}")

    targets = ac.list_gateway_targets(gatewayIdentifier=gw_id)
    for t in targets.get("items", []):
        print(f"  Target: {t['name']} — {t['status']}")

    print(f"\nUpdate GATEWAY_URL in deploy_agent/agent.py to:")
    print(f"  {gateway_url}")


if __name__ == "__main__":
    main()
