import boto3
import time

ac = boto3.client("bedrock-agentcore-control", region_name="ap-southeast-2")
GW = "devopsagentgateway-0xdeyhdvbs"

targets = ac.list_gateway_targets(gatewayIdentifier=GW)
for t in targets.get("items", []):
    tid = t["targetId"]
    name = t["name"]
    print(f"Syncing {name} ({tid})...")
    ac.synchronize_gateway_targets(gatewayIdentifier=GW, targetIdList=[tid])
    time.sleep(3)

time.sleep(10)
gw = ac.get_gateway(gatewayIdentifier=GW)
status = gw["status"]
print(f"\nGateway status: {status}")

targets = ac.list_gateway_targets(gatewayIdentifier=GW)
for t in targets.get("items", []):
    name = t["name"]
    st = t["status"]
    print(f"  {name}: {st}")

print(f"\nGateway URL: https://{GW}.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp")
