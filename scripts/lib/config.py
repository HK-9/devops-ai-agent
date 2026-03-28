"""
Shared configuration for AgentCore deployments.

All tuneable values in one place.  Override via env vars where noted.
"""

from __future__ import annotations

import os
import urllib.parse

# ── AWS ──────────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "650251690796")
PLATFORM = "linux/arm64"  # AgentCore runtimes require arm64

# ── MCP Server Definitions ──────────────────────────────────────────────

MCP_SERVERS = {
    "aws_infra": {
        "deploy_dir": "deploy_aws_infra",
        "runtime_name": "mcp_server",
        "runtime_id": "mcp_server-4kjU5oAHWM",
        "ecr_repo": "bedrock_agentcore-mcp_server",
        "role": "aws-infra-server",
    },
    "monitoring": {
        "deploy_dir": "deploy_monitoring",
        "runtime_name": "monitoring_server",
        "runtime_id": "monitoring_server-CI86d62MYP",
        "ecr_repo": "bedrock_agentcore-monitoring_server",
        "role": "aws-infra-server",
    },
    "sns": {
        "deploy_dir": "deploy_sns",
        "runtime_name": "sns_server",
        "runtime_id": "sns_server-2f8klN8rTF",
        "ecr_repo": "bedrock_agentcore-sns_server",
        "role": "aws-infra-server",
    },
    "teams": {
        "deploy_dir": "deploy_teams",
        "runtime_name": "teams_server",
        "runtime_id": "teams_server-hbrhm38Ef3",
        "ecr_repo": "bedrock_agentcore-teams_server",
        "role": "aws-infra-server",
    },
}

# ── Agent Definitions ────────────────────────────────────────────────────

AGENT_NAME = "devops_agent"
AGENT_ECR_REPO = f"bedrock_agentcore-{AGENT_NAME}"
AGENT_ROLE = "devops-agent-runner"
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-lite-v1:0")

# ── Gateway ──────────────────────────────────────────────────────────────

GATEWAY_ID = "devopsagentgatewayv3-ar4lmz2x6t"

GATEWAY_URL = os.environ.get(
    "GATEWAY_URL",
    f"https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp",
)

GATEWAY_TARGETS = {
    "aws_infra": "aws-infra-target",
    "monitoring": "monitoring-target",
    "sns": "sns-target",
    "teams": "teams-target",
}

GATEWAY_CREDENTIAL_CONFIG = [
    {
        "credentialProviderType": "OAUTH",
        "credentialProvider": {
            "oauthCredentialProvider": {
                "providerArn": (
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
                    "runtime/token-vault/default/oauth2credentialprovider/"
                    "devops-gateway-cognito-oauth"
                ),
                "scopes": ["mcp-tools/invoke"],
                "grantType": "CLIENT_CREDENTIALS",
            }
        },
    }
]


# ── Derived helpers ──────────────────────────────────────────────────────

def ecr_uri(ecr_repo: str) -> str:
    """Full ECR image URI (without tag)."""
    return f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ecr_repo}"


def role_arn(role_name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{role_name}"


def runtime_arn(runtime_id: str) -> str:
    return f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{runtime_id}"


def runtime_endpoint(runtime_id: str) -> str:
    arn = runtime_arn(runtime_id)
    encoded = urllib.parse.quote(arn, safe="")
    return f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded}/invocations"


def log_group(runtime_id: str) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
