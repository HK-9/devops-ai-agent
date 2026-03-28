# Deployment Guide — DevOps AI Agent on Bedrock AgentCore

This guide covers deploying the DevOps AI Agent and its 4 MCP servers to
AWS Bedrock AgentCore.  For architecture and component details, see
[high-level-architecture.md](high-level-architecture.md) and
[devops-agent-walkthrough.md](devops-agent-walkthrough.md).

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Architecture Overview](#architecture-overview)
3. [First-Time Setup](#first-time-setup)
4. [Deploying MCP Servers](#deploying-mcp-servers)
5. [Deploying the Agent](#deploying-the-agent)
6. [Checking Status](#checking-status)
7. [Viewing Logs](#viewing-logs)
8. [Testing](#testing)
9. [Versioning](#versioning)
10. [Troubleshooting](#troubleshooting)
11. [Reference](#reference)

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.12+ | Deploy scripts, agent code |
| Docker Desktop | Latest | Build arm64 container images |
| AWS CLI | v2 | ECR auth, resource management |
| boto3 | Latest | Python AWS SDK (in venv) |
| Git | Any | Version tracking (SHA in tags) |

**AWS Permissions required:**
- ECR: `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:PutImage`, `ecr:DescribeImages`
- AgentCore: `bedrock-agentcore:*` (or scoped to `UpdateAgentRuntime`, `GetAgentRuntime`, `ListAgentRuntimes`, gateway operations)
- IAM: `iam:GetRole`, `iam:PutRolePolicy` (for setup only)
- CloudWatch Logs: `logs:FilterLogEvents`, `logs:DescribeLogStreams` (for `logs` command)

**Environment setup:**
```bash
cd devops-ai-agent
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -e ".[dev]"
```

---

## Architecture Overview

```
┌──────────────┐     ┌─────────────────────────┐
│ Deploy Script │────▶│  ECR (Container Images) │
└──────┬───────┘     └────────────┬────────────┘
       │                          │
       │  update_agent_runtime    │  pull image
       ▼                          ▼
┌──────────────────────────────────────────┐
│        Bedrock AgentCore Runtimes        │
│  ┌──────────┐ ┌─────────┐ ┌───────────┐ │
│  │ aws_infra│ │   sns   │ │monitoring │ │
│  │ (8 tools)│ │(2 tools)│ │ (4 tools) │ │
│  └──────────┘ └─────────┘ └───────────┘ │
│  ┌──────────┐ ┌─────────────────────────┐│
│  │  teams   │ │     devops_agent        ││
│  │ (2 tools)│ │ (Strands + Nova Lite)   ││
│  └──────────┘ └─────────────────────────┘│
└──────────────────┬───────────────────────┘
                   │
            ┌──────┴──────┐
            │   Gateway   │  (AWS_IAM auth)
            │  V3 + OAuth │  tool discovery
            └─────────────┘
```

**What gets deployed:**
- **4 MCP Servers** — containerized FastMCP servers exposing tools (EC2, SSM, CloudWatch, SNS, Teams)
- **1 Agent** — Strands agent using Nova Lite via Bedrock, connected to MCP servers through gateway
- **Gateway targets** — map each MCP server to the gateway for tool discovery

---

## First-Time Setup

Run once per AWS account to create ECR repos and IAM roles:

```bash
# Agent infrastructure (ECR repo + IAM role)
python scripts/deploy_agent.py setup

# MCP server infrastructure is pre-configured in the SERVERS dict.
# ECR repos were created via `bedrock-agentcore create` CLI.
# IAM role "aws-infra-server" is shared by all 4 MCP servers.
```

**Cognito & Gateway setup** (one-time):
```bash
# Creates Cognito user pool, OAuth provider, and gateway with targets
python scripts/setup_cognito_and_register_targets.py
```

---

## Deploying MCP Servers

### Deploy all servers (recommended)

```bash
python scripts/deploy_mcp_servers.py deploy
```

This runs the full pipeline:
1. **Pre-flight** — checks Docker, warns on uncommitted changes
2. **Auto-version** — computes `v{N}-{git_sha}` from ECR tags
3. **Build** — `docker build --platform linux/arm64` for each server
4. **Push** — pushes to ECR with versioned tag + `latest`
5. **Update runtimes** — read-merge-write (preserves `protocolConfiguration`, `authorizerConfiguration`)
6. **Wait** — polls until all runtimes are READY
7. **Validate** — verifies critical config fields were not wiped
8. **Gateway sync** — triggers tool re-discovery on gateway targets

### Deploy specific servers

```bash
python scripts/deploy_mcp_servers.py deploy --servers aws_infra,sns
```

### Preview changes (dry run)

```bash
python scripts/deploy_mcp_servers.py deploy --dry-run
```

Shows the auto-computed version, planned image URIs, and current config
for each runtime — without making any changes.

### Manual tag override

```bash
python scripts/deploy_mcp_servers.py deploy --tag v5-hotfix
```

### Skip gateway sync

```bash
python scripts/deploy_mcp_servers.py deploy --skip-gateway
```

Useful when testing container changes that don't affect tool registration.

---

## Deploying the Agent

```bash
python scripts/deploy_agent.py deploy
```

Same pipeline: pre-flight → auto-version → build → push → deploy → wait.

### Options

```bash
python scripts/deploy_agent.py deploy --dry-run      # preview
python scripts/deploy_agent.py deploy --tag v10       # manual tag
python scripts/deploy_agent.py deploy --no-cache      # force rebuild
```

### Local testing

```bash
python scripts/deploy_agent.py local
```

Builds and runs the agent container locally with mounted AWS credentials.

---

## Checking Status

### MCP Servers

```bash
python scripts/deploy_mcp_servers.py status
```

Shows for each server: runtime status, image tag, protocol config, auth
config, and gateway target status.

### Agent

```bash
python scripts/deploy_agent.py status
```

---

## Viewing Logs

### MCP server logs

```bash
python scripts/deploy_mcp_servers.py logs aws_infra
python scripts/deploy_mcp_servers.py logs monitoring --minutes 10
```

Filters out health-check (`GET /ping`) noise automatically.

### Agent logs

```bash
python scripts/deploy_agent.py logs
python scripts/deploy_agent.py logs --minutes 10
```

---

## Testing

### Invoke the agent directly

```bash
python scripts/deploy_agent.py invoke "List all EC2 instances"
python scripts/deploy_agent.py invoke "What tools do you have available?"
```

### Run the test script

```bash
python scripts/test_agent.py
```

### Run unit tests

```bash
pytest -v -m unit
```

---

## Versioning

### How it works

The deploy scripts use **automatic semantic versioning**:

- **Scheme:** `v{N}-{git_sha[:7]}` (e.g., `v4-a3b4c5d`)
- **N** auto-increments by querying ECR for the highest existing `v*` tag
- **git SHA** provides commit traceability — any running container can be
  traced back to the exact commit that built it

### Version flow

```
ECR has: v1-abc1234, v2-def5678, v3-ghi9012
Next deploy: v4-{current_HEAD_sha}
```

### Manual override

Pass `--tag` to use a specific tag instead of auto-increment:

```bash
python scripts/deploy_mcp_servers.py deploy --tag v3-hotfix
```

### Dirty tree warning

If you deploy with uncommitted changes, the script warns:

```
WARNING: Uncommitted changes detected. The deploy will use the
current HEAD SHA, but your local changes won't match the tagged version.
```

---

## Troubleshooting

### Gateway targets FAILED after deploy

**Symptom:** `status` shows gateway targets as FAILED with "20000ms timeout"

**Root cause:** The runtime's `protocolConfiguration` was wiped. The
`update_agent_runtime` API is a full-replace — any field not explicitly
passed gets removed.

**Fix:** The deploy script now uses read-merge-write (GET current config →
overlay changes → PUT full config). If you hit this with an older script
version, manually restore the protocol config:

```python
ac = boto3.client('bedrock-agentcore-control', region_name='ap-southeast-2')
ac.update_agent_runtime(
    agentRuntimeId='mcp_server-4kjU5oAHWM',
    # ... all other fields from get_agent_runtime() ...
    protocolConfiguration={'serverProtocol': 'MCP'},
)
```

### Auth wiped after redeploy

**Symptom:** Agent invocation fails with auth errors after MCP server redeploy.

**Root cause:** Same as above — `authorizerConfiguration` was not included
in the `update_agent_runtime` call.

**Fix:** The deploy script's read-merge-write preserves this automatically.
Check with `status` command — it now shows `Auth: present` or `Auth: MISSING`.

### Container not restarting (old code running)

**Symptom:** Deploy succeeds, runtime is READY, but tools haven't changed.

**Root cause:** Updating a runtime with the same image digest doesn't trigger
a container restart.

**Fix:** The deploy script sets `DEPLOY_VERSION={tag}` as an environment
variable, which changes the runtime config and forces a restart. Auto-versioning
also ensures each deploy uses a new tag.

### Architecture mismatch

**Symptom:** Runtime FAILED with "Architecture incompatible...Supported
architectures: [arm64]"

**Root cause:** Docker image was built for `linux/amd64` instead of `linux/arm64`.

**Fix:** The deploy scripts hardcode `PLATFORM = "linux/arm64"`. Ensure Docker
Desktop has the arm64 emulation enabled (Settings → General → "Use Rosetta"
on macOS, or QEMU on Windows/Linux).

### Tools not visible to agent

**Symptom:** Agent only sees a subset of tools (e.g., 10 instead of 16).

**Possible causes:**
1. Gateway targets not synced — run `deploy --servers <name>` (includes sync)
2. MCP server container running old image — check `status` command for tag
3. Gateway target FAILED — check `status` for target status

---

## Reference

### Resource IDs

| Resource | ID |
|----------|-----|
| Agent Runtime | `devops_agent-AYHFY5ECcy` |
| aws_infra Runtime | `mcp_server-4kjU5oAHWM` |
| monitoring Runtime | `monitoring_server-CI86d62MYP` |
| sns Runtime | `sns_server-2f8klN8rTF` |
| teams Runtime | `teams_server-hbrhm38Ef3` |
| Gateway V3 | `devopsagentgatewayv3-ar4lmz2x6t` |
| IAM Role (MCP servers) | `aws-infra-server` |
| IAM Role (Agent) | `devops-agent-runner` |
| Cognito Pool | `ap-southeast-2_OmD4OzAYI` |
| OAuth Provider | `devops-gateway-cognito-oauth` |

### ECR Repositories

| Server | Repository |
|--------|-----------|
| aws_infra | `bedrock_agentcore-mcp_server` |
| monitoring | `bedrock_agentcore-monitoring_server` |
| sns | `bedrock_agentcore-sns_server` |
| teams | `bedrock_agentcore-teams_server` |
| agent | `bedrock_agentcore-devops_agent` |

### Deploy Scripts

| Script | Purpose |
|--------|---------|
| `scripts/deploy_mcp_servers.py` | Deploy all 4 MCP servers + gateway sync |
| `scripts/deploy_agent.py` | Deploy the agent runtime |
| `scripts/_version.py` | Auto-versioning utility (shared) |
| `scripts/test_agent.py` | Invoke agent for testing |
| `scripts/setup_cognito_and_register_targets.py` | One-time Cognito + gateway setup |
| `scripts/register_gateway_targets.py` | One-time gateway target registration |

### Key Configuration

| Config | Value |
|--------|-------|
| Region | `ap-southeast-2` |
| Platform | `linux/arm64` |
| Model | `amazon.nova-lite-v1:0` |
| Gateway Auth | `AWS_IAM` |
| MCP Server Auth | Cognito JWT (custom authorizer) |
| Network Mode | `PUBLIC` |
