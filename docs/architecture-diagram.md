# DevOps AI Agent — Architecture Diagram

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                           DevOps AI Agent — Event-Driven Flow                            │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Cloud Runtime Architecture (Production)

```mermaid
flowchart TB
    subgraph Triggers["Event Triggers"]
        EC2[(EC2 Instance)]
        EC2 -->|CPU metrics| CW[CloudWatch Metrics]
        CW -->|threshold exceeded| ALM[CloudWatch Alarm<br/>devops-agent-high-cpu]
        ALM -->|state: ALARM| EB[EventBridge Rule<br/>devops-agent-alarm-trigger]
    end

    subgraph Lambda["Agent Lambda"]
        EB --> LH[Lambda Handler<br/>lambda_handler.handler]
        LH --> EP[Event Parser]
        EP -->|AlarmEvent| AC[AgentCore<br/>DevOpsAgent]
        AC -->|invoke_agent / invoke_inline_agent| BR[AWS Bedrock<br/>Claude / Nova]
        BR -->|tool use requests| AC
        AC -->|call_tool| MC[MCP Client]
        MC -->|stdio| MCP[MCP Servers<br/>subprocesses]
    end

    subgraph MCPServers["MCP Servers (4 subprocesses)"]
        direction TB
        AWS_MCP[aws-infra<br/>EC2 tools]
        MON_MCP[monitoring<br/>CloudWatch tools]
        TMS_MCP[teams<br/>Webhook tools]
        SNS_MCP[sns<br/>Publish tools]
    end

    subgraph External["External Services"]
        EC2_API[(EC2 API)]
        CWM_API[(CloudWatch API)]
        TEA_URL[Teams Webhook URL]
        SNS_TOP[SNS Topic]
    end

    MCP --> AWS_MCP
    MCP --> MON_MCP
    MCP --> TMS_MCP
    MCP --> SNS_MCP

    AWS_MCP --> EC2_API
    MON_MCP --> CWM_API
    TMS_MCP --> TEA_URL
    SNS_MCP --> SNS_TOP
```

---

## 2. CDK Infrastructure Stacks

```mermaid
flowchart LR
    subgraph CDK["AWS CDK App"]
        direction TB
        NW[DevOpsAgent-Networking]
        MON[DevOpsAgent-Monitoring]
        RUN[DevOpsAgent-Runner]
    end

    subgraph NW_Out["Networking Stack Outputs"]
        VPC[VPC<br/>Public + Private subnets]
        SG[Security Group<br/>AgentSG]
    end

    subgraph MON_Out["Monitoring Stack Outputs"]
        CW_ALM[CloudWatch Alarm<br/>High CPU]
        EB_RULE[EventBridge Rule]
    end

    subgraph RUN_Out["Agent Runner Stack Outputs"]
        LAMBDA[Lambda Function<br/>devops-ai-agent-handler]
        SNS[SNS Topic<br/>devops-agent-alerts]
    end

    NW --> NW_Out
    MON --> MON_Out
    RUN --> RUN_Out

    MON -.->|alarm_rule| RUN
    RUN -.->|EventBridge target| EB_RULE
```

---

## 3. Agent Reasoning Loop (Detailed)

```mermaid
sequenceDiagram
    participant EB as EventBridge
    participant LH as Lambda Handler
    participant AC as AgentCore
    participant BR as Bedrock (LLM)
    participant MC as MCP Client
    participant MCP as MCP Servers

    EB->>LH: Alarm state change (ALARM)
    LH->>LH: Parse event → AlarmEvent
    LH->>AC: agent.invoke(prompt, is_alarm=True)

    loop Reasoning loop (until end turn)
        AC->>AC: Build action groups from MCP tools
        AC->>BR: invoke_agent / invoke_inline_agent
        BR-->>AC: stream: text chunks, returnControl (tool use)

        alt Tool call requested
            AC->>AC: Extract tool name + params
            AC->>MC: call_tool(name, arguments)
            MC->>MCP: stdio → correct server
            MCP->>MCP: EC2 / CloudWatch / Teams / SNS
            MCP-->>MC: JSON result
            MC-->>AC: result
            AC->>BR: returnControlInvocationResults
        end
    end

    AC-->>LH: { response, tool_calls }
    LH-->>EB: 200 + body
```

---

## 4. Source Code Layout

```mermaid
flowchart TB
    subgraph Src["src/"]
        subgraph Handlers["src/handlers/"]
            LH_F[lambda_handler.py]
            EP_F[event_parser.py]
        end

        subgraph Agent["src/agent/"]
            AC_F[agent_core.py]
            SP_F[system_prompt.py]
            CF_F[config.py]
        end

        subgraph MCPClient["src/mcp_client/"]
            MC_F[client.py]
        end

        subgraph MCPServers["src/mcp_servers/"]
            AWS_F[aws_infra/server.py]
            MON_F[monitoring/server.py]
            TMS_F[teams/server.py]
            SNS_F[sns/server.py]
        end
    end

    subgraph Web["web/"]
        APP[app.py]
        ROUTES[routes.py]
    end

    subgraph Infra["infra/"]
        APP_CDK[app.py]
        NW_ST[stacks/networking_stack.py]
        MON_ST[stacks/monitoring_stack.py]
        RUN_ST[stacks/agent_runner_stack.py]
    end

    LH_F --> AC_F
    AC_F --> MC_F
    MC_F --> AWS_F
    MC_F --> MON_F
    MC_F --> TMS_F
    MC_F --> SNS_F
    APP --> AC_F
```

---

## 5. Local Development vs Cloud Deployment

```mermaid
flowchart TB
    subgraph Local["Local Development"]
        direction TB
        DEMO[demo.py<br/>End-to-end test]
        WEB[web/app.py<br/>Flask :5001]
        AC_L[AgentCore]
        MC_L[MCP Client]
        MCP_L[MCP Servers<br/>stdio subprocesses]
        WEB --> AC_L
        DEMO --> AC_L
        AC_L --> MC_L
        MC_L --> MCP_L
    end

    subgraph Cloud["Cloud (Lambda)"]
        direction TB
        EB_C[EventBridge]
        LH_C[Lambda Handler]
        AC_C[AgentCore]
        MC_C[MCP Client]
        MCP_C[MCP Servers<br/>in same Lambda]
        EB_C --> LH_C
        LH_C --> AC_C
        AC_C --> MC_C
        MC_C --> MCP_C
    end

    BR[(AWS Bedrock<br/>Same in both)]
    AC_L -.->|invoke_agent| BR
    AC_C -.->|invoke_agent| BR
```

---

## 6. MCP Tool Routing

```mermaid
flowchart LR
    AC[AgentCore] -->|discover_tools| MC[MCP Client]
    AC -->|call_tool| MC

    MC -->|list_ec2_instances<br/>restart_ec2_instance<br/>stop_ec2_instance| AWS[aws-infra]
    MC -->|get_cpu_utilization<br/>list_cloudwatch_metrics| MON[monitoring]
    MC -->|send_teams_message<br/>create_incident_notification| TMS[teams]
    MC -->|publish_to_sns| SNS[sns]

    AWS --> EC2[(EC2 API)]
    MON --> CW[(CloudWatch API)]
    TMS --> TEAMS[Teams Webhook]
    SNS --> SNS_TOP[SNS Topic]
```

---

## 7. Data Flow Summary

| Stage | Component | Input | Output |
|-------|-----------|-------|--------|
| 1 | CloudWatch | EC2 CPU metrics | Alarm state change |
| 2 | EventBridge | Alarm event | Lambda invocation |
| 3 | Lambda Handler | Raw event | Parsed AlarmEvent |
| 4 | AgentCore | Prompt + tools | Bedrock request |
| 5 | Bedrock | Prompt + tool results | LLM response / tool use |
| 6 | MCP Client | Tool name + args | MCP server call |
| 7 | MCP Servers | Tool call | AWS API / Teams / SNS |
| 8 | AgentCore | Tool results | Next Bedrock turn or final response |
| 9 | Lambda Handler | Agent response | HTTP 200 + JSON body |
