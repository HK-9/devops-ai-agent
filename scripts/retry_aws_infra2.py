import boto3
import time
import urllib.parse

REGION = "ap-southeast-2"
ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"
OAUTH_PROVIDER_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"

# List all current targets
targets = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
for t in targets.get("items", []):
    name = t["name"]
    tid = t["targetId"]
    status = t["status"]
    print(f"{name}: {tid} -> {status}")

# Delete the failed aws-infra target
for t in targets.get("items", []):
    if t["status"] == "FAILED":
        print(f"\nDeleting failed: {t['name']} ({t['targetId']})")
        ac.delete_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=t["targetId"])

print("\nWaiting 15s for cleanup...")
time.sleep(15)

# Retry aws-infra
RT_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM"
encoded = urllib.parse.quote(RT_ARN, safe="")
endpoint = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded}/invocations"

print("\nRegistering aws-infra again...")
resp = ac.create_gateway_target(
    gatewayIdentifier=GATEWAY_ID,
    name="aws-infra",
    description="EC2 infrastructure tools",
    targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}},
    credentialProviderConfigurations=[
        {
            "credentialProviderType": "OAUTH",
            "credentialProvider": {
                "oauthCredentialProvider": {
                    "providerArn": OAUTH_PROVIDER_ARN,
                    "scopes": ["mcp-tools/invoke"],
                    "grantType": "CLIENT_CREDENTIALS",
                }
            },
        }
    ],
)
tid = resp["targetId"]
print(f"Created: {tid}")

# Wait with more patience
for i in range(60):
    time.sleep(5)
    r = ac.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=tid)
    st = r["status"]
    if i % 4 == 0:
        print(f"  [{i*5}s] {st}")
    if st == "READY":
        print(f"  READY!")
        # Sync gateway
        print("\nSyncing gateway...")
        ac.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        time.sleep(10)
        gw = ac.get_gateway(gatewayIdentifier=GATEWAY_ID)
        print(f"Gateway status: {gw['status']}")
        break
    elif st in ("FAILED", "UPDATE_UNSUCCESSFUL"):
        reasons = r.get("statusReasons", [])
        print(f"  FAILED: {reasons}")
        break
