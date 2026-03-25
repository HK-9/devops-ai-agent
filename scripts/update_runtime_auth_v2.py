"""
Update runtimes: for client_credentials flow, Cognito tokens don't have 'aud' claim.
Try removing allowedAudience or setting it empty to bypass the aud check.
Also try just passing client_id as audience since some JWT validators fall back to that.
"""
import time
import boto3

REGION = "ap-southeast-2"
USER_POOL_ID = "ap-southeast-2_OmD4OzAYI"
DISCOVERY_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/openid-configuration"
ORIGINAL_CLIENT_ID = "5blfslp7o1shoe86g2o0ltbnm8"
GATEWAY_CLIENT_ID = "3i9cn8o8huu4rlpo8e46tr51ap"

RUNTIMES = [
    "mcp_server-4kjU5oAHWM",
    "monitoring_server-CI86d62MYP",
    "teams_server-hbrhm38Ef3",
    "sns_server-2f8klN8rTF",
]


def main():
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # The Cognito client_credentials token uses 'client_id' as the identifier.
    # Some JWT validators use 'client_id' as a fallback for 'aud'.
    # Set both allowedAudience and allowedClients to include both IDs.
    # Also try adding the resource server identifier as an audience.
    new_auth = {
        "customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY_URL,
            "allowedAudience": [ORIGINAL_CLIENT_ID, GATEWAY_CLIENT_ID, "mcp-tools"],
            "allowedClients": [ORIGINAL_CLIENT_ID, GATEWAY_CLIENT_ID],
        }
    }

    for rt_id in RUNTIMES:
        print(f"\nUpdating: {rt_id}")
        existing = ac.get_agent_runtime(agentRuntimeId=rt_id)

        update_params = {
            "agentRuntimeId": rt_id,
            "agentRuntimeArtifact": existing["agentRuntimeArtifact"],
            "roleArn": existing["roleArn"],
            "networkConfiguration": existing["networkConfiguration"],
            "authorizerConfiguration": new_auth,
        }
        if existing.get("protocolConfiguration"):
            update_params["protocolConfiguration"] = existing["protocolConfiguration"]

        try:
            ac.update_agent_runtime(**update_params)
            print("  Update submitted, waiting...")
            for _ in range(24):
                r = ac.get_agent_runtime(agentRuntimeId=rt_id)
                s = r["status"]
                print(f"    {s}")
                if s == "READY":
                    break
                if s == "FAILED":
                    break
                time.sleep(5)
        except Exception as e:
            print(f"  ERROR: {e}")

    # Now delete failed targets and retry
    print("\n\nDeleting failed targets...")
    existing_targets = ac.list_gateway_targets(gatewayIdentifier="devopsagentgateway-0xdeyhdvbs")
    for item in existing_targets.get("items", []):
        if item["status"] == "FAILED":
            print(f"  Deleting: {item['name']} ({item['targetId']})")
            ac.delete_gateway_target(
                gatewayIdentifier="devopsagentgateway-0xdeyhdvbs",
                targetId=item["targetId"],
            )

    print("  Waiting 15s...")
    time.sleep(15)
    print("\nDone. Now run register_gateway_targets.py or fix_auth_and_reregister.py")


if __name__ == "__main__":
    main()
