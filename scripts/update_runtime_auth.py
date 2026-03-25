"""
Step 1 ONLY: Update runtime authorizer configs to accept the gateway OAuth client.
Fetches existing runtime config and re-sends all required fields alongside the updated auth.
"""

import time

import boto3

REGION = "ap-southeast-2"
ORIGINAL_CLIENT_ID = "5blfslp7o1shoe86g2o0ltbnm8"
GATEWAY_OAUTH_CLIENT_ID = "3i9cn8o8huu4rlpo8e46tr51ap"
USER_POOL_ID = "ap-southeast-2_OmD4OzAYI"
DISCOVERY_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/openid-configuration"

RUNTIMES = [
    "mcp_server-4kjU5oAHWM",
    "monitoring_server-CI86d62MYP",
    "teams_server-hbrhm38Ef3",
    "sns_server-2f8klN8rTF",
]


def main():
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)

    allowed = [ORIGINAL_CLIENT_ID, GATEWAY_OAUTH_CLIENT_ID]
    new_auth = {
        "customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY_URL,
            "allowedAudience": allowed,
            "allowedClients": allowed,
        }
    }

    for rt_id in RUNTIMES:
        print(f"\n{'=' * 50}")
        print(f"Updating: {rt_id}")

        # Get existing config
        existing = ac.get_agent_runtime(agentRuntimeId=rt_id)
        print(f"  Current status: {existing['status']}")
        print(f"  Current auth: {existing.get('authorizerConfiguration', {})}")

        # Build update params with all required fields from existing config
        update_params = {
            "agentRuntimeId": rt_id,
            "agentRuntimeArtifact": existing["agentRuntimeArtifact"],
            "roleArn": existing["roleArn"],
            "networkConfiguration": existing["networkConfiguration"],
            "authorizerConfiguration": new_auth,
        }

        # Include optional fields if they exist
        if existing.get("protocolConfiguration"):
            update_params["protocolConfiguration"] = existing["protocolConfiguration"]

        try:
            ac.update_agent_runtime(**update_params)
            print(f"  Update submitted. Waiting for READY...")

            # Wait for READY
            for _ in range(30):
                r = ac.get_agent_runtime(agentRuntimeId=rt_id)
                status = r["status"]
                print(f"    status: {status}")
                if status == "READY":
                    # Verify auth was updated
                    auth = r.get("authorizerConfiguration", {})
                    jwt = auth.get("customJWTAuthorizer", {})
                    clients = jwt.get("allowedClients", [])
                    print(f"    allowedClients: {clients}")
                    break
                if status == "FAILED":
                    print(f"    FAILED!")
                    break
                time.sleep(5)
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\nDone! Verify in next step.")


if __name__ == "__main__":
    main()
