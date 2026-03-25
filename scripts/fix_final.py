"""
Fix: Add the ACTUAL OAuth provider client ID to runtimes, then re-register.
The OAuth provider uses 3oner8qr0qcquviof4vo9hidfm but runtimes only had 3i9cn8o8huu4rlpo8e46tr51ap.
"""
import time
import urllib.parse

import boto3

REGION = "ap-southeast-2"
USER_POOL_ID = "ap-southeast-2_OmD4OzAYI"
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"
DISCOVERY_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/openid-configuration"
DATA_PLANE_HOST = f"https://bedrock-agentcore.{REGION}.amazonaws.com"
OAUTH_PROVIDER_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"

# ALL client IDs that runtimes must accept
ALL_CLIENTS = [
    "5blfslp7o1shoe86g2o0ltbnm8",    # original agentcore CLI client
    "3i9cn8o8huu4rlpo8e46tr51ap",     # gateway client attempt 1
    "3oner8qr0qcquviof4vo9hidfm",     # gateway client attempt 2 (ACTUAL one used by OAuth provider)
]

RUNTIMES = [
    {
        "id": "mcp_server-4kjU5oAHWM",
        "name": "aws-infra",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM",
        "description": "EC2 infrastructure tools: list, describe, restart instances",
    },
    {
        "id": "monitoring_server-CI86d62MYP",
        "name": "monitoring",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/monitoring_server-CI86d62MYP",
        "description": "CloudWatch metrics: CPU, memory, disk usage",
    },
    {
        "id": "teams_server-hbrhm38Ef3",
        "name": "teams",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/teams_server-hbrhm38Ef3",
        "description": "Microsoft Teams notifications: messages and incident cards",
    },
    {
        "id": "sns_server-2f8klN8rTF",
        "name": "sns",
        "arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/sns_server-2f8klN8rTF",
        "description": "Alert failover: Teams primary, SNS fallback",
    },
]


def wait_for_target_ready(client, target_id, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        resp = client.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=target_id)
        status = resp["status"]
        reasons = resp.get("statusReasons", [])
        print(f"    {status}")
        if status == "READY":
            return "READY"
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL"):
            print(f"    reasons: {reasons}")
            return "FAILED"
        time.sleep(5)
    return "TIMEOUT"


def main():
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    # ─── Step 1: Update runtimes with ALL client IDs + force redeploy ────
    print("=" * 60)
    print("Step 1: Updating runtimes with correct client IDs + redeploy")
    print("=" * 60)

    new_auth = {
        "customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY_URL,
            "allowedAudience": ALL_CLIENTS,
            "allowedClients": ALL_CLIENTS,
        }
    }

    for rt in RUNTIMES:
        print(f"\n  {rt['name']} ({rt['id']})")
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
        print(f"    Updated with ts={ts}")

    # Wait for all READY
    print("\n  Waiting for all runtimes READY...")
    for attempt in range(40):
        time.sleep(5)
        all_ready = True
        statuses = []
        for rt in RUNTIMES:
            r = ac.get_agent_runtime(agentRuntimeId=rt["id"])
            statuses.append(f"{rt['name']}={r['status']}")
            if r["status"] != "READY":
                all_ready = False
        if attempt % 3 == 0:
            print(f"    {', '.join(statuses)}")
        if all_ready:
            print(f"    All READY!")
            break

    # Verify
    for rt in RUNTIMES:
        r = ac.get_agent_runtime(agentRuntimeId=rt["id"])
        clients = r.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {}).get("allowedClients", [])
        print(f"    {rt['name']}: allowedClients={clients}")

    # ─── Step 2: Delete any failed targets ───────────────────────────
    print(f"\n{'=' * 60}")
    print("Step 2: Cleaning up failed targets")
    existing_targets = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    for item in existing_targets.get("items", []):
        if item["status"] in ("FAILED", "CREATING"):
            print(f"  Deleting: {item['name']} ({item['targetId']})")
            ac.delete_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=item["targetId"])
    time.sleep(10)

    # ─── Step 3: Register targets ────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("Step 3: Registering gateway targets")
    print("=" * 60)

    scope = "mcp-tools/invoke"
    results = []

    for rt in RUNTIMES:
        print(f"\n  {rt['name']}")
        encoded = urllib.parse.quote(rt["arn"], safe="")
        endpoint = f"{DATA_PLANE_HOST}/runtimes/{encoded}/invocations"

        try:
            resp = ac.create_gateway_target(
                gatewayIdentifier=GATEWAY_ID,
                name=rt["name"],
                description=rt["description"],
                targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}},
                credentialProviderConfigurations=[{
                    "credentialProviderType": "OAUTH",
                    "credentialProvider": {
                        "oauthCredentialProvider": {
                            "providerArn": OAUTH_PROVIDER_ARN,
                            "scopes": [scope],
                            "grantType": "CLIENT_CREDENTIALS",
                        }
                    },
                }],
            )
            tid = resp["targetId"]
            print(f"    Created: {tid}")
            status = wait_for_target_ready(ac, tid)
            results.append({"name": rt["name"], "id": tid, "status": status})
        except Exception as e:
            print(f"    ERROR: {e}")
            results.append({"name": rt["name"], "id": None, "status": str(e)})

    # ─── Summary ─────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    all_ok = True
    for r in results:
        ok = r["status"] == "READY"
        print(f"  [{'OK' if ok else 'FAIL'}] {r['name']}: {r['status']}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\nAll targets registered! Syncing gateway...")
        ac.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        time.sleep(10)
        gw = ac.get_gateway(gatewayIdentifier=GATEWAY_ID)
        print(f"  Gateway status: {gw['status']}")
        print(f"\nGateway URL: https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")


if __name__ == "__main__":
    main()
