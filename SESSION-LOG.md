# DevOps AI Agent — Build & Deploy Session Log

> **Date:** March 8, 2026
> **Region:** `ap-southeast-2`
> **Account:** `650251690796`
> **Instance Monitored:** `i-0bf11b006e8f12844`

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Initial Audit](#2-initial-audit)
3. [Implementing the Agent Reasoning Loop](#3-implementing-the-agent-reasoning-loop)
4. [Lambda Dependency Packaging](#4-lambda-dependency-packaging)
5. [Cross-Platform Wheel Fix (Linux)](#5-cross-platform-wheel-fix-linux)
6. [Bedrock Agent Configuration](#6-bedrock-agent-configuration)
7. [Inline Agent Migration](#7-inline-agent-migration)
8. [IAM Permission Fixes](#8-iam-permission-fixes)
9. [Bedrock Parameter Quota Fix](#9-bedrock-parameter-quota-fix)
10. [Model Access Resolution](#10-model-access-resolution)
11. [Inline Agent API Differences](#11-inline-agent-api-differences)
12. [Final Working State](#12-final-working-state)
13. [Commands Reference](#13-commands-reference)
14. [Remaining Items](#14-remaining-items)

---

## 1. Project Overview

The DevOps AI Agent is an event-driven system that automatically responds to AWS infrastructure alerts. The full pipeline is:

```
CloudWatch Alarm → EventBridge Rule → Lambda → Bedrock Agent → MCP Tools → AWS Actions → Teams Notification
```

### Architecture

| Component | Technology |
|-----------|-----------|
| Infrastructure as Code | AWS CDK (Python) |
| Agent Runtime | AWS Bedrock `invoke_inline_agent` |
| Foundation Model | `amazon.nova-lite-v1:0` |
| Tool Protocol | MCP (Model Context Protocol) via stdio |
| Lambda Runtime | Python 3.12 |
| Notification | Microsoft Teams (Adaptive Cards) |

### MCP Servers & Tools (9 total)

| Server | Tools |
|--------|-------|
| `aws-infra` | `list_ec2_instances`, `describe_ec2_instance`, `restart_ec2_instance` |
| `monitoring` | `get_cpu_metrics`, `get_cpu_metrics_for_instances`, `get_memory_metrics`, `get_disk_usage` |
| `teams` | `send_teams_message`, `create_incident_notification` |

### CDK Stacks

| Stack | Purpose |
|-------|---------|
| `DevOpsAgent-Networking` | VPC and networking resources |
| `DevOpsAgent-Monitoring` | CloudWatch alarm + EventBridge rule |
| `DevOpsAgent-Runner` | Lambda function + IAM policies |

---

## 2. Initial Audit

Ran a comprehensive audit of every file in the project. Identified 12 missing or incomplete pieces:

| # | Issue | Priority |
|---|-------|----------|
| 1 | `_handle_reasoning_loop` was a stub (no real Bedrock calls) | Critical |
| 2 | No `requirements-lambda.txt` for Lambda dependency bundling | Critical |
| 3 | Lambda packaging didn't include pip dependencies | Critical |
| 4 | Bedrock agent not registered / env vars not set | High |
| 5 | IAM policy missing Bedrock permissions | High |
| 6 | `TEAMS_WEBHOOK_URL` not configured | Medium |
| 7 | `asyncio.get_event_loop()` deprecated usage | Low |
| 8 | Settings singleton not reloaded in tests | Low |
| 9 | No CLI test harness script | Medium |
| 10 | System prompt needed tuning | Low |
| 11 | `cdk.json` context values needed | Medium |
| 12 | MCP tool schemas needed Bedrock format conversion | Critical |

---

## 3. Implementing the Agent Reasoning Loop

**File:** `src/agent/agent_core.py`

Replaced the stub `_handle_reasoning_loop` with a full implementation that:

- Supports two modes: **registered agent** (`invoke_agent`) and **inline agent** (`invoke_inline_agent`)
- Processes the Bedrock event stream (text chunks, `returnControl`, trace events)
- Routes tool calls through the MCP client
- Feeds results back via `returnControlInvocationResults`
- Iterates up to `max_reasoning_turns` (default 10)

### Key Methods Added

| Method | Purpose |
|--------|---------|
| `_handle_reasoning_loop()` | Main reasoning loop with Bedrock |
| `_build_invoke_kwargs()` | Builds API call parameters for either mode |
| `_build_action_groups()` | Converts MCP tool schemas → Bedrock action group format |
| `_map_json_type_to_bedrock()` | Maps JSON Schema types to Bedrock types |
| `_cast_parameter()` | Casts stringified params back to Python types |

---

## 4. Lambda Dependency Packaging

**Problem:** Lambda invocation failed with `No module named 'pydantic_settings'` — dependencies weren't bundled.

### Solution

**Created** `requirements-lambda.txt`:

```
mcp>=1.0.0
httpx>=0.27.0
pydantic>=2.7.0
pydantic-settings>=2.3.0
```

**Updated** `infra/stacks/agent_runner_stack.py` with a pre-build bundling approach:

```python
def _build_lambda_bundle() -> str:
    bundle = _BUNDLE_DIR
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    subprocess.check_call([
        "pip", "install",
        "-r", str(req_file),
        "-t", str(bundle),
        "--quiet",
        "--disable-pip-version-check",
        "--platform", "manylinux2014_x86_64",
        "--only-binary=:all:",
        "--implementation", "cp",
        "--python-version", "3.12",
    ])

    shutil.copytree(str(src_dir), str(bundle / "src"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return str(bundle)
```

**Updated** `.gitignore` to include `.lambda-bundle/`.

### Why Not Docker Bundling?

We tried three approaches:
1. **CDK Docker bundling** — failed with `ENOENT` (Docker not installed)
2. **`ILocalBundling`** — failed with jsii serialization error
3. **Pre-build function** (winner) — runs `pip install` + `shutil.copytree` at synth time

---

## 5. Cross-Platform Wheel Fix (Linux)

**Problem:** After deploying, Lambda failed with `No module named 'pydantic_core._pydantic_core'` — Windows `.whl` files were installed but Lambda runs Linux.

**Fix:** Added platform targeting flags to pip:

```
--platform manylinux2014_x86_64
--only-binary=:all:
--implementation cp
--python-version 3.12
```

This ensures pip downloads Linux x86_64 binary wheels regardless of the build machine's OS.

---

## 6. Bedrock Agent Configuration

### Registered Agent Attempt

Verified a Bedrock agent existed:

```powershell
aws bedrock-agent list-agents --region ap-southeast-2
# Agent ID: KYZ4EKSMX5, Status: PREPARED

aws bedrock-agent list-agent-aliases --agent-id KYZ4EKSMX5 --region ap-southeast-2
# Alias ID: LFVTIWMNFK
```

Set environment variables in the Lambda:

```python
"AGENT_ID": "KYZ4EKSMX5",
"AGENT_ALIAS_ID": "LFVTIWMNFK",
```

**Result:** `resourceNotFoundException` — despite the agent existing and being in PREPARED status, `invoke_agent` consistently returned "ARN not found."

Tried broadening IAM to `resources=["*"]` — same error.

---

## 7. Inline Agent Migration

Since registered agent mode failed, switched to **inline agent** mode:

**Removed** `AGENT_ID` and `AGENT_ALIAS_ID` from Lambda environment variables.

When these are absent, the code uses `invoke_inline_agent` instead, which sends the system prompt and action groups inline with each request (no pre-registration needed).

---

## 8. IAM Permission Fixes

### Fix 1: Add `bedrock:InvokeInlineAgent`

**Error:**
```
AccessDeniedException: User is not authorized to perform: bedrock:InvokeInlineAgent
```

**Fix:** Added the action to the IAM policy:

```python
actions=[
    "bedrock:InvokeAgent",
    "bedrock:InvokeInlineAgent",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
]
```

### Fix 2: Broaden to `bedrock:*`

Even after adding specific actions, a different `accessDeniedException` occurred (from Bedrock's service side, not IAM). Temporarily broadened to:

```python
actions=["bedrock:*"]
```

> **Note:** This should be scoped down in production.

---

## 9. Bedrock Parameter Quota Fix

**Error:**
```
ServiceQuotaExceededException: The maximum number of resources is 5,
but account requested 6 resources.
```

**Cause:** The `create_incident_notification` tool has 6 parameters, but Bedrock inline agents limit each function to 5.

**Fix:** Added parameter capping in `_build_action_groups()`:

```python
MAX_PARAMS_PER_FUNCTION = 5

sorted_params = sorted(
    properties.items(),
    key=lambda kv: (kv[0] not in required_params, kv[0]),
)
if len(sorted_params) > MAX_PARAMS_PER_FUNCTION:
    sorted_params = sorted_params[:MAX_PARAMS_PER_FUNCTION]
```

Required parameters are prioritized over optional ones when truncating.

---

## 10. Model Access Resolution

**Error:**
```
accessDeniedException: Access denied when calling Bedrock.
```

### Root Cause

Anthropic models require a **use case form** to be submitted in the Bedrock console. Verified by testing direct model invocation:

```powershell
# Claude 3 Sonnet — FAILED
aws bedrock-runtime invoke-model \
  --model-id "anthropic.claude-3-sonnet-20240229-v1:0" \
  --region ap-southeast-2 ...
# Error: Model use case details have not been submitted

# Claude 3 Haiku — SUCCEEDED
aws bedrock-runtime invoke-model \
  --model-id "anthropic.claude-3-haiku-20240307-v1:0" ...
# ✅ Worked
```

However, even Haiku failed with `invoke_inline_agent` (same access denied error from Bedrock's internal model call).

### Solution

Switched to **Amazon Nova Lite** which doesn't require the Anthropic use case form:

```powershell
# Test from CLI
python -c "
import boto3
c = boto3.client('bedrock-agent-runtime', region_name='ap-southeast-2')
r = c.invoke_inline_agent(
    foundationModel='amazon.nova-lite-v1:0',
    instruction='You are a helpful DevOps assistant...',
    sessionId='test-12345',
    inputText='Say hello'
)
evts = [e for e in r['completion']]
print(evts)
"
# Output: [{'chunk': {'bytes': b'Hello! How can I assist you today?'}}]
```

Updated Lambda env var:
```python
"BEDROCK_MODEL_ID": "amazon.nova-lite-v1:0"
```

---

## 11. Inline Agent API Differences

The `invoke_inline_agent` API differs from `invoke_agent` in several ways that required code changes:

### 11.1 Session State Key

| Mode | Key |
|------|-----|
| Registered (`invoke_agent`) | `sessionState` |
| Inline (`invoke_inline_agent`) | `inlineSessionState` |

### 11.2 Invocation ID Placement

| Mode | Where |
|------|-------|
| Registered | `functionResult.actionInvocationId` |
| Inline | `inlineSessionState.invocationId` (top-level) |

### 11.3 Instruction Minimum Length

`invoke_inline_agent` requires `instruction` to be at least 40 characters.

### Code Changes

```python
# Session state key
if use_registered_agent:
    kwargs["sessionState"] = {
        "returnControlInvocationResults": return_control_results
    }
else:
    inline_state = {
        "returnControlInvocationResults": return_control_results
    }
    if return_control_invocation_id:
        inline_state["invocationId"] = return_control_invocation_id
    kwargs["inlineSessionState"] = inline_state

# Invocation ID in function results
func_result = {
    "actionGroup": tc["action_group"],
    "function": tc["tool"],
    "responseBody": {"TEXT": {"body": result_body}},
}
if use_registered_agent and tc["invocation_id"]:
    func_result["actionInvocationId"] = tc["invocation_id"]
```

---

## 12. Final Working State

### Successful Invocation

```powershell
aws lambda invoke \
  --function-name devops-ai-agent-handler \
  --payload fileb://test_event.json \
  --cli-binary-format raw-in-base64-out \
  response.json \
  --region ap-southeast-2
```

### Response

```json
{
  "statusCode": 200,
  "body": {
    "alarm_name": "devops-agent-high-cpu",
    "instance_id": "i-0bf11b006e8f12844",
    "agent_response": "TEAMS_WEBHOOK_URL is not configured",
    "tool_calls_count": 3,
    "session_id": "782fbeb0-c6ac-4744-a0f1-2007c720f0f8"
  }
}
```

The agent:
1. Received the CloudWatch alarm event
2. Parsed the alarm (instance `i-0bf11b006e8f12844`, CPU > 80%)
3. Made **3 tool calls** (describe instance, get CPU metrics, attempt Teams notification)
4. Returned successfully — Teams notification correctly reported webhook not configured

---

## 13. Commands Reference

### Setup & Development

```bash
# Activate virtual environment
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux/Mac

# Install dependencies
pip install -e ".[dev]"
```

### CDK Deployment

```bash
# Synthesize CloudFormation templates
cdk synth

# Deploy all stacks
cdk deploy --all --require-approval never

# Deploy specific stack
cdk deploy DevOpsAgent-Runner --require-approval never
cdk deploy DevOpsAgent-Monitoring --require-approval never

# Destroy stacks
cdk destroy --all
```

### Lambda Testing

```bash
# Invoke Lambda with test event
aws lambda invoke \
  --function-name devops-ai-agent-handler \
  --payload fileb://test_event.json \
  --cli-binary-format raw-in-base64-out \
  response.json \
  --region ap-southeast-2

# View response
cat response.json | python -m json.tool          # bash
Get-Content response.json | python -m json.tool   # PowerShell
```

### Bedrock Model Verification

```bash
# List available Claude models
aws bedrock list-foundation-models \
  --region ap-southeast-2 \
  --by-provider Anthropic \
  --query "modelSummaries[].modelId" \
  --output json

# Check specific model status
aws bedrock get-foundation-model \
  --model-identifier "anthropic.claude-3-sonnet-20240229-v1:0" \
  --region ap-southeast-2

# Test direct model invocation
aws bedrock-runtime invoke-model \
  --model-id "amazon.nova-lite-v1:0" \
  --region ap-southeast-2 \
  --body fileb://model_test.json \
  --content-type application/json \
  response_model.json

# List Bedrock agents
aws bedrock-agent list-agents --region ap-southeast-2

# List agent aliases
aws bedrock-agent list-agent-aliases \
  --agent-id KYZ4EKSMX5 \
  --region ap-southeast-2
```

### Test Inline Agent (Python CLI)

```python
import boto3

client = boto3.client('bedrock-agent-runtime', region_name='ap-southeast-2')
response = client.invoke_inline_agent(
    foundationModel='amazon.nova-lite-v1:0',
    instruction='You are a helpful DevOps assistant that helps diagnose infrastructure issues.',
    sessionId='test-12345',
    inputText='Say hello',
)
events = [e for e in response['completion']]
print(events)
```

### Unit Tests

```bash
pytest tests/unit/ -v
pytest tests/integration/ -v
```

---

## 14. Remaining Items

| Item | Status | Notes |
|------|--------|-------|
| Teams Webhook | Not configured | Set `TEAMS_WEBHOOK_URL` in Lambda env vars |
| Anthropic Model Access | Blocked | Submit use case form in Bedrock console to unlock Claude |
| IAM Scoping | `bedrock:*` | Narrow to specific actions once stable |
| `asyncio.get_event_loop()` | Deprecated | Replace with `asyncio.run()` in `lambda_handler.py` |
| Settings test isolation | Not done | Reload `Settings` singleton in test fixtures |
| Production model | Nova Lite | Upgrade to Claude 3.5 Sonnet once Anthropic access is granted |

### To enable Teams notifications:

1. Create a webhook in Microsoft Teams (via Power Automate workflow)
2. Update the Lambda environment variable:
   ```python
   "TEAMS_WEBHOOK_URL": "https://your-tenant.webhook.office.com/..."
   ```
3. Redeploy: `cdk deploy DevOpsAgent-Runner --require-approval never`

### To upgrade to Claude once Anthropic access is approved:

1. Go to **AWS Console → Bedrock → Model access** (ap-southeast-2)
2. Enable **Anthropic → Claude 3 Sonnet** (or Claude 3.5 Sonnet)
3. Update the model ID:
   ```python
   "BEDROCK_MODEL_ID": "anthropic.claude-3-5-sonnet-20241022-v2:0"
   ```
4. Redeploy: `cdk deploy DevOpsAgent-Runner --require-approval never`

---

## Files Modified in This Session

| File | Change |
|------|--------|
| `src/agent/agent_core.py` | Full reasoning loop implementation |
| `infra/stacks/agent_runner_stack.py` | Pre-build bundling, IAM, model ID, inline mode |
| `requirements-lambda.txt` | **Created** — Lambda pip dependencies |
| `test_event.json` | **Created** — CloudWatch alarm test event |
| `.gitignore` | Added `.lambda-bundle/` |
