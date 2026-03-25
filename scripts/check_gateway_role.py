"""Check the gateway execution role permissions."""
import boto3
import json

iam = boto3.client("iam")
role_name = "AgentCoreGatewayExecutionRole"

# Check managed policies
attached = iam.list_attached_role_policies(RoleName=role_name)
for p in attached["AttachedPolicies"]:
    arn = p["PolicyArn"]
    ver = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
    doc = iam.get_policy_version(PolicyArn=arn, VersionId=ver)["PolicyVersion"]["Document"]
    print(f"=== {p['PolicyName']} ===")
    print(json.dumps(doc, indent=2)[:2000])
    print()

# Trust policy
role = iam.get_role(RoleName=role_name)
print("=== Trust Policy ===")
print(json.dumps(role["Role"]["AssumeRolePolicyDocument"], indent=2))
