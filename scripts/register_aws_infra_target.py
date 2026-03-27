"""Delete failed aws-infra target, re-register, and sync gateway."""
import boto3
import time
import urllib.parse

REGION = "ap-southeast-2"
ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"
OAUTH_PROVIDER_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"
RT_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM"

# Step 1: Check runtime is READY
r = ac.get_agent_runtime(agentRuntimeId="mcp_server-4kjU5oAHWM")
print(f"Runtime status: {r['status']}")

# Step 2: List and delete any failed targets
print("\nCurrent targets:")
targets = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
for t in targets.get("items", []):
    print(f"  {t['name']}: {t['targetId']} -> {t['status']}")
    if t["status"] == "FAILED":
        print(f"    Deleting...")
        ac.delete_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=t["targetId"])

print("\nWaiting 15s for cleanup...")
time.sleep(15)

# Step 3: Register aws-infra target
encoded = urllib.parse.quote(RT_ARN, safe="")
endpoint = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded}/invocations"

print("Registering aws-infra target...")
resp = ac.create_gateway_target(
    gatewayIdentifier=GATEWAY_ID,
    name="aws-infra",
    description="EC2 infrastructure tools: list, describe, restart instances",
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
print(f"Created target: {tid}")

# Step 4: Wait for READY
for i in range(60):
    time.sleep(5)
    r = ac.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=tid)
    st = r["status"]
    if i % 3 == 0:
        print(f"  [{i*5}s] {st}")
    if st == "READY":
        print(f"  READY!")
        break
    elif st in ("FAILED", "UPDATE_UNSUCCESSFUL"):
        reasons = r.get("statusReasons", [])
        print(f"  FAILED: {reasons}")
        break

# Step 5: Final status of all targets
print("\n=== ALL TARGETS ===")
targets = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
all_ready = True
for t in targets.get("items", []):
    ok = t["status"] == "READY"
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {t['name']}: {t['status']}")
    if not ok:
        all_ready = False

if all_ready:
    print("\nAll 4 targets READY! Syncing gateway...")
    ac.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    time.sleep(10)
    gw = ac.get_gateway(gatewayIdentifier=GATEWAY_ID)
    print(f"Gateway status: {gw['status']}")
    print(f"\nGateway URL: https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")
