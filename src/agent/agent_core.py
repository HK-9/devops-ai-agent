"""
AgentCore client wrapper.

Bridges Bedrock AgentCore's reasoning loop with the MCP client.
AgentCore manages orchestration, retries, and memory — this module
translates its tool-call requests into MCP invocations and feeds
the results back.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import boto3

from src.agent.config import settings
from src.agent.system_prompt import SYSTEM_PROMPT
from src.mcp_client.client import MCPClient
from src.utils.aws_helpers import setup_logging

logger = setup_logging("agent-core")


class DevOpsAgent:
    """High-level agent that wires AgentCore to MCP tools.

    Lifecycle::

        agent = DevOpsAgent()
        await agent.initialise()          # connect to MCP servers
        response = await agent.invoke("Check CPU on i-abc123")
        await agent.shutdown()

    Or use as an async context manager::

        async with DevOpsAgent() as agent:
            response = await agent.invoke("Check CPU on i-abc123")
    """

    def __init__(self) -> None:
        self._mcp = MCPClient()
        self._bedrock_client: Any = None

    async def __aenter__(self) -> DevOpsAgent:
        await self.initialise()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.shutdown()

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def initialise(self) -> None:
        """Connect MCP clients and set up the Bedrock runtime client."""
        # Connect to MCP servers
        await self._mcp.connect_all()
        tools = await self._mcp.discover_tools()
        logger.info("Discovered %d MCP tools", len(tools))

        # Set up Bedrock Agent Runtime client
        self._bedrock_client = boto3.client(
            "bedrock-agent-runtime",
            region_name=settings.aws_region,
        )
        logger.info("AgentCore client initialised (model=%s)", settings.bedrock_model_id)

    async def shutdown(self) -> None:
        """Disconnect from all MCP servers."""
        await self._mcp.disconnect_all()
        logger.info("Agent shut down")

    # ── Invocation ───────────────────────────────────────────────────────

    async def invoke(self, prompt: str, session_id: str | None = None) -> dict[str, Any]:
        """Invoke the agent with a natural-language prompt.

        This method sends the prompt to Bedrock AgentCore, which manages
        the reasoning loop.  When AgentCore decides to call a tool, we
        intercept it, route via the MCP client, and return the result
        so the LLM can continue reasoning.

        Args:
            prompt:     Natural-language instruction for the agent.
            session_id: Optional session ID for conversation continuity.

        Returns:
            Dict with ``response`` text and ``tool_calls`` trace.
        """
        if not self._bedrock_client:
            return {"error": True, "message": "Agent not initialised — call .initialise() first"}

        logger.info("Invoking agent: %.120s…", prompt)

        tool_calls_trace: list[dict[str, Any]] = []

        try:
            sid = session_id or str(uuid.uuid4())
            response_text, turns = await self._handle_reasoning_loop(prompt, sid, tool_calls_trace)

            return {
                "response": response_text,
                "tool_calls": tool_calls_trace,
                "turns": turns,
            }

        except Exception as exc:
            logger.error("Agent invocation failed: %s", exc)
            return {"error": True, "message": str(exc)}

    async def _handle_reasoning_loop(
        self,
        prompt: str,
        session_id: str,
        trace: list[dict[str, Any]],
    ) -> tuple[str, int]:
        """Execute the Bedrock reasoning loop, bridging tool calls to MCP.

        Sends the prompt to Bedrock via ``invoke_agent`` (pre-registered)
        or ``invoke_inline_agent`` (ad-hoc).  When the model requests a
        tool call via ``returnControl``, the call is routed through the
        MCP client and the result is fed back for the next turn.

        Returns:
            A tuple of (response_text, turn_count).
        """
        tool_defs = self._mcp.get_tools_for_agent()
        use_registered_agent = bool(settings.agent_id and settings.agent_alias_id)
        logger.info(
            "Starting reasoning loop (%s) with %d tools: %s",
            "registered agent" if use_registered_agent else "inline agent",
            len(tool_defs),
            [t["name"] for t in tool_defs],
        )

        response_parts: list[str] = []
        return_control_results: list[dict[str, Any]] | None = None
        return_control_invocation_id: str | None = None
        turn = 0

        for turn in range(1, settings.max_reasoning_turns + 1):
            logger.info("Reasoning turn %d", turn)

            # ── Build invocation kwargs ──────────────────────────────
            invoke_kwargs = self._build_invoke_kwargs(
                prompt=prompt if turn == 1 else "",
                session_id=session_id,
                tool_defs=tool_defs,
                return_control_results=return_control_results,
                use_registered_agent=use_registered_agent,
                return_control_invocation_id=return_control_invocation_id,
            )

            # ── Call Bedrock (sync SDK → thread) ─────────────────────
            if use_registered_agent:
                response = await asyncio.to_thread(
                    self._bedrock_client.invoke_agent, **invoke_kwargs
                )
            else:
                response = await asyncio.to_thread(
                    self._bedrock_client.invoke_inline_agent, **invoke_kwargs
                )

            # ── Process the event stream ─────────────────────────────
            return_control_results = None
            pending_tool_calls: list[dict[str, Any]] = []
            return_control_invocation_id_this_turn: str | None = None

            for event in response.get("completion", []):
                # Text chunk from the model
                if "chunk" in event:
                    chunk_bytes = event["chunk"].get("bytes", b"")
                    text = (
                        chunk_bytes.decode("utf-8")
                        if isinstance(chunk_bytes, bytes)
                        else str(chunk_bytes)
                    )
                    response_parts.append(text)

                # Model requests tool execution (RETURN_CONTROL)
                if "returnControl" in event:
                    # Capture top-level invocationId (used by inline agents)
                    return_control_invocation_id_this_turn = event["returnControl"].get(
                        "invocationId", ""
                    )
                    for inv_input in event["returnControl"].get("invocationInputs", []):
                        func_input = inv_input.get("functionInvocationInput", {})
                        tool_name = func_input.get("function", "")
                        action_group = func_input.get("actionGroup", "")
                        invocation_id = func_input.get("actionInvocationId", "")

                        parameters: dict[str, Any] = {}
                        for param in func_input.get("parameters", []):
                            parameters[param["name"]] = _cast_parameter(
                                param.get("value", ""), param.get("type", "string")
                            )

                        pending_tool_calls.append({
                            "tool": tool_name,
                            "arguments": parameters,
                            "action_group": action_group,
                            "invocation_id": invocation_id,
                        })

                # Trace / observability events
                if "trace" in event:
                    trace.append(event["trace"])

            # ── No tool calls → model finished reasoning ────────────
            if not pending_tool_calls:
                logger.info("Reasoning complete after %d turn(s)", turn)
                break

            # ── Execute each pending tool call via MCP ───────────────
            return_control_results = []
            for tc in pending_tool_calls:
                logger.info("Calling MCP tool: %s(%s)", tc["tool"], json.dumps(tc["arguments"], default=str))

                result = await self._mcp.call_tool(tc["tool"], tc["arguments"])
                result_body = json.dumps(result, default=str)

                trace.append({
                    "tool_call": tc["tool"],
                    "arguments": tc["arguments"],
                    "result": result,
                })

                func_result: dict[str, Any] = {
                    "actionGroup": tc["action_group"],
                    "function": tc["tool"],
                    "responseBody": {
                        "TEXT": {"body": result_body}
                    },
                }
                # Registered agents include actionInvocationId in each
                # functionResult; inline agents omit it.
                if use_registered_agent and tc["invocation_id"]:
                    func_result["actionInvocationId"] = tc["invocation_id"]

                return_control_results.append({"functionResult": func_result})

                logger.info("Tool %s returned: %.200s", tc["tool"], result_body)

            # Preserve the invocation ID for the next inline-agent turn
            return_control_invocation_id = return_control_invocation_id_this_turn
        else:
            logger.warning(
                "Reached max reasoning turns (%d) without completion",
                settings.max_reasoning_turns,
            )

        final_text = "".join(response_parts) or "[Agent produced no text response]"
        return final_text, turn

    # ── Invocation builders ──────────────────────────────────────────

    def _build_invoke_kwargs(
        self,
        prompt: str,
        session_id: str,
        tool_defs: list[dict[str, Any]],
        return_control_results: list[dict[str, Any]] | None,
        *,
        use_registered_agent: bool,
        return_control_invocation_id: str | None = None,
    ) -> dict[str, Any]:
        """Build kwargs for ``invoke_agent`` or ``invoke_inline_agent``."""
        if use_registered_agent:
            kwargs: dict[str, Any] = {
                "agentId": settings.agent_id,
                "agentAliasId": settings.agent_alias_id,
                "sessionId": session_id,
            }
        else:
            kwargs = {
                "foundationModel": settings.bedrock_model_id,
                "instruction": SYSTEM_PROMPT,
                "sessionId": session_id,
                "actionGroups": self._build_action_groups(tool_defs),
            }

        if prompt:
            kwargs["inputText"] = prompt

        if return_control_results:
            if use_registered_agent:
                kwargs["sessionState"] = {
                    "returnControlInvocationResults": return_control_results
                }
            else:
                inline_state: dict[str, Any] = {
                    "returnControlInvocationResults": return_control_results
                }
                if return_control_invocation_id:
                    inline_state["invocationId"] = return_control_invocation_id
                kwargs["inlineSessionState"] = inline_state

        return kwargs

    def _build_action_groups(
        self, tool_defs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert MCP tool definitions into Bedrock action-group format.

        Groups all MCP tools under a single ``MCPTools`` action group
        with ``RETURN_CONTROL`` so tool execution is handled locally.
        """
        MAX_PARAMS_PER_FUNCTION = 5  # Bedrock inline-agent quota

        functions: list[dict[str, Any]] = []
        for tool in tool_defs:
            schema = tool.get("input_schema", {})
            properties = schema.get("properties", {})
            required_params = set(schema.get("required", []))

            # Sort so required params come first; drop extras beyond the quota.
            sorted_params = sorted(
                properties.items(),
                key=lambda kv: (kv[0] not in required_params, kv[0]),
            )
            if len(sorted_params) > MAX_PARAMS_PER_FUNCTION:
                dropped = [k for k, _ in sorted_params[MAX_PARAMS_PER_FUNCTION:]]
                logger.warning(
                    "Tool %s has %d params (limit %d) — dropping: %s",
                    tool["name"], len(sorted_params), MAX_PARAMS_PER_FUNCTION, dropped,
                )
                sorted_params = sorted_params[:MAX_PARAMS_PER_FUNCTION]

            parameters: dict[str, dict[str, Any]] = {}
            for param_name, param_schema in sorted_params:
                parameters[param_name] = {
                    "type": _map_json_type_to_bedrock(param_schema.get("type", "string")),
                    "description": param_schema.get("description", f"Parameter: {param_name}"),
                    "required": param_name in required_params,
                }

            functions.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": parameters,
            })

        return [
            {
                "actionGroupName": "MCPTools",
                "actionGroupExecutor": {"customControl": "RETURN_CONTROL"},
                "functionSchema": {"functions": functions},
            }
        ]

    # ── Direct tool access (for testing / scripting) ─────────────────

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Directly call an MCP tool (bypasses AgentCore reasoning).

        Useful for testing individual tools or building scripts.
        """
        return await self._mcp.call_tool(name, arguments)


# ── Module-level helpers ─────────────────────────────────────────────────


def _map_json_type_to_bedrock(json_type: str) -> str:
    """Map JSON Schema types to Bedrock parameter types."""
    mapping = {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "array": "array",
        "object": "string",  # complex objects serialised as JSON strings
    }
    return mapping.get(json_type, "string")


def _cast_parameter(value: str, param_type: str) -> Any:
    """Cast a stringified parameter value to its Python type."""
    if param_type in ("integer", "int"):
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    elif param_type in ("number", "float", "double"):
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    elif param_type == "boolean":
        return value.lower() in ("true", "1", "yes")
    elif param_type in ("array", "object"):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value
