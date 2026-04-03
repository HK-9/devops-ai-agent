# DevOps AI Agent — Project Handoff Document

> **Last updated:** 2026-04-02
> **Branch:** `feature/testing`
> **Current deployed version:** `v19-e0f1d14` (Nova Pro + all fixes)
> **Status:** MINOR and MAJOR alarm workflows tested and working end-to-end

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Repository Structure](#3-repository-structure)
4. [Key Files](#4-key-files)
5. [Environment Setup](#5-environment-setup)
6. [Configuration](#6-configuration)
7. [Deployment Scripts](#7-deployment-scripts)
8. [Testing Scripts](#8-testing-scripts)
9. [MCP Gateway Tools](#9-mcp-gateway-tools)
10. [How the Alarm Pipeline Works](#10-how-the-alarm-pipeline-works)
11. [Agent System Prompt & Workflow](#11-agent-system-prompt--workflow)
12. [Fixes Applied (History)](#12-fixes-applied-history)
13. [Known Issues & Remaining Work](#13-known-issues--remaining-work)
14. [Troubleshooting](#14-troubleshooting)
15. [Quick Reference Commands](#15-quick-reference-commands)

---

## 1. Project Overview

An **autonomous DevOps AI agent** that monitors AWS EC2 instances via CloudWatch alarms, diagnoses issues via SSM, and either auto-fixes minor problems or requests human approval for major ones — all via email with clickable APPROVE/REJECT links.

| Item | Value |
|------|-------|
| **Language** | Python 3.12 (container) / 3.14 (local dev) |
| **AI Framework** | [Strands Agents SDK](https://github.com/strands-agents/sdk-python) (`strands-agents>=1.0.0`) |
| **Foundation Model** | `amazon.nova-pro-v1:0` (Amazon Bedrock) |
| **Tool Protocol** | MCP (Model Context Protocol) via AgentCore Gateway |
| **AWS Region** | `ap-southeast-2` (Sydney) |
| **AWS Account** | `650251690796` |
| **Notification** | SNS email to `kash8580@gmail.com` (Teams webhook optional) |

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    ALARM PIPELINE                            │
│                                                              │
│  CloudWatch Alarm                                            │
│       │                                                      │
│       ▼                                                      │
│  EventBridge Rule (state change → ALARM)                     │
│       │                                                      │
│       ▼                                                      │
│  Lambda (src/handlers/lambda_handler.py)                     │
│    • Parses alarm event                                      │
│    • DynamoDB dedup (10-min window, same alarm = skip)        │
│    • Invokes AgentCore runtime via SigV4 HTTP                │
│       │                                                      │
│       ▼                                                      │
│  Agent Container (deployments/agent/agent.py)                │
│    • Strands Agent + Nova Pro model                          │
│    • Tool name sanitization (hyphens → underscores)          │
│    • Intent-based tool routing (12 tools for alarms)         │
│    • describe→diagnose interception                          │
│    • MAX_TURNS=4 hard cap                                    │
│       │                                                      │
│       ▼                                                      │
│  MCP Gateway (devopsagentgatewayv3)                          │
│    ├── aws-infra-target  (8 tools: diagnose, remediate, etc) │
│    ├── monitoring-target (4 tools: CPU/mem/disk metrics)      │
│    ├── sns-target        (4 tools: email, approval workflow)  │
│    └── teams-target      (2 tools: Teams messages)            │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                    WEB APP (optional)                         │
│                                                              │
│  Flask UI (web/app.py) → web/agent.py                        │
│    • Remote mode: calls deployed agent via invoke_agent_runtime│
│    • Local mode:  imports deployments/agent/agent.py directly │
│    • Runs on http://127.0.0.1:5001                           │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Repository Structure

```
devops-ai-agent/
├── deployments/
│   ├── agent/                      # Agent container (deployed to AgentCore)
│   │   ├── agent.py                # ★ MAIN FILE — all agent logic
│   │   ├── sigv4_auth.py           # SigV4 auth for MCP gateway
│   │   ├── Dockerfile              # Container image (python:3.12-slim, arm64)
│   │   ├── requirements.txt        # Container dependencies
│   │   ├── permissions-policy.json # IAM policy (Nova Pro + Lite whitelisted)
│   │   ├── trust-policy.json       # IAM trust for bedrock-agentcore
│   │   └── setup_infra.py          # One-time ECR + IAM setup
│   │
│   └── mcp_servers/                # MCP tool servers (deployed to AgentCore)
│       ├── aws_infra/              # EC2, SSM, diagnose, remediate tools
│       │   ├── mcp_server.py       # FastMCP server registration
│       │   ├── tools.py            # Tool implementations
│       │   ├── aws_helpers.py      # boto3 wrappers
│       │   └── config.py           # Settings
│       ├── monitoring/             # CloudWatch metrics tools
│       ├── sns/                    # Email, approval workflow tools
│       │   ├── tools.py            # send_alert, request_approval, etc.
│       │   ├── approval_handler.py # Lambda for APPROVE/REJECT link clicks
│       │   └── sns_server.py       # FastMCP server
│       └── teams/                  # Microsoft Teams webhook tools
│
├── src/
│   ├── handlers/
│   │   └── lambda_handler.py       # ★ EventBridge → Lambda → Agent
│   ├── agent/                      # OLD architecture (not used)
│   └── mcp_servers/                # OLD local MCP servers (not used)
│
├── web/
│   ├── app.py                      # Flask app factory (port 5001)
│   ├── agent.py                    # Thin client (remote or local agent)
│   ├── routes.py                   # /chat UI, /api/chat API
│   └── templates/                  # HTML templates
│
├── scripts/
│   ├── deploy_agent.py             # ★ Deploy agent: build → push ECR → update runtime
│   ├── deploy_mcp_servers.py       # ★ Deploy MCP servers: build → push → update
│   ├── deploy_approval_handler.py  # Deploy the approval Lambda
│   ├── test_remediation.py         # ★ Test MINOR/MAJOR scenarios with real stress
│   ├── test_automation.py          # Full E2E: lower thresholds → stress → wait
│   ├── test_agent.py               # Quick agent invocation test
│   ├── demo.py                     # Demo script
│   ├── validate_setup.py           # Validate AWS resources
│   └── lib/                        # Shared deploy utilities
│       ├── config.py               # ★ All IDs, ARNs, regions, gateway config
│       ├── runtime.py              # AgentCore runtime update logic
│       ├── aws.py                  # AgentCore boto3 client helpers
│       ├── docker.py               # Docker build/push helpers
│       ├── gateway.py              # MCP gateway target sync
│       ├── console.py              # Terminal colors, logging
│       └── version.py              # Auto-versioning from git + ECR tags
│
├── infra/                          # AWS CDK stack (partially used)
│   ├── app.py
│   └── stacks/
│       └── agent_runner_stack.py   # Lambda, DynamoDB, SNS, API Gateway
│
├── docs/
│   ├── HANDOFF.md                  # ★ This file
│   ├── AGENT-CONTEXT.md            # Session context (for AI assistants)
│   ├── SESSION-LOG.md              # Build session log
│   ├── deployment-guide.md         # Older deployment guide
│   └── ...                         # Other architecture docs
│
├── .env                            # Local environment variables (not committed)
├── requirements.txt                # Local dev dependencies
└── pyproject.toml                  # Project metadata
```

---

## 4. Key Files

| File | Purpose | When to edit |
|------|---------|-------------|
| `deployments/agent/agent.py` | **Single source of truth** for all agent logic: model config, system prompt, tool routing, Nova workarounds, MAX_TURNS, retry logic | Changing agent behavior, prompt, model, tool routing |
| `deployments/mcp_servers/aws_infra/tools.py` | SSM diagnostic & remediation tools (diagnose, kill process, disk cleanup) | Changing what diagnose collects, remediation logic |
| `deployments/mcp_servers/sns/tools.py` | Email notifications, approval workflow with APPROVE/REJECT links | Changing email format, approval flow |
| `src/handlers/lambda_handler.py` | Lambda entry point: parses CloudWatch alarm, dedup check, invokes agent | Changing alarm parsing, dedup window |
| `scripts/lib/config.py` | Central config: all AWS IDs, ARNs, gateway URLs, model ID | Adding new MCP servers, changing regions/accounts |
| `web/agent.py` | Web app thin client (remote or local agent invocation) | Changing how web UI talks to agent |

---

## 5. Environment Setup

### Prerequisites

- Python 3.12+ (3.14 works locally, container uses 3.12)
- Docker Desktop (for building arm64 container images)
- AWS CLI configured with credentials for account `650251690796`
- Region set to `ap-southeast-2`

### First-time setup

```bash
# Clone and enter project
cd C:\Users\pvhar\Work\devops-ai-agent

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

### .env file

Create `.env` in the project root with:

```env
AWS_REGION=ap-southeast-2
AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy
GATEWAY_URL=https://devopsagentgatewayv3-ar4lmz2x6t.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp
MODEL_ID=amazon.nova-pro-v1:0
SNS_TOPIC_ARN=arn:aws:sns:ap-southeast-2:650251690796:devops-agent-alerts
```

---

## 6. Configuration

### AWS Resources

| Resource | Identifier |
|----------|-----------|
| **Agent Runtime** | `devops_agent-AYHFY5ECcy` |
| **Agent Runtime ARN** | `arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy` |
| **MCP Gateway** | `devopsagentgatewayv3-ar4lmz2x6t` |
| **Lambda** | `devops-ai-agent-handler` |
| **SNS Topic** | `arn:aws:sns:ap-southeast-2:650251690796:devops-agent-alerts` |
| **DynamoDB Table** | `devops-agent-approvals` (approvals + alarm dedup) |
| **Approval API Gateway** | `https://nrp0zqvt76.execute-api.ap-southeast-2.amazonaws.com` |
| **ECR (Agent)** | `bedrock_agentcore-devops_agent` |
| **ECR (MCP aws_infra)** | `bedrock_agentcore-mcp_server` |
| **ECR (MCP monitoring)** | `bedrock_agentcore-monitoring_server` |
| **ECR (MCP sns)** | `bedrock_agentcore-sns_server` |
| **ECR (MCP teams)** | `bedrock_agentcore-teams_server` |
| **IAM Role (Agent)** | `devops-agent-runner` |
| **IAM Role (MCP)** | `aws-infra-server` |
| **Model** | `amazon.nova-pro-v1:0` |

### MCP Gateway Targets

| Target | Runtime ID | Tools |
|--------|-----------|-------|
| `aws-infra-target` | `mcp_server-4kjU5oAHWM` | 8 (EC2, SSM, diagnose, remediate) |
| `monitoring-target` | `monitoring_server-CI86d62MYP` | 4 (CloudWatch metrics) |
| `sns-target` | `sns_server-2f8klN8rTF` | 4 (email, approvals) |
| `teams-target` | `teams_server-hbrhm38Ef3` | 2 (Teams messages) |

### Test Instances

| Instance ID | Name | Type | Public IP | Notes |
|-------------|------|------|-----------|-------|
| `i-0327d856931d3b38f` | test-4 | t2.nano | 13.236.119.112 | Primary test — SSM + CW Agent installed |
| `i-09c3bf01641fc3aa7` | CloudTask Bastion 2 | t2.micro | 3.27.60.68 | — |
| `i-0bf11b006e8f12844` | Cloud Task Backend 1 | t2.micro | N/A (private) | SSM slow (private subnet) |

### CloudWatch Alarms (test-4)

| Alarm | Threshold | Purpose |
|-------|-----------|---------|
| `devops-agent-high-cpu-31d3b38f` | 90% | CPU alarm for testing |
| `devops-agent-high-memory-31d3b38f` | 85% | Memory alarm |
| `devops-agent-high-disk-31d3b38f` | 90% | Disk alarm |

---

## 7. Deployment Scripts

### Deploy the Agent Container

Builds Docker image (arm64), pushes to ECR, updates AgentCore runtime.

```bash
# Full deploy (auto-versioned tag)
python scripts/deploy_agent.py deploy

# Explicit tag
python scripts/deploy_agent.py deploy --tag v20-myfix

# Dry run (preview without changes)
python scripts/deploy_agent.py deploy --dry-run

# Force clean build (no Docker cache)
python scripts/deploy_agent.py deploy --no-cache

# Check status
python scripts/deploy_agent.py status

# Tail logs
python scripts/deploy_agent.py logs
python scripts/deploy_agent.py logs --minutes 10

# Test invoke
python scripts/deploy_agent.py invoke "List all EC2 instances"
python scripts/deploy_agent.py invoke "Show CPU metrics for i-0327d856931d3b38f"

# Build + run locally (for testing without deploying)
python scripts/deploy_agent.py local
```

### Deploy MCP Servers

```bash
# Deploy all 4 MCP servers
python scripts/deploy_mcp_servers.py deploy

# Deploy specific server(s)
python scripts/deploy_mcp_servers.py deploy --servers aws_infra
python scripts/deploy_mcp_servers.py deploy --servers aws_infra,sns

# Check status of all servers
python scripts/deploy_mcp_servers.py status

# Tail logs for a specific server
python scripts/deploy_mcp_servers.py logs monitoring --minutes 10

# Dry run
python scripts/deploy_mcp_servers.py deploy --dry-run
```

### Deploy the Lambda Handler

The Lambda is a single Python file. Deploy via inline script:

```bash
python -c "
import boto3, zipfile, io
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as zf:
    zf.write('src/handlers/lambda_handler.py', 'lambda_handler.py')
buf.seek(0)
lam = boto3.client('lambda', region_name='ap-southeast-2')
lam.update_function_code(FunctionName='devops-ai-agent-handler', ZipFile=buf.read())
print('Lambda updated')
"
```

### Deploy the Approval Handler Lambda

```bash
python scripts/deploy_approval_handler.py deploy
```

---

## 8. Testing Scripts

### `test_remediation.py` — Primary Test Script

This is the main testing tool. It can start real stress processes on an instance via SSM, then trigger the alarm to test the full pipeline.

```bash
# ── MINOR CPU (1 stress process → agent auto-fixes → 1 "AUTO-FIXED" email) ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor

# ── MAJOR CPU (4 stress processes → agent requests approval → 1 "ACTION REQUIRED" email with APPROVE/REJECT links) ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario major

# ── Memory tests ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --type memory --scenario minor
python scripts/test_remediation.py -i i-0327d856931d3b38f --type memory --scenario major

# ── Disk tests ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --type disk --scenario minor

# ── Mock-only (no SSH stress, just force alarm state) ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor --mock-only

# ── Direct invoke (bypass Lambda, call agent directly) ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor --direct

# ── Dry run (show what would happen) ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor --dry-run

# ── Tail agent logs ──
python scripts/test_remediation.py --logs
python scripts/test_remediation.py --logs --minutes 10

# ── Restore alarm to OK after testing ──
python scripts/test_remediation.py -i i-0327d856931d3b38f --restore
```

**Important notes:**
- Always `--restore` the alarm between test runs
- The Lambda has a **10-minute dedup window** — if you re-run the same alarm type within 10 minutes, it will be skipped. Wait 10 minutes or use `--direct` to bypass
- `--direct` skips the stress startup step — stress processes must already be running
- MINOR stress runs for 120s, MAJOR for 240s

### `test_agent.py` — Quick Agent Test

```bash
python scripts/test_agent.py
```

### `test_automation.py` — Full E2E Automation

Lowers alarm thresholds, SSHes in to stress, waits for alarm to fire naturally, observes agent response. More comprehensive but slower.

### `validate_setup.py` — Validate AWS Setup

Checks that all AWS resources (IAM roles, ECR repos, runtimes) exist and are configured correctly.

```bash
python scripts/validate_setup.py
```

---

## 9. MCP Gateway Tools

The agent accesses 18 tools through the MCP gateway. During alarm processing, only 12 are routed (describe/list excluded to prevent confusion).

| # | Target | Tool | Key Params | Used In |
|---|--------|------|-----------|---------|
| 1 | aws-infra | `diagnose_instance_tool` | instance_id | Alarm (STEP 1) |
| 2 | aws-infra | `remediate_high_cpu_tool` | instance_id, pid (int) | MINOR CPU |
| 3 | aws-infra | `remediate_high_memory_tool` | instance_id, pid (int) | MINOR memory |
| 4 | aws-infra | `remediate_disk_full_tool` | instance_id | MINOR disk |
| 5 | aws-infra | `run_ssm_command_tool` | instance_id, shell_command | Ad-hoc |
| 6 | aws-infra | `describe_ec2_instance_tool` | instance_id | Chat only |
| 7 | aws-infra | `list_ec2_instances_tool` | state_filter, tag_filters | Chat only |
| 8 | aws-infra | `restart_ec2_instance_tool` | instance_id | After APPROVE |
| 9 | monitoring | `get_cpu_metrics_tool` | instance_id | Fallback |
| 10 | monitoring | `get_cpu_metrics_for_instances_tool` | instance_ids[] | Chat |
| 11 | monitoring | `get_memory_metrics_tool` | instance_id | Fallback |
| 12 | monitoring | `get_disk_usage_tool` | instance_id | Fallback |
| 13 | sns | `send_alert_with_failover_tool` | subject, message | MINOR notify |
| 14 | sns | `request_approval_tool` | instance_id, action_type, reason, details | MAJOR notify |
| 15 | sns | `check_approval_status_tool` | approval_id | After APPROVE |
| 16 | sns | `update_approval_status_tool` | approval_id, status | After APPROVE |
| 17 | teams | `send_teams_message_tool` | message | Chat |
| 18 | teams | `create_incident_notification_tool` | alarm_name, instance_id, etc. | Chat |

---

## 10. How the Alarm Pipeline Works

### MINOR Path (1 offending process → auto-fix)

```
CloudWatch Alarm (CPU > 90%)
    → EventBridge
    → Lambda
        → Dedup check (DynamoDB) — skip if same alarm within 10 min
        → Invoke AgentCore
    → Agent (Nova Pro)
        → STEP 1: diagnose_instance_tool  → finds 1 stress-ng at 92% CPU
        → STEP 2: Count offending processes → 1 (MINOR)
        → STEP 3a: remediate_high_cpu_tool(instance_id, pid) → kills process
        → STEP 3a: send_alert_with_failover_tool → 1 email "AUTO-FIXED: High CPU on i-xxx"
        → STOP (end_turn)
```

**Result:** Process killed, 1 email sent, alarm resolves.

### MAJOR Path (2+ offending processes → human approval)

```
CloudWatch Alarm (CPU > 90%)
    → EventBridge → Lambda → Dedup → Agent
    → Agent (Nova Pro)
        → STEP 1: diagnose_instance_tool → finds 4 stress-ng processes
        → STEP 2: Count offending processes → 4 (MAJOR)
        → STEP 3b: request_approval_tool → stores in DynamoDB, sends email with APPROVE/REJECT links
        → STOP (end_turn)

    ... Human clicks APPROVE link ...

    → API Gateway → Approval Handler Lambda
        → Updates DynamoDB status to "approved"
        → Invokes AgentCore agent with "APPROVED ACTION — EXECUTE IMMEDIATELY"
    → Agent
        → check_approval_status_tool → confirms approved
        → restart_ec2_instance_tool → restarts instance
        → update_approval_status_tool → marks as executed
        → send_alert_with_failover_tool → "EXECUTED: restart on i-xxx"
```

**Result:** 1 approval email with links, human approves, instance restarted, 1 confirmation email.

### Fallback Path (diagnose fails)

```
Agent → diagnose_instance_tool → ERROR (timeout/502)
      → get_cpu_metrics_tool → CloudWatch data
      → send_alert_with_failover_tool → "NEEDS ATTENTION: alarm on i-xxx"
      → STOP
```

---

## 11. Agent System Prompt & Workflow

The system prompt is in `deployments/agent/agent.py` (variable `SYSTEM_PROMPT`). It's ~60 lines, structured as:

- **STEP 1:** Always call `diagnose_instance_tool` first
- **STEP 2:** Count USER processes above 80% CPU (ignore system processes like ssm-agent, cloudwatch-agent)
- **STEP 3a (MINOR):** 1 offending process → remediate → send "AUTO-FIXED" email → stop
- **STEP 3b (MAJOR):** 2+ offending processes → `request_approval_tool` with PIDs and CPU% → stop
- **STEP 3c (fallback):** diagnose failed → get metrics → send "NEEDS ATTENTION" email → stop
- **RULES:** 1 email per alarm, PID as integer, stop after notification
- **CHAT MODE:** for non-alarm questions
- **APPROVED ACTIONS:** for executing after human approves

### Nova-specific Workarounds (in `NovaBedrockModel` class)

| Workaround | Why |
|-----------|-----|
| **Tool name sanitization** | MCP gateway names have hyphens (`aws-infra-target___tool`). Bedrock Converse API requires `[a-zA-Z0-9_]` only. Hyphens → underscores, with reverse name map. |
| **Intent-based tool routing** | Nova can't handle 18 tools at once. Alarm prompts get 12 tools (no describe/list). Chat gets category-based subsets. |
| **describe→diagnose interception** | Nova sometimes emits `describe_ec2_instance_tool` when it means `diagnose_instance_tool` (similar prefixes). The `_normalize_chunk` method intercepts and redirects during alarm mode. |
| **MAX_TURNS=4** | Hard cap enforced in `_stream()`. Each model call = 1 turn. After 4 turns, forces `end_turn`. Prevents runaway loops / email spam. Counter resets per invocation. |
| **Retry with backoff** | Up to 3 retries for "invalid tool-use sequence" errors with exponential backoff. |
| **No-tools fallback** | If all retries fail, one last call without any tools. |
| **`outputSchema` removal** | Stripped from tool specs (Bedrock Converse API doesn't support it). |

---

## 12. Fixes Applied (History)

### Session 1 (initial build)
- Built full architecture: CDK, Lambda, MCP servers, agent container

### Session 2 (root cause fixes)
- **Root Cause #1:** Hyphens in MCP tool names → "invalid tool-use sequence" error. Fixed with `sanitize_tool_name()`.
- **Root Cause #2:** Lambda sent "investigating" email + agent sent email = duplicates. Removed Lambda email.
- **Root Cause #3:** Nova calls `describe` when it means `diagnose`. Added intent-based routing, removed describe/list from alarm tools.

### Session 3 (stress test fixes — current)
- **Diagnose timeout:** `diagnose_instance()` ran 6 sequential SSM commands (180s). Rewritten to single combined SSM command with section markers (15-20s).
- **System prompt rewrite:** 210 lines → 60 lines. Short, positive, sequential steps instead of repetitive negative rules.
- **Nova Lite → Nova Pro:** Nova Lite couldn't follow even the simplified prompt. Nova Pro follows it correctly.
- **MAX_TURNS enforcement:** Variable existed but was never passed to anything. Implemented as turn counter in `NovaBedrockModel._stream()`.
- **Gateway URL fix:** Deploy script preserved stale env vars (gatewayv2). Fixed `update_runtime()` to accept `env_overrides` and deploy script passes `RUNTIME_ENV`.
- **Lambda dedup:** Added DynamoDB-based deduplication — same alarm skipped for 10 minutes. Uses existing `devops-agent-approvals` table with key prefix `alarm_dedup:`.
- **describe→diagnose interception:** `_normalize_chunk` intercepts tool calls in alarm mode and redirects describe to diagnose.
- **Process classification:** Prompt now explicitly lists system processes to ignore (ssm-agent, cloudwatch-agent, etc.) and states "5% CPU is NOT an offender."
- **PID details in emails:** Prompt requires MAJOR approval emails to list every offending PID with process name and CPU%.

---

## 13. Known Issues & Remaining Work

### Non-blocking Issues

| Issue | Details |
|-------|---------|
| **X-Ray 403 errors** | `xray:PutTraceSegments` denied in agent logs. Non-blocking — add permission to `devops-agent-runner` IAM role. |
| **CDK deploy broken** | `AWS_REGION` env var is reserved by Lambda runtime. Fix in `infra/stacks/agent_runner_stack.py`. |
| **CW Agent not on all instances** | `i-0bf11b006e8f12844` doesn't have CW Agent — memory/disk metrics return no data. |
| **Pre-commit hooks** | `.pre-commit-config.yaml` missing. Use `PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit ...` |
| **Python 3.14 locally** | Container uses 3.12. Minor typing differences. |

### Potential Improvements

| Improvement | Effort | Impact |
|-------------|--------|--------|
| **Switch to Claude** (if billing fixed) | Low | Claude follows multi-step tool workflows perfectly — all Nova workarounds become unnecessary |
| **Alarm dedup per-instance** | Low | Currently dedup is per alarm name. Could be per instance_id for multi-instance setups |
| **Memory/disk MINOR tests** | Medium | CPU path is fully tested. Memory and disk paths need validation |
| **Approval execution E2E test** | Medium | Click APPROVE link → verify agent restarts instance → verify confirmation email |
| **20-min reminder emails** | Low | `request_approval_tool` schedules a reminder. Verify it works and doesn't spam |
| **Multi-alarm handling** | Medium | What happens if CPU + memory alarms fire simultaneously? |
| **CloudWatch alarms for other instances** | Low | Only `i-0327d856931d3b38f` has alarms. Add for other instances |
| **Teams webhook integration** | Low | Currently falls over to SNS. Configure `TEAMS_WEBHOOK_URL` for Teams cards |
| **Web UI polish** | Medium | Chat interface works but could show alarm history, approval status, etc. |

---

## 14. Troubleshooting

### Agent not receiving alarms

1. Check Lambda logs: `aws logs tail /aws/lambda/devops-ai-agent-handler --since 5m`
2. Look for `DEDUP:` messages — alarm may be deduplicated (wait 10 minutes)
3. Look for `Ignoring non-ALARM state` — alarm may have transitioned to OK
4. Check EventBridge rule is enabled and targets the Lambda

### Agent calls wrong tool

- Check agent logs for `Nova tool confusion:` warning — the interception should redirect describe→diagnose
- If a NEW tool confusion appears, add similar interception in `_normalize_chunk`

### Agent sends multiple emails

- Check `MAX_TURNS` — should be 4 in `deployments/agent/agent.py`
- Check Lambda dedup — look for `DEDUP:` in Lambda logs
- Check if `request_approval_tool` was called (it sends email internally) AND `send_alert_with_failover_tool` was also called (that's a double-send)

### Agent says "no offending processes" but alarm is firing

- Stress processes may have ended before agent diagnosed
- t2.nano has 1 vCPU — 4 stress workers each get ~25%, none above 80% individually
- Check timing: stress starts → 15s wait → alarm forced → Lambda → agent → diagnose
- Use `aws ssm start-session --target i-0327d856931d3b38f` to verify stress is running

### Deploy fails

- ECR auth: run `aws ecr get-login-password --region ap-southeast-2 | docker login ...`
- Docker: ensure Docker Desktop is running and set to linux/arm64
- SSL errors with Python 3.14: first deploy attempt may timeout on SSL handshake, just retry

### Gateway URL wrong (agent hits v2 instead of v3)

- Fixed in `scripts/lib/runtime.py` — `update_runtime` now accepts `env_overrides`
- Verify after deploy: check agent startup logs for `gateway=https://devopsagentgatewayv3...`

---

## 15. Quick Reference Commands

```bash
# ── Environment ──────────────────────────────────────────────
cd C:\Users\pvhar\Work\devops-ai-agent
.venv\Scripts\activate

# ── Deploy ───────────────────────────────────────────────────
python scripts/deploy_agent.py deploy          # Agent container
python scripts/deploy_mcp_servers.py deploy    # All MCP servers
python scripts/deploy_mcp_servers.py deploy --servers aws_infra  # Specific server

# Lambda (inline deploy)
python -c "
import boto3, zipfile, io
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as zf:
    zf.write('src/handlers/lambda_handler.py', 'lambda_handler.py')
buf.seek(0)
lam = boto3.client('lambda', region_name='ap-southeast-2')
lam.update_function_code(FunctionName='devops-ai-agent-handler', ZipFile=buf.read())
print('Lambda updated')
"

# ── Status & Logs ────────────────────────────────────────────
python scripts/deploy_agent.py status
python scripts/deploy_agent.py logs --minutes 5
python scripts/deploy_mcp_servers.py status

# ── Test ─────────────────────────────────────────────────────
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario major
python scripts/test_remediation.py -i i-0327d856931d3b38f --restore
python scripts/test_remediation.py --logs --minutes 5

# Direct agent test (bypass Lambda)
python scripts/deploy_agent.py invoke "List all EC2 instances"
python scripts/deploy_agent.py invoke "Show CPU metrics for i-0327d856931d3b38f"

# ── Web App ──────────────────────────────────────────────────
python web/app.py   # http://127.0.0.1:5001

# ── SSM (shell into instance, no SSH key needed) ─────────────
aws ssm start-session --target i-0327d856931d3b38f --region ap-southeast-2

# ── Git (pre-commit hooks missing) ───────────────────────────
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add -A
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "message"
git push origin feature/testing
```

---

## Appendix: Changing the Model

To switch models, change the `MODEL_ID` in TWO files:

1. `deployments/agent/agent.py` line ~43:
   ```python
   MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-pro-v1:0")
   ```

2. `scripts/lib/config.py` line ~56:
   ```python
   MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-pro-v1:0")
   ```

Then redeploy: `python scripts/deploy_agent.py deploy`

If switching to Claude (requires billing fix):
- Use `anthropic.claude-3-5-haiku-20241022-v1:0` (fast, cheap) or `anthropic.claude-3-5-sonnet-20241022-v2:0` (better)
- Add the model ARN to `deployments/agent/permissions-policy.json`
- Most Nova workarounds (tool interception, MAX_TURNS) become unnecessary with Claude but won't cause harm if left in

Available models in `permissions-policy.json`:
- `amazon.nova-lite-v1:0` ← was the default, too weak
- `amazon.nova-pro-v1:0` ← current default, works well