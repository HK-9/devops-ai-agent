"""
AWS SNS MCP Server.

Exposes alerting tools with Teams-to-SNS failover over the
Model Context Protocol.
Transport: streamable-http (for AgentCore Runtime hosting).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from tools import send_alert_with_failover, request_approval
from aws_helpers import setup_logging

logger = setup_logging("mcp-server.sns")

# ── Server setup ─────────────────────────────────────────────────────────

mcp = FastMCP("sns-server", host="0.0.0.0", stateless_http=True)

# ── Tool registrations ───────────────────────────────────────────────────


@mcp.tool()
async def send_alert_with_failover_tool(subject: str, message: str) -> str:
    """Send an alert to Teams; if Teams is unavailable, fail over to AWS SNS.
    Returns which channel was used or whether both failed.
    """
    result = await send_alert_with_failover(
        subject=subject,
        message=message,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def request_approval_tool(
    instance_id: str,
    action_type: str,
    reason: str,
    details: str = "",
) -> str:
    """Request human approval for a remediation action via email with
    clickable APPROVE/REJECT links. Supported action_types: restart,
    disk_cleanup, kill_process, cache_clear.
    """
    result = await request_approval(
        instance_id=instance_id,
        action_type=action_type,
        reason=reason,
        details=details,
    )
    return json.dumps(result, indent=2, default=str)


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over streamable-http transport."""
    logger.info("Starting SNS MCP server (streamable-http) …")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
