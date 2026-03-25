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
    diagnose_instance,
    list_ec2_instances,
    remediate_disk_full,
    remediate_high_cpu,
    remediate_high_memory,
    restart_ec2_instance,
    run_ssm_command,
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
                    "type": "string",
                    "description": "Optional tag filters as comma-separated key=value pairs, e.g. 'Name=web,Env=prod'. Leave empty for no tag filtering.",
                    "default": "",
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
    Tool(
        name="run_ssm_command",
        description=(
            "Run a shell command on an EC2 instance via SSM. "
            "The instance must have SSM Agent running and an IAM role with "
            "AmazonSSMManagedInstanceCore. Use for remote diagnostics."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Target EC2 instance ID.",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute, e.g. 'top -bn1 | head -20'.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Max seconds to wait (default 60).",
                    "default": 60,
                },
            },
            "required": ["instance_id", "command"],
        },
    ),
    Tool(
        name="diagnose_instance",
        description=(
            "Run a full diagnostic suite on an EC2 instance via SSM: "
            "top CPU processes, top memory processes, disk usage, memory info, "
            "uptime/load, and active connections."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Target EC2 instance ID.",
                },
            },
            "required": ["instance_id"],
        },
    ),
    Tool(
        name="remediate_high_cpu",
        description=(
            "Kill a runaway process on an instance by PID via SSM. "
            "Only use after diagnosing the instance and confirming the PID."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Target EC2 instance ID.",
                },
                "pid": {
                    "type": "string",
                    "description": "Process ID to kill.",
                },
            },
            "required": ["instance_id", "pid"],
        },
    ),
    Tool(
        name="remediate_disk_full",
        description=(
            "Clean up disk space on an instance via SSM: removes old logs, "
            "temp files, and package caches. Reports space before and after."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Target EC2 instance ID.",
                },
            },
            "required": ["instance_id"],
        },
    ),
    Tool(
        name="remediate_high_memory",
        description=(
            "Kill a memory-hogging process by PID and report memory status. "
            "Only use after diagnosing the instance and confirming the PID."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Target EC2 instance ID.",
                },
                "pid": {
                    "type": "string",
                    "description": "Process ID to kill.",
                },
            },
            "required": ["instance_id", "pid"],
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
    elif name == "run_ssm_command":
        result = await run_ssm_command(
            instance_id=arguments["instance_id"],
            command=arguments["command"],
            timeout_seconds=arguments.get("timeout_seconds", 60),
        )
    elif name == "diagnose_instance":
        result = await diagnose_instance(instance_id=arguments["instance_id"])
    elif name == "remediate_high_cpu":
        result = await remediate_high_cpu(
            instance_id=arguments["instance_id"],
            pid=arguments["pid"],
        )
    elif name == "remediate_disk_full":
        result = await remediate_disk_full(instance_id=arguments["instance_id"])
    elif name == "remediate_high_memory":
        result = await remediate_high_memory(
            instance_id=arguments["instance_id"],
            pid=arguments["pid"],
        )
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
 