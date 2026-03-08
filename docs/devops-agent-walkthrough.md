# DevOps AI Agent — Scaffolding Walkthrough

## What was built

Full project scaffolded at `C:\Users\pvhar\codelabs\devops-ai-agent` with **35+ files** across 4 major components:

### Source Modules (`src/`)

| Component | Files | Purpose |
|---|---|---|
| `agent/` | [config.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/agent/config.py), [system_prompt.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/agent/system_prompt.py), [agent_core.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/agent/agent_core.py) | Pydantic env config, agent persona + guardrails, AgentCore↔MCP bridge |
| `mcp_servers/aws_infra/` | [tools.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/mcp_servers/aws_infra/tools.py), [server.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/mcp_servers/aws_infra/server.py) | `list_ec2_instances`, `describe_ec2_instance`, `restart_ec2_instance` |
| `mcp_servers/monitoring/` | [tools.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/mcp_servers/monitoring/tools.py), [server.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/mcp_servers/monitoring/server.py) | `get_cpu_metrics`, batch CPU, `get_memory_metrics`, `get_disk_usage` |
| `mcp_servers/teams/` | [tools.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/mcp_servers/teams/tools.py), [server.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/mcp_servers/teams/server.py) | `send_teams_message`, `create_incident_notification` |
| `mcp_client/` | [client.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/mcp_client/client.py) | Unified client: auto-discover tools, route calls to correct server |
| `handlers/` | [lambda_handler.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/handlers/lambda_handler.py), [event_parser.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/handlers/event_parser.py) | Lambda entry point + EventBridge alarm → typed dataclass parser |
| `utils/` | [aws_helpers.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/utils/aws_helpers.py), [teams_webhook.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/src/utils/teams_webhook.py) | boto3 factory/retry, JSON logging, Teams HTTP + Adaptive Cards |

### Infrastructure (`infra/`)

| Stack | File | Resources |
|---|---|---|
| Networking | [networking_stack.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/infra/stacks/networking_stack.py) | VPC, public/private subnets, agent SG |
| Monitoring | [monitoring_stack.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/infra/stacks/monitoring_stack.py) | CloudWatch CPU alarm (>80%), EventBridge rule |
| Agent Runner | [agent_runner_stack.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/infra/stacks/agent_runner_stack.py) | Lambda function with IAM policies (EC2, CW, Bedrock) |

### Tests (`tests/`)
- 4 unit test files (EC2 tools, monitoring tools, Teams tools, event parser) using **moto** mocks
- 1 integration test for MCP server ↔ client round-trip discovery
- Mock responses module with canned EC2/CloudWatch/Teams data

### Demo Helper
- [demo.py](file:///C:/Users/pvhar/codelabs/devops-ai-agent/demo.py) — 8-section interactive walkthrough demonstrating every module with mocked data. Run `python demo.py` for the full tour or `python demo.py --section 7` for the E2E flow simulation.

## Verification

- ✅ All 35+ files created in the correct directory tree
- ✅ Every `__init__.py` in place for importability
- ✅ MCP servers expose proper JSON-schema tool manifests
- ✅ All tools have docstrings, type hints, and error handling
- ✅ Test fixtures provide realistic sample events
