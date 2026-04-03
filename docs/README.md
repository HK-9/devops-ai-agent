# DevOps AI Agent

An **autonomous DevOps AI agent** that monitors AWS EC2 instances via CloudWatch alarms, diagnoses issues via SSM, and either auto-fixes minor problems or requests human approval for major ones — all via email with clickable APPROVE/REJECT links.

| Item | Value |
|------|-------|
| **AI Framework** | Strands Agents SDK |
| **Foundation Model** | `amazon.nova-pro-v1:0` (Amazon Bedrock) |
| **Tool Protocol** | MCP via AgentCore Gateway |
| **AWS Region** | `ap-southeast-2` (Sydney) |
| **Notification** | SNS email |

---

## Quick Start

```bash
# Setup
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Deploy agent
python scripts/deploy_agent.py deploy

# Deploy MCP servers
python scripts/deploy_mcp_servers.py deploy

# Run web UI
python web/app.py   # http://127.0.0.1:5001
```

---

## How It Works

### MINOR (1 offending process → auto-fix)
```
CloudWatch Alarm → EventBridge → Lambda → Agent
  → diagnose_instance  (finds 1 process at 92% CPU)
  → remediate_high_cpu (kills process)
  → send email: "AUTO-FIXED: High CPU on i-xxx"
```

### MAJOR (2+ offending processes → human approval)
```
CloudWatch Alarm → EventBridge → Lambda → Agent
  → diagnose_instance  (finds 4 processes)
  → request_approval   (sends email with APPROVE/REJECT links)
  → Human clicks APPROVE
  → Agent restarts instance + sends confirmation email
```

---

## Testing

```bash
# MINOR test (1 stress process → agent auto-fixes)
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor

# MAJOR test (4 stress processes → agent requests approval)
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario major

# Restore alarm to OK after testing
python scripts/test_remediation.py -i i-0327d856931d3b38f --restore

# View agent logs
python scripts/test_remediation.py --logs --minutes 5

# Direct invoke (bypass Lambda dedup)
python scripts/test_remediation.py -i i-0327d856931d3b38f --type cpu --scenario minor --direct
```

---

## Project Structure

```
├── deployments/
│   ├── agent/agent.py          # ★ Main agent logic, system prompt, Nova workarounds
│   └── mcp_servers/            # MCP tool servers (aws_infra, monitoring, sns, teams)
├── src/handlers/lambda_handler.py  # EventBridge → Lambda → Agent
├── web/                        # Flask UI (port 5001)
├── scripts/
│   ├── deploy_agent.py         # Build + push ECR + update AgentCore runtime
│   ├── deploy_mcp_servers.py   # Deploy all 4 MCP servers
│   └── test_remediation.py     # Test MINOR/MAJOR scenarios
└── docs/HANDOFF.md             # Full technical documentation
```

---

## Key AWS Resources

| Resource | Identifier |
|----------|-----------|
| Agent Runtime | `devops_agent-AYHFY5ECcy` |
| MCP Gateway | `devopsagentgatewayv3-ar4lmz2x6t` |
| Lambda | `devops-ai-agent-handler` |
| SNS Topic | `arn:aws:sns:ap-southeast-2:650251690796:devops-agent-alerts` |
| DynamoDB | `devops-agent-approvals` |
| Test Instance | `i-0327d856931d3b38f` (test-4) |

---

## Environment Variables (.env)

```env
AWS_REGION=ap-southeast-2
AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy
GATEWAY_URL=https://devopsagentgatewayv3-ar4lmz2x6t.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp
MODEL_ID=amazon.nova-pro-v1:0
SNS_TOPIC_ARN=arn:aws:sns:ap-southeast-2:650251690796:devops-agent-alerts
```

---

## Documentation

For full technical details, architecture diagrams, troubleshooting, and deployment history see **[docs/HANDOFF.md](docs/HANDOFF.md)**.
 