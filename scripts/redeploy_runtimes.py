"""
Force-redeploy all 4 runtimes by re-creating them with the same config
but updated authorizerConfiguration.

The running containers need to restart to pick up the new allowedClients.
We do this by adding a trivial env var change to trigger a new deployment.
"""
import time

import boto3

REGION = "ap-southeast-2"
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"

RUNTIMES = [
    "mcp_server-4kjU5oAHWM",
    "monitoring_server-CI86d62MYP",
    "teams_server-hbrhm38Ef3",
    "sns_server-2f8klN8rTF",
]


def main():
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # First, delete the failed targets
    print("Cleaning up failed targets...")
    existing_targets = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    for item in existing_targets.get("items", []):
        if item["status"] == "FAILED":
            print(f"  Deleting: {item['name']} ({item['targetId']})")
            ac.delete_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=item["targetId"])

    # Re-deploy each runtime by updating with an env var to force restart
    print("\nForce-redeploying runtimes...")
    import datetime
    redeploy_ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    for rt_id in RUNTIMES:
        print(f"\n  Re-deploying: {rt_id}")
        existing = ac.get_agent_runtime(agentRuntimeId=rt_id)

        update_params = {
            "agentRuntimeId": rt_id,
            "agentRuntimeArtifact": existing["agentRuntimeArtifact"],
            "roleArn": existing["roleArn"],
            "networkConfiguration": existing["networkConfiguration"],
            "authorizerConfiguration": existing["authorizerConfiguration"],
            "environmentVariables": {"REDEPLOY_TS": redeploy_ts},
        }
        if existing.get("protocolConfiguration"):
            update_params["protocolConfiguration"] = existing["protocolConfiguration"]

        try:
            ac.update_agent_runtime(**update_params)
            print(f"    Submitted with REDEPLOY_TS={redeploy_ts}")
        except Exception as e:
            print(f"    ERROR: {e}")

    # Wait for all to be READY
    print("\nWaiting for all runtimes to be READY...")
    for _ in range(40):  # up to ~200 seconds
        all_ready = True
        for rt_id in RUNTIMES:
            r = ac.get_agent_runtime(agentRuntimeId=rt_id)
            status = r["status"]
            if status != "READY":
                all_ready = False
        if all_ready:
            break
        time.sleep(5)
        print("  Still waiting...")

    for rt_id in RUNTIMES:
        r = ac.get_agent_runtime(agentRuntimeId=rt_id)
        jwt = r.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {})
        print(f"  {rt_id}: status={r['status']}, clients={jwt.get('allowedClients', [])}")

    print("\nAll runtimes redeployed. Now re-run target registration.")


if __name__ == "__main__":
    main()
