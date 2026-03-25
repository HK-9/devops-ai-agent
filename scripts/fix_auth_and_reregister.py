"""
Fix runtime authorizer configs to accept the gateway OAuth client,
then delete failed targets and re-register them.
"""

import time
import urllib.parse

import boto3

REGION = "ap-southeast-2"
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"
USER_POOL_ID = "ap-southeast-2_OmD4OzAYI"
ORIGINAL_CLIENT_ID = "5blfslp7o1shoe86g2o0ltbnm8"
GATEWAY_OAUTH_CLIENT_ID = "3i9cn8o8huu4rlpo8e46tr51ap"
DISCOVERY_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/openid-configuration"
OAUTH_PROVIDER_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:token-vault/default/oauth2credentialprovider/devops-gateway-cognito-oauth"
DATA_PLANE_HOST = f"https://bedrock-agentcore.{REGION}.amazonaws.com"

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


def wait_for_runtime_ready(client, runtime_id: str, timeout: int = 120):
    start = time.time()
    while time.time() - start < timeout:
        r = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = r["status"]
        print(f"    {runtime_id}: {status}")
        if status == "READY":
            return
        if status == "FAILED":
            print(f"    ERROR: runtime is FAILED")
            return
        time.sleep(5)


def wait_for_target_ready(client, gateway_id: str, target_id: str, timeout: int = 120) -> str:
    start = time.time()
    while time.time() - start < timeout:
        resp = client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = resp["status"]
        reasons = resp.get("statusReasons", [])
        print(f"    status: {status}")
        if status == "READY":
            return status
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL"):
            print(f"    reasons: {reasons}")
            return status
        time.sleep(5)
    return "TIMEOUT"


def main():
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # Both client IDs that runtimes should accept
    allowed_clients = [ORIGINAL_CLIENT_ID, GATEWAY_OAUTH_CLIENT_ID]
    allowed_audience = [ORIGINAL_CLIENT_ID, GATEWAY_OAUTH_CLIENT_ID]

    # ── Step 1: Update runtime authorizer configs ────────────────────
    print("=" * 60)
    print("Step 1: Updating runtime authorizer configs")
    print("=" * 60)

    for rt in RUNTIMES:
        print(f"\n  Updating {rt['name']} ({rt['id']})...")
        try:
            ac.update_agent_runtime(
                agentRuntimeId=rt["id"],
                authorizerConfiguration={
                    "customJWTAuthorizer": {
                        "discoveryUrl": DISCOVERY_URL,
                        "allowedAudience": allowed_audience,
                        "allowedClients": allowed_clients,
                    }
                },
            )
            print(f"    Updated — waiting for READY...")
            wait_for_runtime_ready(ac, rt["id"])
        except Exception as e:
            print(f"    ERROR: {e}")

    # ── Step 2: Delete the failed targets ────────────────────────────
    print(f"\n{'=' * 60}")
    print("Step 2: Deleting failed targets")
    print("=" * 60)

    existing = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    for item in existing.get("items", []):
        if item["status"] == "FAILED":
            print(f"  Deleting failed target: {item['name']} (ID: {item['targetId']})")
            try:
                ac.delete_gateway_target(
                    gatewayIdentifier=GATEWAY_ID,
                    targetId=item["targetId"],
                )
                print(f"    Deleted.")
            except Exception as e:
                print(f"    Delete error: {e}")

    # Wait a bit for deletes to propagate
    print("  Waiting 10s for deletes to propagate...")
    time.sleep(10)

    # ── Step 3: Re-register targets ──────────────────────────────────
    print(f"\n{'=' * 60}")
    print("Step 3: Registering targets with OAuth credentials")
    print("=" * 60)

    scope = "mcp-tools/invoke"
    created = []

    for rt in RUNTIMES:
        print(f"\n  Registering: {rt['name']}")
        encoded_arn = urllib.parse.quote(rt["arn"], safe="")
        mcp_endpoint = f"{DATA_PLANE_HOST}/runtimes/{encoded_arn}/invocations"
        print(f"    Endpoint: {mcp_endpoint}")

        try:
            resp = ac.create_gateway_target(
                gatewayIdentifier=GATEWAY_ID,
                name=rt["name"],
                description=rt["description"],
                targetConfiguration={
                    "mcp": {
                        "mcpServer": {
                            "endpoint": mcp_endpoint,
                        }
                    }
                },
                credentialProviderConfigurations=[
                    {
                        "credentialProviderType": "OAUTH",
                        "credentialProvider": {
                            "oauthCredentialProvider": {
                                "providerArn": OAUTH_PROVIDER_ARN,
                                "scopes": [scope],
                                "grantType": "CLIENT_CREDENTIALS",
                            }
                        },
                    }
                ],
            )
            target_id = resp["targetId"]
            print(f"    Created ID: {target_id} — waiting...")
            status = wait_for_target_ready(ac, GATEWAY_ID, target_id)
            created.append({"name": rt["name"], "id": target_id, "status": status})
        except Exception as e:
            print(f"    FAILED: {e}")
            created.append({"name": rt["name"], "id": None, "status": f"ERROR: {e}"})

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print("=" * 60)
    all_ok = True
    for c in created:
        ok = c["status"] == "READY"
        print(f"  [{'OK' if ok else 'FAIL'}] {c['name']}: {c['status']} (ID: {c['id']})")
        if not ok:
            all_ok = False

    if all_ok:
        print(f"\nAll targets registered! Syncing gateway...")
        try:
            ac.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID)
            time.sleep(10)
            gw = ac.get_gateway(gatewayIdentifier=GATEWAY_ID)
            print(f"  Gateway status: {gw['status']}")
        except Exception as e:
            print(f"  Sync: {e}")
        print(f"\nGateway URL: https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")
        print("Done!")
    else:
        print("\nSome targets failed — check errors above.")


if __name__ == "__main__":
    main()
