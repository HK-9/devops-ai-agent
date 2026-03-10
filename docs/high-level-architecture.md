# DevOps AI Agent - High-Level Project Document

## Project Overview
DevOps AI Agent is an event-driven, LLM-powered operations assistant built to reduce manual incident triage for cloud infrastructure.  
It listens to monitoring events, reasons about what to do, uses tools through MCP servers, and produces actionable incident output with notifications.

## Problem Statement
Cloud incidents are often handled manually:
- Alerts arrive without full context.
- Engineers must pivot across multiple consoles and APIs.
- Response quality depends on who is on call.

This increases response time and creates inconsistent remediation.

## Solution Summary
The project implements an autonomous workflow:
1. Receive alarm/event context.
2. Parse and normalize the event.
3. Use LLM reasoning to plan investigation/remediation.
4. Invoke MCP tools for infrastructure and monitoring checks.
5. Decide next action and generate structured output.
6. Notify stakeholders (Teams/SNS paths).

This delivers a repeatable and auditable incident-handling loop.

## Tech Stack

### Core Language and Runtime
- Python 3.12
- AWS Lambda (runtime execution)
- Makefile-driven developer commands

### AI and Agent Layer
- AWS Bedrock AgentCore integration
- Anthropic Claude model usage in reasoning loop
- Prompt-driven behavioral constraints

### Protocol and Tool Orchestration
- MCP (Model Context Protocol) architecture
- MCP client for tool discovery/invocation
- Multiple MCP servers by domain:
  - AWS Infra
  - Monitoring
  - Teams
  - SNS

### Cloud and Infrastructure
- AWS services: Lambda, CloudWatch, EventBridge, SNS, EC2
- AWS CDK (Python) for infrastructure as code
- Stack split by concern:
  - Networking stack
  - Monitoring stack
  - Agent runner stack

### Quality and Testing
- `pytest` for unit and integration tests
- `ruff` for linting/formatting
- `mypy` for static typing
- Mock-based tests + MCP round-trip integration testing

## System Workflow

### Runtime Workflow (Agent Execution)
1. CloudWatch/EventBridge event triggers the Lambda handler.
2. Event parser extracts alarm context (resource, threshold, state).
3. Agent core builds context and starts LLM reasoning loop.
4. LLM decides which MCP tools to call.
5. MCP client routes calls to appropriate MCP servers.
6. Tool outputs are observed and fed back into reasoning.
7. Agent finalizes decision/recommendation/action summary.
8. Notification path sends structured incident output (Teams/SNS).

### Delivery Workflow (Engineering Workflow)
1. Set up environment and install dependencies.
2. Run lint/type/test checks.
3. Run demo flow for end-to-end walkthrough.
4. Deploy stacks via CDK for cloud execution.
5. Validate with test events and inspect logs.

## Architecture Snapshot
- Event Ingestion Layer: `src/handlers/`
- Agent Orchestration Layer: `src/agent/`
- MCP Integration Layer: `src/mcp_client/` and `src/mcp_servers/`
- Deployment Layer: `infra/stacks/`
- Quality Layer: `tests/`, `pyproject.toml`, `Makefile`

This separation keeps reasoning logic, tool implementations, and deployment concerns modular.

## Testing and Observability
- Unit tests validate parsing and tool behaviors.
- Integration tests validate MCP client/server interaction.
- Structured logging supports execution traceability.
- Demo script supports reviewer-friendly reproducibility.

## Deployment Model
The project supports:
- Local development/demo workflows.
- Cloud deployment through AWS CDK.
- Event-driven production-style runtime with monitoring and notification integration.

## Current Maturity
### Strong Areas
- Event-driven architecture
- LLM-centered reasoning loop
- MCP-based modular tool integration
- Test and deployment foundations

### MVP Boundaries
- Tool coverage is focused, not exhaustive
- Some safety behavior is prompt-driven rather than fully policy-enforced
- Evaluation framework is functional but not yet benchmark-heavy

## Key Workflow Commands (Representative)
- Install and setup: `make install`
- Lint/type checks: `make lint` and `make typecheck`
- Tests: `make test`, `make test-unit`, `make test-integration`
- Demo: `python demo.py`
- Deploy: `cdk deploy --all`

## Key Project References
- `README.md`
- `docs/devops-agent-walkthrough.md`
- `src/agent/agent_core.py`
- `src/agent/system_prompt.py`
- `src/handlers/lambda_handler.py`
- `src/handlers/event_parser.py`
- `src/mcp_client/client.py`
- `src/mcp_servers/aws_infra/server.py`
- `src/mcp_servers/monitoring/server.py`
- `src/mcp_servers/teams/server.py`
- `src/mcp_servers/sns/server.py`
- `infra/stacks/agent_runner_stack.py`
- `infra/stacks/monitoring_stack.py`
- `infra/stacks/networking_stack.py`
- `tests/integration/test_mcp_round_trip.py`
- `Makefile`