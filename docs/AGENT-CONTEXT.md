# DevOps AI Agent — Session Context

> Use this file to bootstrap a new AI assistant session if the conversation
> gets too long. Paste its contents as the first message.

---

## Project Overview

- **Repo:** `devops-ai-agent` on branch `feature/testing`
- **Stack:** Python 3.14, Flask web UI, Strands Agents SDK (`strands-agents==1.34.0`), AWS Bedrock, MCP (Model Context Protocol)
- **Architecture:** Flask app (`web/app.py`) → Strands Agent (`web/agent.py`) → AgentCore MCP Gateway (18 tools across 5 targets) → Bedrock model
- **Gateway URL:** `https://devopsagentgatewayv3-ar4lmz2x6t.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp`
- **Region:** `ap-southeast-2`

---

## How to Start the Web App

```bash
cd C:\Users\pvhar\Work\devops-ai-agent
.venv\Scripts\activate
python web/app.py
# Runs on http://127.0.0.1:5001
```

**Important:** Must run from the project root, NOT from `web/`.

---

## DEFINITIVE ROOT CAUSE — "Invalid Tool-Use Sequence" Error

### The Problem

When using Amazon Nova models (Lite or Pro) through the Bedrock Converse API,
every query fails with:

```
ModelErrorException: Model produced invalid sequence as part of ToolUse
```

### Root Cause: HYPHENS in MCP Gateway Tool Names

The MCP Gateway returns tool names like:

```
aws-infra-target___list_ec2_instances_tool
monitoring-target___get_cpu_metrics_tool
sns-target___send_alert_with_failover_tool
```

These names contain **hyphens** (`-`). The Bedrock Converse API expects tool
names matching `[a-zA-Z][a-zA-Z0-9_]*` — **no hyphens allowed**.

- **Claude models** are lenient about this and work fine.
- **Nova models** strictly choke and produce malformed tool-use JSON output.

### Proof (direct boto3, no Strands SDK involved)

| Test | Result |
|------|--------|
| Nova + tool named `aws-infra-target___list_ec2_instances_tool` (hyphen) | ❌ **Always fails** |
| Nova + tool named `aws_infra_target___list_ec2_instances_tool` (underscore) | ✅ **Works** |
| Nova + 1 simple tool (no hyphens) | ✅ Works |
| Nova + 18 MCP tools (all have hyphens) | ❌ Always fails |
| Nova + 4 filtered MCP tools (still have hyphens) | ❌ Still fails |
| Claude + 18 MCP tools (hyphens don't matter) | ✅ Works |

### Secondary Issue: Too Many Tools

Even after fixing hyphens, Nova models struggle with 18 tools at once.
The fix also includes intent-based tool routing to send only 4-8 relevant
tools per query.

### Why the Colleague's Setup Worked

The colleague used a **completely different architecture**:

- **Old code** (commits `8fb3583`, `a6a07be`): `web/routes.py` imported
  `DevOpsAgent` from `src/agent/agent_core.py`, which calls
  `invoke_inline_agent` (Bedrock AgentCore API). This API handles tool
  orchestration **server-side** — the model never receives tool specs directly,
  so tool name format doesn't matter.
- **New code** (`web/agent.py`, using Strands SDK): Calls the Bedrock
  `converse()` API directly, passing all tool specs in the request. Nova
  receives the hyphenated names and breaks.
- `web/agent.py` was **never committed** by the colleague (it was untracked).
  She likely used Claude on her machine (which tolerates hyphens).
- The commit `ed8e328` on `origin/feature/testing` shows `MODEL_ID` defaulting
  to `apac.anthropic.claude-sonnet-4-20250514-v1:0` (Claude).

### Claude Access Blocked

This user's AWS account has an `INVALID_PAYMENT_INSTRUMENT` billing error
that blocks all Anthropic/third-party models via Bedrock Marketplace.
Nova models (Amazon's own) are unaffected by this.

---

## The Fix (in `web/agent.py`)

### 1. Tool Name Sanitization (`_sanitize_tool_name`)

Replaces hyphens and any non-alphanumeric/underscore characters with
underscores:

```
aws-infra-target___list_ec2_instances_tool
→ aws_infra_target___list_ec2_instances_tool
```

### 2. Remove `outputSchema`

MCP gateway tools include an `outputSchema` field that the Bedrock Converse
API does not support. This is stripped during sanitization.

### 3. Intent-Based Tool Routing

Instead of sending all 18 tools, detects query intent and routes only
relevant tools:

| Category | Keywords | Tools (~count) |
|----------|----------|----------------|
| `ec2` | list, instances, ec2, describe, show, running, stopped | 4 |
| `monitoring` | cpu, memory, disk, metric, usage, monitor, health | 4 |
| `remediation` | remediate, fix, kill, ssm, run command, shell | 4 |
| `notification` | teams, notify, alert, incident, send message | 3 |
| `approval` | approval, approve | 3 |

Default (no intent detected): `ec2` + `monitoring` (8 tools).

Routing uses **thread-local storage** (`_tool_route`) to pass hints from
`invoke()` → `_NovaBedrockModel._stream()`.

### 4. Retry with Exponential Backoff

If tool-use error still occurs, retries up to 3 times (1s → 2s → 4s).
Configurable via `NOVA_MAX_RETRIES` and `NOVA_RETRY_DELAY` env vars.

### 5. Graceful No-Tools Fallback

After all retries fail, one final attempt without tools so the user
gets a text answer instead of a 500 error.

### 6. Agent Reset on Persistent Failure

If the agent's internal conversation state is corrupted, the singleton
is destroyed so the next request starts fresh.

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
| `web/app.py` | Flask app factory, loads `.env`, runs on port 5001 |
| `web/agent.py` | Strands Agent wrapper — tool name sanitization, intent-based routing, Nova workarounds, SigV4 auth, retry logic |
| `web/routes.py` | Flask routes — `/chat` UI, `/api/chat` API, prompt augmentation with tool hints |
| `web/templates/` | Jinja2 templates for the web UI |
| `src/agent/agent_core.py` | OLD architecture — `DevOpsAgent` using `invoke_inline_agent` (not used by current `web/agent.py`) |
| `docs/AGENT-CONTEXT.md` | This file |

---

## Changes Made (This Session — NOT all committed yet)

### Committed (4 commits pushed to `feature/testing`)

1. `a3bb077` — Moved `deploy_*` directories → `deployments/agent/` and `deployments/mcp_servers/`
2. `d7c6ccf` — Relocated `SESSION-LOG.md`, `demo.py`, `validate_setup.py`, `test_event.json`
3. `91d7f4c` — Updated infra stacks, deploy scripts, and lambda handler
4. `ed8e328` — Added `web/agent.py` (initially with Claude model ID)

### Uncommitted (need to commit + push)

- **`web/agent.py`** — Major rewrite with:
  - Tool name sanitization (hyphens → underscores) — THE key fix
  - `outputSchema` removal
  - Intent-based tool routing (`TOOL_CATEGORIES`, `INTENT_KEYWORDS`, `_detect_categories`)
  - Thread-local `_tool_route` for routing hints
  - `_NovaBedrockModel` with retry logic + no-tools fallback
  - `_reset_agent()` for recovery
  - Default `MODEL_ID` changed to `amazon.nova-lite-v1:0`
  - `max_tokens` increased to 8192
  - `streaming` changed to `True`
- **`docs/AGENT-CONTEXT.md`** — This file

### Dependency Fixes (done in venv, not committed)

- Upgraded `click` 7.1.2 → 8.3.1 (Flask 3.1.3 requires Click 8+)
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

## Known Issues / TODOs

1. **AWS billing** — Fix payment instrument to unlock Claude models (best long-term fix; Claude handles all 18 tools without any workarounds)
2. **`strands-agents-tools`** — Listed in `requirements.txt` but NOT installed; not needed since all tools come from MCP gateway
3. **Python 3.14** — Very new; colleague likely used 3.12/3.13. Some packages may have compatibility issues.
4. **Tool routing edge cases** — Queries spanning many categories may need more than 8 tools.
5. **Uncommitted changes** — `web/agent.py` and `docs/AGENT-CONTEXT.md` need to be committed and pushed.
6. **Pre-commit hooks** — `.pre-commit-config.yaml` is missing; use `PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit ...` to bypass.
7. **Consider reverting to old architecture** — The `invoke_inline_agent` approach in `src/agent/agent_core.py` avoids tool name issues entirely since AgentCore handles tool orchestration server-side.

---

## Useful Commands

```bash
# Start web app
cd C:\Users\pvhar\Work\devops-ai-agent
.venv\Scripts\activate
python web/app.py

# Commit bypassing pre-commit
PRE_COMMIT_ALLOW_NO_CONFIG=1 git add web/agent.py docs/AGENT-CONTEXT.md
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m "fix: sanitize tool names for Nova compatibility

Root cause: MCP gateway tool names contain hyphens (e.g. aws-infra-target___...)
which Nova models cannot handle in the Bedrock Converse API. Replace hyphens with
underscores. Also adds intent-based tool routing and retry logic."
git push origin feature/testing

# Test Bedrock directly
python -c "
import boto3
c = boto3.client('bedrock-runtime', region_name='ap-southeast-2')
r = c.converse(
    modelId='amazon.nova-lite-v1:0',
    messages=[{'role':'user','content':[{'text':'Hello'}]}],
    inferenceConfig={'maxTokens':512}
)
print(r['output']['message']['content'][0]['text'])
"
```
