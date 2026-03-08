"""
Unified MCP Client Adapter.

Connects to all three MCP servers (AWS Infra, Monitoring, Teams) and
exposes a single interface for tool discovery and invocation. This is
the bridge between AgentCore and the MCP tool layer.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass, field
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from src.agent.config import settings
from src.utils.aws_helpers import setup_logging

logger = setup_logging("mcp-client")


# ── Data structures ──────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    """A discovered MCP tool with its schema and source server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str


@dataclass
class MCPServerConnection:
    """Tracks a single MCP server subprocess + session."""

    name: str
    command: str
    session: ClientSession | None = None
    tools: list[ToolDefinition] = field(default_factory=list)


# ── MCP Client ───────────────────────────────────────────────────────────


class MCPClient:
    """Unified client that manages connections to all MCP servers.

    Usage::

        async with MCPClient() as client:
            tools = await client.discover_tools()
            result = await client.call_tool("list_ec2_instances", {"state_filter": "running"})
    """

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerConnection] = {
            "aws-infra": MCPServerConnection(
                name="aws-infra",
                command=settings.mcp_aws_infra_command,
            ),
            "monitoring": MCPServerConnection(
                name="monitoring",
                command=settings.mcp_monitoring_command,
            ),
            "teams": MCPServerConnection(
                name="teams",
                command=settings.mcp_teams_command,
            ),
        }
        self._tool_index: dict[str, str] = {}  # tool_name → server_name
        self._contexts: list[Any] = []  # context managers to clean up

    async def __aenter__(self) -> MCPClient:
        """Connect to all MCP servers and discover tools."""
        await self.connect_all()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Disconnect from all servers."""
        await self.disconnect_all()

    # ── Connection management ────────────────────────────────────────────

    async def connect_all(self) -> None:
        """Start all MCP server subprocesses and initialise sessions."""
        for server in self._servers.values():
            try:
                await self._connect_server(server)
                logger.info("Connected to MCP server: %s", server.name)
            except Exception as exc:
                logger.error("Failed to connect to %s: %s", server.name, exc)

    async def _connect_server(self, server: MCPServerConnection) -> None:
        """Connect to a single MCP server via stdio."""
        parts = shlex.split(server.command)
        params = StdioServerParameters(command=parts[0], args=parts[1:])

        # Open the stdio transport
        transport_ctx = stdio_client(params)
        read_stream, write_stream = await transport_ctx.__aenter__()
        self._contexts.append(transport_ctx)

        # Create and initialise the session
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        self._contexts.append(session)
        await session.initialize()
        server.session = session

        # Discover tools
        tools_response = await session.list_tools()
        for tool in tools_response.tools:
            td = ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema if isinstance(tool.inputSchema, dict) else {},
                server_name=server.name,
            )
            server.tools.append(td)
            self._tool_index[tool.name] = server.name

    async def disconnect_all(self) -> None:
        """Clean up all open contexts (sessions + transports)."""
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Error closing context: %s", exc)
        self._contexts.clear()

    # ── Tool discovery ───────────────────────────────────────────────────

    async def discover_tools(self) -> list[ToolDefinition]:
        """Return all tools discovered across every connected server."""
        all_tools: list[ToolDefinition] = []
        for server in self._servers.values():
            all_tools.extend(server.tools)
        return all_tools

    def get_tools_for_agent(self) -> list[dict[str, Any]]:
        """Build the tool-definition list in the format AgentCore expects.

        Returns a list of dicts with ``name``, ``description``, and
        ``input_schema`` keys.
        """
        return [
            {
                "name": td.name,
                "description": td.description,
                "input_schema": td.input_schema,
            }
            for server in self._servers.values()
            for td in server.tools
        ]

    # ── Tool invocation ──────────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a tool by name, routing to the correct MCP server.

        Args:
            name: Tool name (e.g. ``list_ec2_instances``).
            arguments: Tool input arguments.

        Returns:
            Parsed JSON result from the tool, or an error dict.
        """
        server_name = self._tool_index.get(name)
        if not server_name:
            return {"error": True, "message": f"Unknown tool: {name}"}

        server = self._servers[server_name]
        if not server.session:
            return {"error": True, "message": f"Server {server_name} is not connected"}

        logger.info("Calling tool %s on server %s", name, server_name)

        try:
            result = await asyncio.wait_for(
                server.session.call_tool(name, arguments or {}),
                timeout=settings.tool_timeout_seconds,
            )
            # Parse the text content from the response
            if result.content and len(result.content) > 0:
                text = result.content[0].text
                try:
                    return json.loads(text)  # type: ignore[no-any-return]
                except json.JSONDecodeError:
                    return {"result": text}
            return {"result": None}

        except asyncio.TimeoutError:
            logger.error("Tool %s timed out after %ds", name, settings.tool_timeout_seconds)
            return {"error": True, "message": f"Tool {name} timed out"}
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc)
            return {"error": True, "message": str(exc)}
