"""
Microsoft Teams MCP Server.

Exposes notification tools over the Model Context Protocol.
Transport: streamable-http (for AgentCore Runtime hosting).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from tools import (
    create_incident_notification,
    send_teams_message,
)
from aws_helpers import setup_logging

logger = setup_logging("mcp-server.teams")

# ── Server setup ─────────────────────────────────────────────────────────

mcp = FastMCP("teams-server", host="0.0.0.0", stateless_http=True)

# ── Tool registrations ───────────────────────────────────────────────────


@mcp.tool()
async def send_teams_message_tool(message: str) -> str:
    """Send a plain-text message to the configured Microsoft Teams channel."""
    result = await send_teams_message(message=message)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def create_incident_notification_tool(
    severity: str,
    instance_id: str,
    alarm_name: str,
    metric_value: str,
    summary: str,
    actions_taken: str = "None yet",
) -> str:
    """Send a structured incident Adaptive Card to Teams with severity,
    instance details, metric snapshot, summary, and actions taken.
    """
    result = await create_incident_notification(
        severity=severity,
        instance_id=instance_id,
        alarm_name=alarm_name,
        metric_value=metric_value,
        summary=summary,
        actions_taken=actions_taken,
    )
    return json.dumps(result, indent=2, default=str)


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over streamable-http transport."""
    logger.info("Starting Teams MCP server (streamable-http) …")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
