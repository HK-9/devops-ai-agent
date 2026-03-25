import boto3
import time
import urllib.parse
import datetime

REGION = "ap-southeast-2"
ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"
OAUTH_PROVIDER_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"
ALL_CLIENTS = [
    "5blfslp7o1shoe86g2o0ltbnm8",
    "3i9cn8o8huu4rlpo8e46tr51ap",
    "3oner8qr0qcquviof4vo9hidfm",
]
DISCOVERY_URL = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_OmD4OzAYI/.well-known/openid-configuration"
DATA_PLANE_HOST = f"https://bedrock-agentcore.{REGION}.amazonaws.com"

RUNTIMES = [
    {
        "id": "mcp_server-4kjU5oAHWM",
        "name": "aws-infra",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM",
        "desc": "EC2 infrastructure tools",
    },
    {
        "id": "monitoring_server-CI86d62MYP",
        "name": "monitoring",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/monitoring_server-CI86d62MYP",
        "desc": "CloudWatch metrics tools",
    },
    {
        "id": "teams_server-hbrhm38Ef3",
        "name": "teams",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/teams_server-hbrhm38Ef3",
        "desc": "Teams notification tools",
    },
    {
        "id": "sns_server-2f8klN8rTF",
        "name": "sns",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/sns_server-2f8klN8rTF",
        "desc": "SNS alert tools",
    },
]

new_auth = {
    "customJWTAuthorizer": {
        "discoveryUrl": DISCOVERY_URL,
        "allowedClients": ALL_CLIENTS,
    }
}
ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

# Step 1: Update all runtimes (no allowedAudience)
print("=== Step 1: Update runtimes (no allowedAudience) ===")
for rt in RUNTIMES:
    existing = ac.get_agent_runtime(agentRuntimeId=rt["id"])
    params = {
        "agentRuntimeId": rt["id"],
        "agentRuntimeArtifact": existing["agentRuntimeArtifact"],
        "roleArn": existing["roleArn"],
        "networkConfiguration": existing["networkConfiguration"],
        "authorizerConfiguration": new_auth,
        "environmentVariables": {"REDEPLOY_TS": ts},
    }
    if existing.get("protocolConfiguration"):
        params["protocolConfiguration"] = existing["protocolConfiguration"]
    ac.update_agent_runtime(**params)
    name = rt["name"]
    print(f"  {name}: updated")

# Step 2: Wait for READY
print("\n=== Step 2: Wait for READY ===")
for attempt in range(40):
    time.sleep(5)
    statuses = {}
    for rt in RUNTIMES:
        r = ac.get_agent_runtime(agentRuntimeId=rt["id"])
        statuses[rt["name"]] = r["status"]
    if attempt % 3 == 0:
        print(f"  {statuses}")
    if all(v == "READY" for v in statuses.values()):
        print("  ALL READY!")
        break

# Verify no allowedAudience
for rt in RUNTIMES:
    r = ac.get_agent_runtime(agentRuntimeId=rt["id"])
    auth = r.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {})
    name = rt["name"]
    aud = auth.get("allowedAudience", "NOT_SET")
    clients = auth.get("allowedClients", [])
    print(f"  {name}: audience={aud}, clients={clients}")

# Step 3: Delete all existing targets (failed or otherwise)
print("\n=== Step 3: Cleaning targets ===")
targets = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
for t in targets.get("items", []):
    tid = t["targetId"]
    tname = t["name"]
    tstatus = t["status"]
    print(f"  Deleting {tname} ({tid}) status={tstatus}")
    try:
        ac.delete_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=tid)
    except Exception as e:
        print(f"    Error: {e}")
time.sleep(10)

# Step 4: Register fresh targets
print("\n=== Step 4: Register targets ===")
results = []
for rt in RUNTIMES:
    name = rt["name"]
    encoded = urllib.parse.quote(rt["arn"], safe="")
    endpoint = f"{DATA_PLANE_HOST}/runtimes/{encoded}/invocations"
    try:
        resp = ac.create_gateway_target(
            gatewayIdentifier=GATEWAY_ID,
            name=name,
            description=rt["desc"],
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
        print(f"  {name}: created {tid}, waiting...")
        final_status = "TIMEOUT"
        for _ in range(36):
            time.sleep(5)
            r = ac.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=tid)
            st = r["status"]
            if st == "READY":
                print(f"    READY!")
                final_status = "READY"
                break
            elif st in ("FAILED", "UPDATE_UNSUCCESSFUL"):
                reasons = r.get("statusReasons", [])
                print(f"    FAILED: {reasons}")
                final_status = f"FAILED: {reasons}"
                break
        results.append((name, final_status))
    except Exception as e:
        print(f"  {name}: ERROR {e}")
        results.append((name, str(e)))

print("\n=== SUMMARY ===")
all_ok = True
for name, status in results:
    ok = status == "READY"
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {name}: {status}")
    if not ok:
        all_ok = False

if all_ok:
    print("\nAll targets READY! Syncing gateway...")
    ac.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    time.sleep(10)
    gw = ac.get_gateway(gatewayIdentifier=GATEWAY_ID)
    gw_status = gw["status"]
    print(f"  Gateway: {gw_status}")
    print(f"\nGateway URL: https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")
