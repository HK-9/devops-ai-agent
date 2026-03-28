"""Register MCP server targets on the new AWS_IAM gateway."""
import boto3
import time
from urllib.parse import quote

REGION = "ap-southeast-2"
ACCOUNT = "650251690796"
NEW_GW = "devopsagentgatewayv3-ar4lmz2x6t"
OAUTH_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"

TARGETS = {
    "aws-infra-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/mcp_server-4kjU5oAHWM",
    "monitoring-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/monitoring_server-CI86d62MYP",
    "sns-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/sns_server-2f8klN8rTF",
    "teams-target": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/teams_server-hbrhm38Ef3",
}

ac = boto3.client("bedrock-agentcore-control", region_name=REGION)

for name, runtime_arn in TARGETS.items():
    endpoint = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{quote(runtime_arn, safe='')}/invocations"
    print(f"Registering {name}...")
    try:
        ac.create_gateway_target(
            gatewayIdentifier=NEW_GW,
            name=name,
            targetConfiguration={
                "mcp": {
                    "mcpServer": {
                        "endpoint": endpoint,
                    }
                }
            },
            credentialProviderConfigurations=[{
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": OAUTH_ARN,
                        "scopes": ["mcp-tools/invoke"],
                        "grantType": "CLIENT_CREDENTIALS",
                    }
                }
            }],
        )
        print(f"  OK: {name}")
    except Exception as e:
        print(f"  Error: {e}")
    time.sleep(2)

# Sync targets
print("\nSyncing targets...")
time.sleep(5)
targets = ac.list_gateway_targets(gatewayIdentifier=NEW_GW)
for t in targets.get("items", []):
    tid = t["targetId"]
    tname = t["name"]
    print(f"  Syncing {tname} ({tid})...")
    ac.synchronize_gateway_targets(gatewayIdentifier=NEW_GW, targetIdList=[tid])
    time.sleep(3)

print("\nWaiting 15s for settle...")
time.sleep(15)
targets = ac.list_gateway_targets(gatewayIdentifier=NEW_GW)
for t in targets.get("items", []):
    print(f"  {t['name']}: {t['status']}")
