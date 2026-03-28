"""
Create a new AgentCore Gateway with AWS_IAM auth and migrate targets from old gateway.

The authorizerType cannot be changed on an existing gateway, so we create a
new one and re-register all targets from the NONE-auth gateway.

Usage:
    python scripts/update_gateway_auth.py
"""

import boto3
import time

REGION = "ap-southeast-2"
ACCOUNT = "650251690796"
OLD_GATEWAY_ID = "devopsagentgatewayv2-hvvsllrsvw"
NEW_GATEWAY_NAME = "DevOpsAgentGatewayV3"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/AgentCoreGatewayExecutionRole"
OAUTH_PROVIDER = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"

# Existing runtime targets
TARGETS = {
    "aws-infra-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/mcp_server-4kjU5oAHWM",
    "monitoring-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/monitoring_server-CI86d62MYP",
    "sns-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/sns_server-2f8klN8rTF",
    "teams-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/teams_server-hbrhm38Ef3",
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
            raise RuntimeError("Gateway entered FAILED state")
        time.sleep(10)
    raise TimeoutError(f"Gateway did not reach {target_status} within {timeout}s")


def main():
    # Step 1: Create new gateway with AWS_IAM auth
    print(f"Creating new gateway '{NEW_GATEWAY_NAME}' with AWS_IAM auth...")
    resp = ac.create_gateway(
        name=NEW_GATEWAY_NAME,
        roleArn=ROLE_ARN,
        protocolType="MCP",
        authorizerType="AWS_IAM",
    )
    gw_id = resp["gatewayId"]
    print(f"New Gateway ID: {gw_id}")

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
    print(f"New Gateway ID:  {gw_id}")
    print(f"New Gateway URL: {gateway_url}")
    print(f"Auth:            AWS_IAM")
    print(f"Status:          {gw['status']}")
    print(f"\nOld gateway ({OLD_GATEWAY_ID}) is still running with NONE auth.")
    print("Delete it manually once the new gateway is verified:")
    print(f"  ac.delete_gateway(gatewayIdentifier='{OLD_GATEWAY_ID}')")
    print(f"\nUpdate GATEWAY_URL in your agent config to:")
    print(f"  {gateway_url}")


if __name__ == "__main__":
    main()
