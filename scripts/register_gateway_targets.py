"""
Register the 4 AgentCore Runtime MCP servers as gateway targets.

This script uses the boto3 `bedrock-agentcore-control` API to:
1. Create a gateway target for each runtime using `mcpServer` target type
2. Use `GATEWAY_IAM_ROLE` credentials (the gateway's execution role has
   `bedrock-agentcore:*` so it can invoke runtimes via sigv4)
3. Wait for each target to reach READY status
4. Synchronize the gateway to discover all tools

Architecture:
  Main Agent → Gateway (devopsagentgateway-0xdeyhdvbs)
                   → aws_infra runtime   (mcp_server-4kjU5oAHWM)
                   → monitoring runtime   (monitoring_server-CI86d62MYP)
                   → teams runtime        (teams_server-hbrhm38Ef3)
                   → sns runtime          (sns_server-2f8klN8rTF)
"""

import sys
import time
import urllib.parse

import boto3

REGION = "ap-southeast-2"
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"

# Data plane endpoint for constructing runtime MCP URLs
DATA_PLANE_HOST = f"https://bedrock-agentcore.{REGION}.amazonaws.com"

# The 4 AgentCore Runtimes to register as targets
TARGETS = [
    {
        "name": "aws-infra",
        "runtime_id": "mcp_server-4kjU5oAHWM",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM",
        "description": "EC2 infrastructure tools: list, describe, restart instances",
    },
    {
        "name": "monitoring",
        "runtime_id": "monitoring_server-CI86d62MYP",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/monitoring_server-CI86d62MYP",
        "description": "CloudWatch metrics: CPU, memory, disk usage",
    },
    {
        "name": "teams",
        "runtime_id": "teams_server-hbrhm38Ef3",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/teams_server-hbrhm38Ef3",
        "description": "Microsoft Teams notifications: messages and incident cards",
    },
    {
        "name": "sns",
        "runtime_id": "sns_server-2f8klN8rTF",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/sns_server-2f8klN8rTF",
        "description": "Alert failover: Teams primary, SNS fallback",
    },
]


def wait_for_target_ready(client, gateway_id: str, target_id: str, timeout: int = 120) -> str:
    """Poll until a gateway target reaches READY (or terminal) status."""
    start = time.time()
    while time.time() - start < timeout:
        resp = client.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
        )
        status = resp["status"]
        reasons = resp.get("statusReasons", [])
        print(f"    status: {status}")
        if status == "READY":
            return status
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"):
            print(f"    ERROR - terminal status: {status}, reasons: {reasons}")
            return status
        time.sleep(5)
    print(f"    TIMEOUT after {timeout}s")
    return "TIMEOUT"


def main():
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # --- Step 1: Check existing targets ---
    print("=" * 60)
    print("Checking existing gateway targets...")
    existing = client.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    existing_items = existing.get("items", [])
    if existing_items:
        print(f"  Found {len(existing_items)} existing target(s):")
        for item in existing_items:
            print(f"    - {item['name']} (ID: {item['targetId']}, status: {item['status']})")
        print()
        answer = input("Targets already exist. Continue and add new ones? (y/n): ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)
    else:
        print("  No existing targets — proceeding with registration.\n")

    # --- Step 2: Register each runtime as a gateway target ---
    created_targets = []
    for target_info in TARGETS:
        print(f"\n{'=' * 60}")
        print(f"Registering target: {target_info['name']}")
        print(f"  Runtime ARN: {target_info['runtime_arn']}")

        # Construct the MCP endpoint URL through the AgentCore data plane
        encoded_arn = urllib.parse.quote(target_info["runtime_arn"], safe="")
        mcp_endpoint = f"{DATA_PLANE_HOST}/runtimes/{encoded_arn}/invocations"
        print(f"  MCP endpoint: {mcp_endpoint}")

        try:
            resp = client.create_gateway_target(
                gatewayIdentifier=GATEWAY_ID,
                name=target_info["name"],
                description=target_info["description"],
                targetConfiguration={
                    "mcp": {
                        "mcpServer": {
                            "endpoint": mcp_endpoint,
                        }
                    }
                },
                credentialProviderConfigurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
            )
            target_id = resp["targetId"]
            print(f"  Created target ID: {target_id}")
            print(f"  Waiting for READY...")
            status = wait_for_target_ready(client, GATEWAY_ID, target_id)
            created_targets.append({
                "name": target_info["name"],
                "target_id": target_id,
                "status": status,
            })
        except Exception as exc:
            print(f"  FAILED: {exc}")
            created_targets.append({
                "name": target_info["name"],
                "target_id": None,
                "status": f"ERROR: {exc}",
            })

    # --- Step 3: Summary ---
    print(f"\n{'=' * 60}")
    print("REGISTRATION SUMMARY")
    print("=" * 60)
    all_ready = True
    for t in created_targets:
        marker = "OK" if t["status"] == "READY" else "FAIL"
        print(f"  [{marker}] {t['name']}: {t['status']} (ID: {t['target_id']})")
        if t["status"] != "READY":
            all_ready = False

    if not all_ready:
        print("\nSome targets failed. Check the errors above.")
        print("You can retry by deleting failed targets first:")
        print(f"  client.delete_gateway_target(gatewayIdentifier='{GATEWAY_ID}', targetId='<id>')")
        sys.exit(1)

    # --- Step 4: Sync gateway ---
    print(f"\nSynchronizing gateway targets...")
    try:
        client.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        print("  Sync initiated. Waiting for completion...")
        time.sleep(10)
        gw = client.get_gateway(gatewayIdentifier=GATEWAY_ID)
        print(f"  Gateway status: {gw['status']}")
    except Exception as exc:
        print(f"  Sync warning: {exc}")
        print("  (Gateway may auto-sync. Check status manually if needed.)")

    print(f"\nGateway MCP URL: https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")
    print("Done.")


if __name__ == "__main__":
    main()
