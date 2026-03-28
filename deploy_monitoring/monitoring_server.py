"""
CloudWatch Monitoring MCP Server.

Exposes metrics retrieval tools over the Model Context Protocol.
Transport: streamable-http (for AgentCore Runtime hosting).
"""

from __future__ import annotations

import json
from typing import List

from mcp.server.fastmcp import FastMCP

from tools import (
    get_cpu_metrics,
    get_cpu_metrics_for_instances,
    get_disk_usage,
    get_memory_metrics,
)
from aws_helpers import setup_logging

logger = setup_logging("mcp-server.monitoring")

# ── Server setup ─────────────────────────────────────────────────────────

mcp = FastMCP("monitoring-server", host="0.0.0.0", stateless_http=True)

# ── Tool registrations ───────────────────────────────────────────────────


@mcp.tool()
async def get_cpu_metrics_tool(
    instance_id: str,
    period: int = 300,
    minutes: int = 60,
) -> str:
    """Retrieve CPU utilization metrics for a single EC2 instance over a time window."""
    result = await get_cpu_metrics(
        instance_id=instance_id,
        period=period,
        minutes=minutes,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def get_cpu_metrics_for_instances_tool(
    instance_ids: List[str],
    period: int = 300,
    minutes: int = 60,
) -> str:
    """Batch-retrieve CPU utilization for multiple instances in one call."""
    result = await get_cpu_metrics_for_instances(
        instance_ids=instance_ids,
        period=period,
        minutes=minutes,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def get_memory_metrics_tool(
    instance_id: str,
    period: int = 300,
    minutes: int = 60,
) -> str:
    """Retrieve memory utilization for an instance (requires CloudWatch Agent)."""
    result = await get_memory_metrics(
        instance_id=instance_id,
        period=period,
        minutes=minutes,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def get_disk_usage_tool(
    instance_id: str,
    mount_path: str = "/",
    period: int = 300,
    minutes: int = 60,
) -> str:
    """Retrieve disk usage metrics for an instance (requires CloudWatch Agent)."""
    result = await get_disk_usage(
        instance_id=instance_id,
        mount_path=mount_path,
        period=period,
        minutes=minutes,
    )
    return json.dumps(result, indent=2, default=str)


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over streamable-http transport."""
    logger.info("Starting Monitoring MCP server (streamable-http) …")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
