import boto3
import time
import urllib.parse

REGION = "ap-southeast-2"
ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"
OAUTH_PROVIDER_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"
RT_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM"

# Check runtime status
r = ac.get_agent_runtime(agentRuntimeId="mcp_server-4kjU5oAHWM")
print(f"Runtime status: {r['status']}")

# Delete failed target
ac.delete_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId="BNZ3IICHDB")
print("Deleted failed target")
time.sleep(15)

# Re-register
encoded = urllib.parse.quote(RT_ARN, safe="")
endpoint = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded}/invocations"
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

for i in range(40):
    time.sleep(5)
    r = ac.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=tid)
    st = r["status"]
    if i % 3 == 0:
        print(f"  {st}")
    if st == "READY":
        print("READY!")
        break
    elif st in ("FAILED", "UPDATE_UNSUCCESSFUL"):
        reasons = r.get("statusReasons", [])
        print(f"FAILED: {reasons}")
        break
