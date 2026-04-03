"""
One-time setup for the DevOps Agent runtime.

Creates the IAM role and ECR repository needed before `agentcore launch`.
Safe to re-run — skips resources that already exist.
"""

import json
import boto3

REGION = "ap-southeast-2"
ACCOUNT = "650251690796"
ROLE_NAME = "devops-agent-runner"
ECR_REPO = "bedrock_agentcore-devops_agent"

iam = boto3.client("iam", region_name=REGION)
ecr = boto3.client("ecr", region_name=REGION)


def ensure_role():
    """Create the IAM role if it doesn't exist, then ensure policies are attached."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        iam.get_role(RoleName=ROLE_NAME)
        print(f"IAM role '{ROLE_NAME}' already exists")
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for DevOps Agent AgentCore runtime",
        )
        print(f"Created IAM role '{ROLE_NAME}'")

    # Ensure permissions policy
    permissions_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockModelAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0",
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/*",
            },
        ],
    }

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="agent-permissions",
        PolicyDocument=json.dumps(permissions_policy),
    )
    print(f"Attached 'agent-permissions' policy to '{ROLE_NAME}'")


def ensure_ecr_repo():
    """Create the ECR repository if it doesn't exist."""
    try:
        ecr.describe_repositories(repositoryNames=[ECR_REPO])
        print(f"ECR repo '{ECR_REPO}' already exists")
    except ecr.exceptions.RepositoryNotFoundException:
        ecr.create_repository(repositoryName=ECR_REPO)
        print(f"Created ECR repo '{ECR_REPO}'")

    # Apply policy
    with open("ecr-policy.json") as f:
        policy_text = f.read()
    ecr.set_repository_policy(
        repositoryName=ECR_REPO,
        policyText=policy_text,
    )
    print(f"Applied ECR policy to '{ECR_REPO}'")


if __name__ == "__main__":
    ensure_role()
    ensure_ecr_repo()
    print(f"\nRole ARN: arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}")
    print(f"ECR URI:  {ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}")
    print("\nReady for: agentcore launch")
