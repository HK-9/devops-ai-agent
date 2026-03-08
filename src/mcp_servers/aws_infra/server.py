"""
AWS Infrastructure MCP Server.

Exposes EC2 management tools over the Model Context Protocol.
Transport: stdio (default) or SSE.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.mcp_servers.aws_infra.tools import (
    describe_ec2_instance,
    list_ec2_instances,
    restart_ec2_instance,
)
from src.utils.aws_helpers import setup_logging

logger = setup_logging("mcp-server.aws-infra")

# ── Server setup ─────────────────────────────────────────────────────────

server = Server("aws-infra-server")

# ── Tool definitions ─────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="list_ec2_instances",
        description=(
            "List EC2 instances filtered by state and/or tags. "
            "Returns instance ID, type, state, IPs, and tags."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "state_filter": {
                    "type": "string",
                    "description": "Filter by state: 'running', 'stopped', or 'all'.",
                    "default": "running",
                    "enum": ["running", "stopped", "terminated", "all"],
                },
                "tag_filters": {
                    "type": "object",
                    "description": "Optional tag key-value pairs to filter by.",
                    "additionalProperties": {"type": "string"},
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max instances to return (default 50).",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="describe_ec2_instance",
        description=(
            "Get detailed information about a single EC2 instance, including "
            "network, security groups, IAM role, and EBS volumes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "The EC2 instance ID, e.g. 'i-0abc123def456'.",
                },
            },
            "required": ["instance_id"],
        },
    ),
    Tool(
        name="restart_ec2_instance",
        description=(
            "Restart (stop + start) an EC2 instance. Use with caution — "
            "prefer confirming with the user first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "The EC2 instance ID to restart.",
                },
            },
            "required": ["instance_id"],
        },
    ),
]


# ── Handlers ─────────────────────────────────────────────────────────────


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """Return the tool manifest for this server."""
    return TOOLS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call to the appropriate implementation."""
    logger.info("Tool call: %s(%s)", name, json.dumps(arguments, default=str))

    if name == "list_ec2_instances":
        result = await list_ec2_instances(
            state_filter=arguments.get("state_filter", "running"),
            tag_filters=arguments.get("tag_filters"),
            max_results=arguments.get("max_results", 50),
        )
    elif name == "describe_ec2_instance":
        result = await describe_ec2_instance(instance_id=arguments["instance_id"])
    elif name == "restart_ec2_instance":
        result = await restart_ec2_instance(instance_id=arguments["instance_id"])
    else:
        result = {"error": True, "message": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over stdio transport."""
    logger.info("Starting AWS Infra MCP server …")

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
