"""
Microsoft Teams MCP Server.

Exposes notification tools over the Model Context Protocol.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.mcp_servers.teams.tools import (
    create_incident_notification,
    send_teams_message,
)
from src.utils.aws_helpers import setup_logging

logger = setup_logging("mcp-server.teams")

# ── Server setup ─────────────────────────────────────────────────────────

server = Server("teams-server")

# ── Tool definitions ─────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="send_teams_message",
        description="Send a plain-text message to the configured Microsoft Teams channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message body to send.",
                },
            },
            "required": ["message"],
        },
    ),
    Tool(
        name="create_incident_notification",
        description=(
            "Send a structured incident Adaptive Card to Teams with severity, "
            "instance details, metric snapshot, summary, and actions taken."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "description": "Incident severity: CRITICAL, WARNING, or INFO.",
                    "enum": ["CRITICAL", "WARNING", "INFO"],
                },
                "instance_id": {
                    "type": "string",
                    "description": "Affected EC2 instance ID.",
                },
                "alarm_name": {
                    "type": "string",
                    "description": "CloudWatch alarm name that fired.",
                },
                "metric_value": {
                    "type": "string",
                    "description": "Human-readable metric snapshot, e.g. 'CPU 92.3%'.",
                },
                "summary": {
                    "type": "string",
                    "description": "Narrative description of the incident.",
                },
                "actions_taken": {
                    "type": "string",
                    "description": "Actions already taken (default 'None yet').",
                    "default": "None yet",
                },
            },
            "required": ["severity", "instance_id", "alarm_name", "metric_value", "summary"],
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

    if name == "send_teams_message":
        result = await send_teams_message(message=arguments["message"])
    elif name == "create_incident_notification":
        result = await create_incident_notification(
            severity=arguments["severity"],
            instance_id=arguments["instance_id"],
            alarm_name=arguments["alarm_name"],
            metric_value=arguments["metric_value"],
            summary=arguments["summary"],
            actions_taken=arguments.get("actions_taken", "None yet"),
        )
    else:
        result = {"error": True, "message": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    logger.info("Starting Teams MCP server …")

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
