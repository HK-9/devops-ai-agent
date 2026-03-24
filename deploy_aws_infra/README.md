# deploy_aws_infra — AWS Infrastructure MCP Server for Bedrock AgentCore

A standalone MCP (Model Context Protocol) server that exposes EC2 management tools for **AWS Bedrock AgentCore Runtime**. Designed to be deployed as a container and hosted by Bedrock's managed MCP runtime.

---

## Overview

| Aspect | Description |
|--------|-------------|
| **Purpose** | Expose EC2 list/describe/restart tools to Bedrock agents via MCP |
| **Transport** | Streamable HTTP (stateless) — required for AgentCore Runtime |
| **Framework** | FastMCP |
| **Deployment** | Docker container → ECR → Bedrock AgentCore Runtime |
| **Auth** | JWT via Amazon Cognito (configurable in `.bedrock_agentcore.yaml`) |

### Tools Exposed

| Tool | Description |
|------|-------------|
| `list_ec2_instances_tool` | List EC2 instances by state (running/stopped/all) and optional tags |
| `describe_ec2_instance_tool` | Get detailed info for a single instance (network, security groups, volumes) |
| `restart_ec2_instance_tool` | Restart (stop + start) an EC2 instance |

---

## Directory Structure

```
deploy_aws_infra/
├── README.md                 # This file
├── Dockerfile                # Container build for AgentCore Runtime
├── .bedrock_agentcore.yaml   # Deployment config (ECR, IAM, Cognito, etc.)
├── .dockerignore             # Excludes non-runtime files from image
├── requirements.txt          # Python dependencies
├── mcp_server.py             # FastMCP server entry point
├── tools.py                  # EC2 tool implementations
├── config.py                 # Pydantic settings (region, profile, etc.)
└── aws_helpers.py            # Boto3 client factory, logging, safe_boto_call
```

---

## Prerequisites

### 1. AWS Account & Credentials

- AWS CLI configured (`aws configure` or env vars)
- IAM execution role with EC2 read + restart permissions

### 2. IAM Execution Role

The container runs under the IAM role specified in `.bedrock_agentcore.yaml`:

```yaml
execution_role: arn:aws:iam::650251690796:role/aws-infra-server
```

The role must allow at least:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:StopInstances",
        "ec2:StartInstances"
      ],
      "Resource": "*"
    }
  ]
}
```

### 3. Cognito User Pool (for JWT Auth)

Bedrock AgentCore uses a custom JWT authorizer. You need:

- A Cognito User Pool
- An App Client with `ALLOW_USER_PASSWORD_AUTH`
- Discovery URL and `allowedClients` / `allowedAudience` in `.bedrock_agentcore.yaml`

Use the provided script:

```powershell
.\scripts\setup_cognito.ps1 -Region ap-southeast-2
```

Then update `.bedrock_agentcore.yaml` with the outputs (Pool ID, Client ID, Discovery URL).

### 4. Python & Tools

- Python 3.14+ (matches Dockerfile base image)
- Docker (for building and pushing the image)
- `bedrock-agentcore-starter-toolkit` (install via `pip install -e ".[deploy]"` from project root)

---

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region for EC2 API calls | `ap-southeast-2` |
| `AWS_DEFAULT_REGION` | Same as above | `ap-southeast-2` |
| `AWS_PROFILE` | Named profile (optional) | — |
| `LOG_LEVEL` | Logging level | `INFO` |
| `LOG_FORMAT` | `json` or `text` | `json` |

These can be set in `.env` (for local runs) or via the container environment.

### `.bedrock_agentcore.yaml`

Controls deployment to Bedrock AgentCore Runtime:

| Section | Purpose |
|---------|---------|
| `default_agent` | Entry point for the toolkit CLI |
| `agents.mcp_server` | Defines the MCP server agent |
| `entrypoint` | `mcp_server.py` — module to run |
| `platform` | `linux/arm64` — build for ARM |
| `aws.execution_role` | IAM role for the container |
| `aws.ecr_repository` | ECR repo (null = auto-create) |
| `aws.ecr_auto_create` | Create ECR repo if missing |
| `network_configuration.network_mode` | `PUBLIC` for internet access |
| `protocol_configuration.server_protocol` | `MCP` |
| `observability.enabled` | CloudWatch / OpenTelemetry |
| `authorizer_configuration.customJWTAuthorizer` | Cognito OIDC discovery URL and allowed clients |

**Important:** `.bedrock_agentcore.yaml` is excluded from the Docker image by `.dockerignore`. It is used only by the `bedrock-agentcore-starter-toolkit` deploy CLI when pushing and registering.

---

## Build & Deploy

### Option A: Deploy via Bedrock AgentCore Starter Toolkit (Recommended)

From the **project root** (parent of `deploy_aws_infra`):

```bash
# Install deploy extras
pip install -e ".[deploy]"

# Navigate to deploy_aws_infra
cd deploy_aws_infra

# Deploy (builds image, pushes to ECR, registers with AgentCore)
agentcore deploy
# or
bedrock-agentcore deploy
```

The exact CLI command depends on how `bedrock-agentcore-starter-toolkit` exposes its interface. Consult the toolkit docs.

### Option B: Manual Docker Build

```bash
cd deploy_aws_infra

# Build
docker build -t aws-infra-mcp-server .

# Run locally (port 8080)
docker run -p 8080:8080 \
  -e AWS_REGION=ap-southeast-2 \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  aws-infra-mcp-server
```

### Option C: Push to ECR and Register Manually

```bash
# Authenticate to ECR
aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin 650251690796.dkr.ecr.ap-southeast-2.amazonaws.com

# Create repo (if needed)
aws ecr create-repository --repository-name aws-infra-mcp-server --region ap-southeast-2

# Build and tag
docker build -t aws-infra-mcp-server .
docker tag aws-infra-mcp-server:latest 650251690796.dkr.ecr.ap-southeast-2.amazonaws.com/aws-infra-mcp-server:latest

# Push
docker push 650251690796.dkr.ecr.ap-southeast-2.amazonaws.com/aws-infra-mcp-server:latest
```

Then register the image with Bedrock AgentCore via the AWS Console or API.

---

## Local Development & Testing

### Run the MCP server locally (streamable-http)

```bash
cd deploy_aws_infra

# Create venv and install deps
python -m venv .venv
.venv\Scripts\activate   # Windows
source .venv/bin/activate  # Linux/macOS

pip install -r requirements.txt

# Ensure AWS credentials are set
export AWS_REGION=ap-southeast-2   # or set in .env

# Run (listens on 0.0.0.0:8080)
python -m mcp_server
```

### Test tools directly

```python
# Example: call list_ec2_instances from Python
import asyncio
from tools import list_ec2_instances

async def main():
    result = await list_ec2_instances(state_filter="running", max_results=10)
    print(result)

asyncio.run(main())
```

---

## Relationship to Main Project

| Main project (`src/mcp_servers/aws_infra/`) | deploy_aws_infra |
|---------------------------------------------|------------------|
| Stdio transport (subprocess) | Streamable HTTP transport |
| Used by MCPClient in-process | Used by Bedrock AgentCore Runtime (hosted) |
| Part of devops-ai-agent app | Standalone deployable unit |

The tool logic in `tools.py` mirrors `src/mcp_servers/aws_infra/tools.py`. The main difference is transport: stdio for local/Lambda, streamable-http for Bedrock-hosted MCP.

---

## Troubleshooting

### Import error: `src.agent.config`

`aws_helpers.py` imports `from src.agent.config import settings`. When running `deploy_aws_infra` as a **standalone** package (no parent `src/`), this fails.

**Fix:** Change `aws_helpers.py` to:

```python
from config import settings
```

Ensure `config.py` defines `aws_region`, `tool_timeout_seconds`, `log_level`, and `log_format`.

### Container fails to start

- Check CloudWatch Logs for the task (if deployed to AgentCore).
- Ensure the execution role has EC2 permissions.
- Verify `AWS_REGION` is set inside the container.

### Cognito JWT rejected

- Confirm `discoveryUrl`, `allowedClients`, and `allowedAudience` in `.bedrock_agentcore.yaml` match your Cognito pool and app client.
- Ensure the user exists and can obtain a token via `ALLOW_USER_PASSWORD_AUTH`.

### ECR push denied

- Run `aws ecr get-login-password` and `docker login` for the correct registry.
- Ensure your IAM user/role has `ecr:GetDownloadUrlForLayer`, `ecr:BatchGetImage`, `ecr:BatchCheckLayerAvailability`, `ecr:PutImage`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`.

---

## Observability

With `observability.enabled: true` in `.bedrock_agentcore.yaml`:

- OpenTelemetry instrumentation is applied via `aws_opentelemetry_distro_genai_beta`.
- The container runs under `opentelemetry-instrument python -m mcp_server`.
- Traces and metrics can be sent to CloudWatch or another collector.

---

## Security

- The container runs as non-root user `bedrock_agentcore` (UID 1000).
- IAM execution role should be scoped to the minimum EC2 permissions required.
- JWT auth ensures only authenticated clients (e.g., Bedrock) can invoke the MCP server.

---

## References

- [AWS Bedrock Agents](https://docs.aws.amazon.com/bedrock/latest/userguide/agents.html)
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- [FastMCP](https://github.com/jlowin/fastmcp)
- Main project README: `../README.md`
