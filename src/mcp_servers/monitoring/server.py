"""
CloudWatch Monitoring MCP Server.

Exposes metrics retrieval tools over the Model Context Protocol.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.mcp_servers.monitoring.tools import (
    get_cpu_metrics,
    get_cpu_metrics_for_instances,
    get_disk_usage,
    get_memory_metrics,
)
from src.utils.aws_helpers import setup_logging

logger = setup_logging("mcp-server.monitoring")

# ── Server setup ─────────────────────────────────────────────────────────

server = Server("monitoring-server")

# ── Tool definitions ─────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="get_cpu_metrics",
        description="Retrieve CPU utilization metrics for a single EC2 instance over a time window.",
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "EC2 instance ID.",
                },
                "period": {
                    "type": "integer",
                    "description": "Metric period in seconds (default 300).",
                    "default": 300,
                },
                "minutes": {
                    "type": "integer",
                    "description": "How many minutes of history to fetch (default 60).",
                    "default": 60,
                },
            },
            "required": ["instance_id"],
        },
    ),
    Tool(
        name="get_cpu_metrics_for_instances",
        description="Batch-retrieve CPU utilization for multiple instances in one call.",
        inputSchema={
            "type": "object",
            "properties": {
                "instance_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of EC2 instance IDs.",
                },
                "period": {
                    "type": "integer",
                    "description": "Metric period in seconds.",
                    "default": 300,
                },
                "minutes": {
                    "type": "integer",
                    "description": "History window in minutes.",
                    "default": 60,
                },
            },
            "required": ["instance_ids"],
        },
    ),
    Tool(
        name="get_memory_metrics",
        description="Retrieve memory utilization for an instance (requires CloudWatch Agent).",
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "EC2 instance ID."},
                "period": {"type": "integer", "default": 300},
                "minutes": {"type": "integer", "default": 60},
            },
            "required": ["instance_id"],
        },
    ),
    Tool(
        name="get_disk_usage",
        description="Retrieve disk usage metrics for an instance (requires CloudWatch Agent).",
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "EC2 instance ID."},
                "mount_path": {
                    "type": "string",
                    "description": "Filesystem mount point (default '/').",
                    "default": "/",
                },
                "period": {"type": "integer", "default": 300},
                "minutes": {"type": "integer", "default": 60},
            },
            "required": ["instance_id"],
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

    if name == "get_cpu_metrics":
        result = await get_cpu_metrics(
            instance_id=arguments["instance_id"],
            period=arguments.get("period", 300),
            minutes=arguments.get("minutes", 60),
        )
    elif name == "get_cpu_metrics_for_instances":
        result = await get_cpu_metrics_for_instances(
            instance_ids=arguments["instance_ids"],
            period=arguments.get("period", 300),
            minutes=arguments.get("minutes", 60),
        )
    elif name == "get_memory_metrics":
        result = await get_memory_metrics(
            instance_id=arguments["instance_id"],
            period=arguments.get("period", 300),
            minutes=arguments.get("minutes", 60),
        )
    elif name == "get_disk_usage":
        result = await get_disk_usage(
            instance_id=arguments["instance_id"],
            mount_path=arguments.get("mount_path", "/"),
            period=arguments.get("period", 300),
            minutes=arguments.get("minutes", 60),
        )
    else:
        result = {"error": True, "message": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    logger.info("Starting Monitoring MCP server …")

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
