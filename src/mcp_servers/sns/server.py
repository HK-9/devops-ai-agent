"""
AWS SNS MCP Server.

Exposes alerting tools with Teams-to-SNS failover over the
Model Context Protocol.
Transport: streamable-http (for AgentCore Runtime hosting).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from src.mcp_servers.sns.tools import send_alert_with_failover
from src.utils.aws_helpers import setup_logging

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


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over streamable-http transport."""
    logger.info("Starting SNS MCP server (streamable-http) …")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
