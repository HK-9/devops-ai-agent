"""
AWS Infrastructure MCP Server.

Exposes EC2 management tools over the Model Context Protocol.
Transport: streamable-http (for AgentCore Runtime hosting).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from tools import (
    describe_ec2_instance,
    diagnose_instance,
    list_ec2_instances,
    remediate_disk_full,
    remediate_high_cpu,
    remediate_high_memory,
    restart_ec2_instance,
    run_ssm_command,
)
from aws_helpers import setup_logging

logger = setup_logging("mcp-server.aws-infra")

# ── Server setup ─────────────────────────────────────────────────────────

mcp = FastMCP("aws-infra-server", host="0.0.0.0", stateless_http=True)

# ── Tool registrations ───────────────────────────────────────────────────


@mcp.tool()
async def list_ec2_instances_tool(
    state_filter: str = "running",
    tag_filters: str = "",
    max_results: int = 50,
) -> str:
    """List EC2 instances filtered by state and/or tags.
    Returns instance ID, type, state, IPs, and tags.
    """
    result = await list_ec2_instances(
        state_filter=state_filter,
        tag_filters=tag_filters,
        max_results=max_results,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def describe_ec2_instance_tool(instance_id: str) -> str:
    """Get detailed information about a single EC2 instance, including
    network, security groups, IAM role, and EBS volumes.
    """
    result = await describe_ec2_instance(instance_id=instance_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def restart_ec2_instance_tool(instance_id: str) -> str:
    """Restart (stop + start) an EC2 instance. Use with caution —
    prefer confirming with the user first.
    """
    result = await restart_ec2_instance(instance_id=instance_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def run_ssm_command_tool(
    instance_id: str,
    shell_command: str,
    timeout_seconds: int = 60,
) -> str:
    """Run a shell command on an EC2 instance via SSM.
    The instance must have the SSM Agent running.
    Returns stdout, stderr, and command status.
    """
    result = await run_ssm_command(
        instance_id=instance_id,
        command=shell_command,
        timeout_seconds=timeout_seconds,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def diagnose_instance_tool(instance_id: str) -> str:
    """Run a full diagnostic suite on an EC2 instance via SSM.
    Collects: top CPU/memory processes, disk usage, memory info,
    uptime/load, and active connections.
    """
    result = await diagnose_instance(instance_id=instance_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def remediate_high_cpu_tool(instance_id: str, pid: int) -> str:
    """Kill a runaway CPU process on an instance by PID.
    Use after diagnose_instance confirms the offending process.
    """
    result = await remediate_high_cpu(instance_id=instance_id, pid=pid)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def remediate_disk_full_tool(instance_id: str) -> str:
    """Clean up disk space on an instance: old logs, temp files,
    package caches. Shows before/after disk usage.
    """
    result = await remediate_disk_full(instance_id=instance_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def remediate_high_memory_tool(instance_id: str, pid: int) -> str:
    """Kill a memory-hogging process on an instance by PID.
    Returns the kill result and current memory status.
    """
    result = await remediate_high_memory(instance_id=instance_id, pid=pid)
    return json.dumps(result, indent=2, default=str)


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over streamable-http transport."""
    logger.info("Starting AWS Infra MCP server (streamable-http) …")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
