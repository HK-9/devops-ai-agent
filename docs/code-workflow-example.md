# Code Workflow: "Check all EC2 instances"

Step-by-step flow when a user types **"check all ec2 instances"** in the chat UI, with file names and line references.

---

## Entry Point: Chat UI (Web)

### Step 1 — User submits the message

| # | File | What happens |
|---|------|--------------|
| 1 | **`web/templates/chat.html`** | User types "check all ec2 instances" and clicks Send. JavaScript `sendMessage()` is called (line ~113). |
| 2 | **`web/templates/chat.html`** | `fetch('/api/chat', { method: 'POST', body: JSON.stringify({ message }) })` (lines 121–125). |

---

## Step 2 — Flask receives the request

| # | File | What happens |
|---|------|--------------|
| 3 | **`web/app.py`** | Flask app routes `/api/chat` to the handler (via `register_routes`). |
| 4 | **`web/routes.py`** | `chat_api()` (lines 226–264) handles POST `/api/chat`. |
| 5 | **`web/routes.py`** | Extracts `message = data.get("message", "")` → `"check all ec2 instances"` (line 229). |
| 6 | **`web/routes.py`** | Calls `get_agent()` (line 237) to get or create the shared `DevOpsAgent` instance. |
| 7 | **`web/routes.py`** | `get_agent()` (lines 36–42): if `_agent` is None, creates `DevOpsAgent()` and calls `await _agent.initialise()`. |
| 8 | **`web/routes.py`** | Calls `await agent.invoke(message, session_id=session["session_id"])` (line 238). |

---

## Step 3 — Agent initialisation (first request only)

| # | File | What happens |
|---|------|--------------|
| 9 | **`src/agent/agent_core.py`** | `DevOpsAgent.initialise()` (lines 57–82). |
| 10 | **`src/agent/agent_core.py`** | `await self._mcp.connect_all()` (line 60) — spawns 4 MCP server subprocesses. |
| 11 | **`src/mcp_client/client.py`** | `MCPClient.connect_all()` (lines 112–119) iterates over servers and calls `_connect_server()` for each. |
| 12 | **`src/mcp_client/client.py`** | `_connect_server()` (lines 121–159): spawns subprocess via `stdio_client()` (e.g. `python -m src.mcp_servers.aws_infra.server`), creates `ClientSession`, calls `session.list_tools()`, populates `_tool_index` (tool_name → server_name). |
| 13 | **`src/agent/agent_core.py`** | `self._bedrock_client = boto3.client("bedrock-agent-runtime", ...)` (lines 64–67). |

---

## Step 4 — Agent invoke and reasoning loop

| # | File | What happens |
|---|------|--------------|
| 14 | **`src/agent/agent_core.py`** | `invoke("check all ec2 instances", session_id=...)` (lines 90–127). |
| 15 | **`src/agent/agent_core.py`** | `_handle_reasoning_loop(prompt, sid, tool_calls_trace, is_alarm=False)` (line 116). Chat mode → `is_alarm=False` → `list_ec2_instances` is **included** in tools. |
| 16 | **`src/agent/agent_core.py`** | `tool_defs = self._mcp.get_tools_for_agent()` (line 146) — gets all tools from all MCP servers. |
| 17 | **`src/mcp_client/client.py`** | `get_tools_for_agent()` (lines 179–193) returns list of `{name, description, input_schema}` for every tool. |
| 18 | **`src/agent/agent_core.py`** | `_build_invoke_kwargs()` (lines 280–324) builds kwargs for Bedrock. |
| 19 | **`src/agent/agent_core.py`** | `_build_action_groups(tool_defs)` (lines 326–374) converts MCP tool schemas to Bedrock action-group format (including `list_ec2_instances`). |
| 20 | **`src/agent/agent_core.py`** | `invoke_kwargs` includes: `inputText="check all ec2 instances"`, `instruction=SYSTEM_PROMPT` (from `src/agent/system_prompt.py`), `actionGroups` with all tools. |
| 21 | **`src/agent/agent_core.py`** | `self._bedrock_client.invoke_inline_agent(**invoke_kwargs)` (line 184) — **AWS Bedrock API call**. |
| 22 | **`src/agent/config.py`** | Settings used: `bedrock_model_id`, `aws_region` (via `BaseSettings`). |

---

## Step 5 — Bedrock decides to call `list_ec2_instances`

| # | File | What happens |
|---|------|--------------|
| 23 | **`src/agent/agent_core.py`** | Event stream is processed (lines 193–231). For each `returnControl` event, extracts `function="list_ec2_instances"` and parameters. |
| 24 | **`src/agent/agent_core.py`** | `pending_tool_calls.append({ "tool": "list_ec2_instances", "arguments": { "state_filter": "running", ... }, ... })` (lines 221–226). |
| 25 | **`src/agent/agent_core.py`** | Since there are pending tool calls, loop continues (lines 237–267). |
| 26 | **`src/agent/agent_core.py`** | `result = await self._mcp.call_tool("list_ec2_instances", tc["arguments"])` (line 243). |

---

## Step 6 — MCP Client routes to the correct server

| # | File | What happens |
|---|------|--------------|
| 27 | **`src/mcp_client/client.py`** | `call_tool("list_ec2_instances", arguments)` (lines 197–237). |
| 28 | **`src/mcp_client/client.py`** | `server_name = self._tool_index.get("list_ec2_instances")` → `"aws-infra"` (line 206). |
| 29 | **`src/mcp_client/client.py`** | `server = self._servers["aws-infra"]` — gets the aws-infra connection (line 211). |
| 30 | **`src/mcp_client/client.py`** | `result = await server.session.call_tool("list_ec2_instances", arguments)` (lines 217–219) — sends MCP protocol message over stdio to the aws-infra subprocess. |

---

## Step 7 — AWS Infra MCP server handles the tool call

| # | File | What happens |
|---|------|--------------|
| 31 | **`src/mcp_servers/aws_infra/server.py`** | MCP server receives the tool call over stdio. |
| 32 | **`src/mcp_servers/aws_infra/server.py`** | `handle_call_tool(name, arguments)` (lines 110–126) is invoked with `name="list_ec2_instances"`. |
| 33 | **`src/mcp_servers/aws_infra/server.py`** | `if name == "list_ec2_instances"` (line 115) → calls `await list_ec2_instances(...)` with parsed args. |
| 34 | **`src/mcp_servers/aws_infra/tools.py`** | `list_ec2_instances()` (lines 21–85) is executed. |
| 35 | **`src/mcp_servers/aws_infra/tools.py`** | `ec2 = get_client("ec2")` (line 36) — gets boto3 EC2 client. |
| 36 | **`src/utils/aws_helpers.py`** | `get_client("ec2")` returns a cached boto3 EC2 client (uses `AWS_REGION`, `AWS_PROFILE` or default creds). |
| 37 | **`src/mcp_servers/aws_infra/tools.py`** | `response = safe_boto_call(ec2.describe_instances, **kwargs)` (line 59) — **actual EC2 API call**. |
| 38 | **`src/mcp_servers/aws_infra/tools.py`** | Builds `instances` list from `response["Reservations"]`, returns `{"instances": [...], "count": N}`. |
| 39 | **`src/mcp_servers/aws_infra/server.py`** | `return [TextContent(type="text", text=json.dumps(result, ...))]` (line 126) — MCP response back over stdio. |

---

## Step 8 — Result flows back to Bedrock

| # | File | What happens |
|---|------|--------------|
| 40 | **`src/mcp_client/client.py`** | Receives MCP response, parses JSON from `result.content[0].text` (lines 222–228). |
| 41 | **`src/mcp_client/client.py`** | Returns `{"instances": [...], "count": N}` to `agent_core.py`. |
| 42 | **`src/agent/agent_core.py`** | Builds `return_control_results` with the tool result (lines 252–264). |
| 43 | **`src/agent/agent_core.py`** | Next iteration of reasoning loop: calls `invoke_inline_agent` again with `returnControlInvocationResults` containing the EC2 list (lines 168–185). |
| 44 | **`src/agent/agent_core.py`** | Bedrock receives the tool result, continues reasoning, and eventually returns a text response (no more tool calls). |
| 45 | **`src/agent/agent_core.py`** | `pending_tool_calls` is empty → breaks out of loop (lines 234–236). |
| 46 | **`src/agent/agent_core.py`** | Returns `{ "response": final_text, "tool_calls": trace, "turns": turn }` to `invoke()` (lines 118–122). |

---

## Step 9 — Response back to the user

| # | File | What happens |
|---|------|--------------|
| 47 | **`web/routes.py`** | `result` from `agent.invoke()` contains `response` and `tool_calls` (line 238). |
| 48 | **`web/routes.py`** | `append_audit_entry(...)` for audit log (lines 244–249). |
| 49 | **`web/routes.py`** | `return jsonify({ "response": result.get("response"), "tool_calls": ..., "turns": ... })` (lines 255–261). |
| 50 | **`web/templates/chat.html`** | `fetch` resolves, `addMessage('agent', data.response)` displays the agent's answer (line 131). |

---

## Flow summary (file list in order)

```
web/templates/chat.html          → User input, fetch /api/chat
web/app.py                       → Flask app, routes
web/routes.py                    → chat_api(), get_agent(), agent.invoke()
src/agent/agent_core.py          → DevOpsAgent.invoke(), _handle_reasoning_loop()
src/agent/config.py              → Settings (model, region, MCP commands)
src/agent/system_prompt.py       → SYSTEM_PROMPT (instruction for Bedrock)
src/mcp_client/client.py         → get_tools_for_agent(), call_tool(), routing
src/mcp_servers/aws_infra/server.py  → handle_call_tool() dispatches to tools
src/mcp_servers/aws_infra/tools.py   → list_ec2_instances() → EC2 API
src/utils/aws_helpers.py         → get_client(), safe_boto_call()
```

---

## Lambda path (alarm-triggered flow)

If the same prompt came from a **CloudWatch alarm** (EventBridge → Lambda):

| File | Role |
|------|------|
| **`src/handlers/lambda_handler.py`** | Entry point; parses event, builds prompt |
| **`src/handlers/event_parser.py`** | `parse_eventbridge_alarm()`, `build_agent_prompt_from_alarm()` |
| **`src/agent/agent_core.py`** | Same as above, but `is_alarm=True` → `list_ec2_instances` is **excluded** for Nova Lite (avoids looping) |

For a **user chat** like "check all ec2 instances", the web path above applies; `is_alarm=False` so `list_ec2_instances` is available.
