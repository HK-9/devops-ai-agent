# DevOps AI Agent — Session Context

> Use this file to bootstrap a new AI assistant session if the conversation
> gets too long. Paste its contents as the first message.
>
> **Last updated:** 2026-04-02 (mid-testing session)

---

## Project Overview

- **Repo:** `devops-ai-agent` on branch `feature/testing`
- **Stack:** Python 3.14, Flask web UI, Strands Agents SDK (`strands-agents==1.34.0`), AWS Bedrock, MCP (Model Context Protocol)
- **Region:** `ap-southeast-2`
- **Agent Runtime ARN:** `arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy`
- **MCP Gateway:** `https://devopsagentgatewayv3-ar4lmz2x6t.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp`
- **Default Model:** `amazon.nova-lite-v1:0`
- **Notification email:** `kash8580@gmail.com` (via SNS topic `arn:aws:sns:ap-southeast-2:650251690796:devops-agent-alerts`)

---

## Architecture (Current — Refactored)

```
Web App (web/agent.py) ──── thin client ────┐
                                            │  invoke_agent_runtime
                                            ▼
                              Deployed Agent Container
                              (deployments/agent/agent.py)
                                - Nova workarounds
                                - Tool name sanitization
                                - Intent-based tool routing
                                - Retry logic
                                - System prompt
                                            │
                                            ▼
                              MCP Gateway (18 tools)
                              ├── aws-infra-target (8 tools)
                              ├── monitoring-target (4 tools)
                              ├── sns-target (4 tools)
                              └── teams-target (2 tools)

CloudWatch Alarm → EventBridge → Lambda (src/handlers/lambda_handler.py)
                                    → invoke_agent_runtime → same Agent
```

### Web App Modes (`web/agent.py`)
- **Remote (production):** `AGENT_RUNTIME_ARN` set in `.env` → calls deployed agent via `invoke_agent_runtime`
- **Local (dev fallback):** `AGENT_RUNTIME_ARN` not set → imports from `deployments/agent/agent.py` and runs locally

### How to Start the Web App

```bash
cd C:\Users\pvhar\Work\devops-ai-agent
.venv\Scripts\activate
python web/app.py
# Runs on http://127.0.0.1:5001
```

**Important:** Must run from the project root, NOT from `web/`.

---

## ROOT CAUSE #1 — "Invalid Tool-Use Sequence" Error

### The Problem

Nova models through Bedrock Converse API fail with:
```
ModelErrorException: Model produced invalid sequence as part of ToolUse
```

### Root Cause: HYPHENS in MCP Gateway Tool Names

MCP Gateway returns names like `aws-infra-target___list_ec2_instances_tool`.
Bedrock Converse API expects `[a-zA-Z][a-zA-Z0-9_]*` — **no hyphens**.
Claude tolerates hyphens; Nova does not.

### Proof (direct boto3)

| Test | Result |
|------|--------|
| Nova + `aws-infra-target___list_ec2_instances_tool` (hyphen) | ❌ Always fails |
| Nova + `aws_infra_target___list_ec2_instances_tool` (underscore) | ✅ Works |
| Nova + 18 tools (all hyphens) | ❌ Always fails |
| Claude + 18 tools (hyphens) | ✅ Works |

### Fix Applied

`deployments/agent/agent.py` → `sanitize_tool_name()` replaces hyphens with
underscores. `NovaBedrockModel._normalize_chunk()` reverse-maps sanitized
names back to originals so the Strands SDK registry can find the tools.

---

## ROOT CAUSE #2 — Duplicate Emails

### The Problem
Two emails sent per alarm: one "investigating" from Lambda, one from agent.

### Root Cause
`src/handlers/lambda_handler.py` called `_send_sns()` with "DevOps Agent is
investigating..." BEFORE invoking the agent. The agent then sent its own
notification after finishing. Result: 2 emails.

### Fix Applied
Removed the `_send_sns()` call from the Lambda handler. Agent sends exactly
ONE email per alarm (AUTO-FIXED for MINOR, approval email for MAJOR).

---

## ROOT CAUSE #3 — Agent Calls Wrong Tool (describe instead of diagnose)

### The Problem (CURRENT — NOT YET FIXED)
When an alarm fires, the agent repeatedly calls `describe_ec2_instance_tool`
in a loop instead of `diagnose_instance_tool`. This is the **#1 blocker**.

### Root Cause (partially fixed, still has issues)
The intent-based tool routing for alarm prompts was only sending monitoring
tools (4 tools), missing `diagnose_instance_tool` from the ec2 category.

**Fix applied:** Added `ALARM_CATEGORIES = ["ec2", "monitoring", "remediation", "approval"]`
so alarm prompts get 15 tools including diagnose.

**Remaining issue:** Even with 15 tools, the Nova model is choosing
`describe_ec2_instance_tool` instead of `diagnose_instance_tool` and looping.
The model's `<thinking>` says "I should call diagnose_instance_tool" but then
actually calls `describe_ec2_instance_tool`. This may be because:
1. With 15 tools, Nova is still confused about tool selection
2. The sanitized tool name mapping might have an issue where `diagnose` tool
   gets a different sanitized name than what the model outputs
3. Nova may need the system prompt to be even more explicit about WHICH tool
   name to use (including the gateway prefix)

**THIS IS THE NEXT THING TO FIX.** See "Immediate Next Steps" below.

---

## Why the Colleague's Setup Worked

The colleague used a **completely different architecture**:

- **Old code** (commits `8fb3583`, `a6a07be`): `web/routes.py` imported
  `DevOpsAgent` from `src/agent/agent_core.py`, which calls
  `invoke_inline_agent` (Bedrock AgentCore API). Tool orchestration handled
  **server-side** — model never sees tool specs, so name format doesn't matter.
- `web/agent.py` was **never committed** by the colleague (it was untracked).
- The commit `ed8e328` defaults to Claude Sonnet 4 (tolerates hyphens).

### Claude Access Blocked
This user's AWS account has `INVALID_PAYMENT_INSTRUMENT` billing error blocking
all Anthropic models. Nova (Amazon's own) is unaffected.

---

## All Fixes Applied (in `deployments/agent/agent.py`)

All agent logic lives in ONE file: `deployments/agent/agent.py`.
`web/agent.py` is a thin client that imports from it.

### 1. Tool Name Sanitization (`sanitize_tool_name`)
Replaces hyphens with underscores. Reverse name map restores originals
in model responses so Strands SDK registry can find them.

### 2. Remove `outputSchema`
Stripped during sanitization (Bedrock Converse API doesn't support it).

### 3. Intent-Based Tool Routing
| Category | Keywords | Tools |
|----------|----------|-------|
| `ec2` | list, instances, ec2, describe, show, running, stopped | 4 |
| `monitoring` | cpu, memory, disk, metric, usage, monitor, health | 4 |
| `remediation` | remediate, fix, kill, ssm, run command, shell | 4 |
| `notification` | teams, notify, alert, incident, send message | 3 |
| `approval` | approval, approve | 3 |

**Alarm prompts** get `ALARM_CATEGORIES = ["ec2", "monitoring", "remediation", "approval"]` → 15 tools.
Default: `ec2` + `monitoring` (8 tools).

### 4. Retry with Exponential Backoff (3 attempts)
### 5. No-Tools Fallback (last resort)
### 6. Agent Reset on Persistent Failure

### 7. System Prompt — MINOR/MAJOR Rules (Fixed)
- **MINOR** (1 offending process): diagnose → fix → 1 email (AUTO-FIXED) → STOP
- **MAJOR** (2+ processes): diagnose → `request_approval_tool` → STOP (1 email with APPROVE/REJECT links)
- `request_approval_tool` sends the email internally — do NOT also call `send_alert_with_failover_tool`
- **ONE email per alarm, no exceptions**

### 8. Lambda Fix (`src/handlers/lambda_handler.py`)
Removed the "DevOps Agent is investigating..." SNS email that caused duplicates.

---

## MCP Gateway Tools (18 total)

| # | Target | Tool Name | Key Params |
|---|--------|-----------|------------|
| 1 | aws-infra-target | describe_ec2_instance_tool | instance_id |
| 2 | aws-infra-target | diagnose_instance_tool | instance_id |
| 3 | aws-infra-target | list_ec2_instances_tool | state_filter, max_results, tag_filters |
| 4 | aws-infra-target | remediate_disk_full_tool | instance_id |
| 5 | aws-infra-target | remediate_high_cpu_tool | instance_id, pid |
| 6 | aws-infra-target | remediate_high_memory_tool | instance_id, pid |
| 7 | aws-infra-target | restart_ec2_instance_tool | instance_id |
| 8 | aws-infra-target | run_ssm_command_tool | instance_id, shell_command |
| 9 | monitoring-target | get_cpu_metrics_for_instances_tool | instance_ids (array) |
| 10 | monitoring-target | get_cpu_metrics_tool | instance_id |
| 11 | monitoring-target | get_disk_usage_tool | instance_id |
| 12 | monitoring-target | get_memory_metrics_tool | instance_id |
| 13 | sns-target | check_approval_status_tool | approval_id |
| 14 | sns-target | request_approval_tool | action_type, instance_id, reason |
| 15 | sns-target | send_alert_with_failover_tool | message, subject |
| 16 | sns-target | update_approval_status_tool | approval_id, status |
| 17 | teams-target | create_incident_notification_tool | alarm_name, instance_id, metric_value, severity, summary |
| 18 | teams-target | send_teams_message_tool | message |

---

## Key Files

| File | Purpose |
|------|---------|
| `deployments/agent/agent.py` | **Single source of truth** — all agent logic: Nova workarounds, tool sanitization, intent routing, system prompt, retry logic. Deployed as container on AgentCore. |
| `web/agent.py` | **Thin client** — calls deployed agent via `invoke_agent_runtime` (remote) or imports from `deployments/agent/agent.py` (local dev fallback) |
| `web/app.py` | Flask app factory, loads `.env`, runs on port 5001 |
| `web/routes.py` | Flask routes — `/chat` UI, `/api/chat` API, prompt augmentation with tool hints |
| `src/handlers/lambda_handler.py` | Lambda: EventBridge alarm → parses event → calls `invoke_agent_runtime` |
| `src/agent/agent_core.py` | OLD architecture — `DevOpsAgent` using `invoke_inline_agent` (not used) |
| `scripts/deploy_agent.py` | Deploy agent container: build → push ECR → update runtime |
| `scripts/test_remediation.py` | Test script: mock CloudWatch alarms or stress+mock for MINOR/MAJOR scenarios |
| `scripts/test_automation.py` | Full E2E test: lower thresholds → SSH stress → wait for alarm → observe agent |
| `scripts/lib/config.py` | Shared config: region, account, gateway, runtime IDs |
| `docs/AGENT-CONTEXT.md` | This file |

---

## Changes Made This Session

### Committed & Pushed to `feature/testing`

1. `a3bb077` — Moved `deploy_*` directories → `deployments/`
2. `d7c6ccf` — Relocated project files to proper directories
3. `91d7f4c` — Updated infra stacks, deploy scripts, lambda handler
4. `ed8e328` — Added `web/agent.py` (initially with Claude model ID)
5. `258e485` — Fix: sanitize MCP tool names for Nova compatibility
6. `ed70c0e` — Refactor: centralize agent logic, make web app thin client

### Deployed

- Agent container: `v16-ed70c0e` (latest with all fixes) — **READY**
- Lambda `devops-ai-agent-handler`: updated with duplicate email fix

### Uncommitted (need to commit + push)

- `deployments/agent/agent.py` — Latest system prompt fixes (MINOR/MAJOR rules, one email), alarm category routing
- `src/handlers/lambda_handler.py` — Removed "investigating" SNS email
- `scripts/test_remediation.py` — New test script for mock alarm triggers
- `docs/AGENT-CONTEXT.md` — This file

### Dependency Fixes (in venv)
- Upgraded `click` 7.1.2 → 8.3.1
- Installed `strands-agents==1.34.0`

---

## Environment Details

| Item | Value |
|------|-------|
| Python | 3.14.3 (bleeding edge — may cause issues with some packages) |
| OS | Windows |
| Shell | PowerShell |
| strands-agents | 1.34.0 |
| boto3 / botocore | 1.42.67 |
| mcp | 1.26.0 |
| Flask | 3.1.3 |
| AWS Region | ap-southeast-2 |
| Claude access | ❌ Blocked — `INVALID_PAYMENT_INSTRUMENT` billing error |
| Nova Lite/Pro access | ✅ Works (with tool name sanitization) |

---

## Immediate Next Steps (BLOCKERS)

### 🔴 BLOCKER: Nova calls `describe` instead of `diagnose` in alarm flow

When an alarm fires, the agent's `<thinking>` says "I should call
diagnose_instance_tool" but then actually outputs `describe_ec2_instance_tool`
and loops. The full pipeline works (EventBridge → Lambda → Agent) but the
agent picks the wrong tool.

**Hypothesis:** With 15 sanitized tools, Nova is confused between
`aws_infra_target___describe_ec2_instance_tool` and
`aws_infra_target___diagnose_instance_tool` (similar prefixes).

**Possible fixes to try:**
1. Reduce alarm tools further — maybe only send `diagnose_instance_tool` +
   remediation tools + approval (skip `describe` and `list` for alarms)
2. Make the system prompt even more explicit: "For alarms, your FIRST tool
   call MUST be `diagnose_instance_tool`. Do NOT call `describe_ec2_instance_tool`."
3. Check if the reverse name mapping is causing the issue — maybe the model
   outputs the sanitized name but the SDK resolves it to the wrong original
4. Try Nova Pro instead of Nova Lite for alarm handling (more capable)
5. Test with only 5-6 tools for alarms instead of 15

### Other Known Issues

1. **AWS billing** — Fix payment instrument to unlock Claude (best long-term fix)
2. **CDK deploy broken** — `AWS_REGION` env var is reserved by Lambda runtime; CDK stack needs fix in `agent_runner_stack.py` line ~130
3. **`strands-agents-tools`** — Listed in requirements but not installed (not needed)
4. **Python 3.14** — Colleague likely used 3.12/3.13
5. **Pre-commit hooks** — Missing `.pre-commit-config.yaml`; use `PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit ...`
6. **X-Ray permissions** — Non-blocking `xray:PutTraceSegments` 403 errors in agent logs; add IAM permission to `devops-agent-runner` role
7. **Diagnose/SSM timeout** — `diagnose_instance_tool` sometimes returns 502 due to SSM command taking too long; may need to increase agent container timeout

---

## Useful Commands

```bash
# Start web app
cd C:\Users\pvhar\Work\devops-ai-agent
.venv\Scripts\activate
python web/app.py

# Deploy agent (build → push ECR → update runtime)
python scripts/deploy_agent.py deploy
python scripts/deploy_agent.py status
python scripts/deploy_agent.py logs --minutes 5

# Test agent directly
python scripts/deploy_agent.py invoke "List all EC2 instances"
python scripts/deploy_agent.py invoke "Diagnose instance i-0327d856931d3b38f"

# Update Lambda code (handler is 'lambda_handler.handler')
python -c "
import boto3, zipfile, io
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as zf:
    zf.write('src/handlers/lambda_handler.py', 'lambda_handler.py')
buf.seek(0)
lam = boto3.client('lambda', region_name='ap-southeast-2')
lam.update_function_code(FunctionName='devops-ai-agent-handler', ZipFile=buf.read())
"

# Test remediation (mock alarm trigger)
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor --mock-only
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario major --mock-only
python scripts/test_remediation.py --logs --minutes 5
python scripts/test_remediation.py -i i-0327d856931d3b38f --restore

# Commit bypassing pre-commit
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add -A
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "message"
git push origin feature/testing
```

## Test Instances

| Instance ID | Type | Name | Public IP | Notes |
|-------------|------|------|-----------|-------|
| `i-0327d856931d3b38f` | t2.nano | test-4 | 13.236.119.112 | Primary test instance, has SSM agent + CW Agent |
| `i-09c3bf01641fc3aa7` | t2.micro | CloudTask Bastion 2 | 3.27.60.68 | |
| `i-0bf11b006e8f12844` | t2.micro | Cloud Task Backend 1 | N/A (private) | |

## CloudWatch Alarms for test-4

| Alarm | Threshold | State |
|-------|-----------|-------|
| `devops-agent-high-cpu-31d3b38f` | 90% | OK |
| `devops-agent-high-memory-31d3b38f` | 85% | OK |
| `devops-agent-high-disk-31d3b38f` | 90% | OK |
```
