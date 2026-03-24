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
    list_ec2_instances,
    restart_ec2_instance,
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


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over streamable-http transport."""
    logger.info("Starting AWS Infra MCP server (streamable-http) …")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
