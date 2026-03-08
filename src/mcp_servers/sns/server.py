"""
AWS SNS MCP Server.

Exposes alerting tools with Teams-to-SNS failover over the
Model Context Protocol.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.mcp_servers.sns.tools import send_alert_with_failover
from src.utils.aws_helpers import setup_logging

logger = setup_logging("mcp-server.sns")

# ── Server setup ─────────────────────────────────────────────────────────

server = Server("sns-server")

# ── Tool definitions ─────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="send_alert_with_failover",
        description=(
            "Send an alert to Teams; if Teams is unavailable, fail over to AWS SNS. "
            "Returns which channel was used or whether both failed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Alert subject line.",
                },
                "message": {
                    "type": "string",
                    "description": "Alert body text.",
                },
            },
            "required": ["subject", "message"],
        },
    ),
]


# ── Handlers ─────────────────────────────────────────────────────────────


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info("Tool call: %s(%s)", name, json.dumps(arguments, default=str))

    if name == "send_alert_with_failover":
        result = await send_alert_with_failover(
            subject=arguments["subject"],
            message=arguments["message"],
        )
    else:
        result = {"error": True, "message": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    logger.info("Starting SNS MCP server …")

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
