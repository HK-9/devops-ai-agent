"""
Integration test: MCP Server ↔ Client round-trip.

Starts an MCP server as a subprocess, connects a client, discovers
tools, and invokes one — verifying the full stdio communication path.
"""

from __future__ import annotations

import pytest

# These tests require MCP SDK and are run separately
pytestmark = pytest.mark.integration


class TestMCPRoundTrip:
    """End-to-end MCP server ↔ client tests.

    These tests start real MCP server subprocesses over stdio and
    validate discovery + invocation.

    Run with: ``pytest -m integration``
    """

    @pytest.mark.asyncio
    async def test_aws_infra_tool_discovery(self):
        """Connect to the AWS Infra MCP server and discover tools."""
        # This test requires: pip install -e ".[dev]"
        # and the MCP servers to be importable.
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command="python",
            args=["-m", "src.mcp_servers.aws_infra.server"],
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

                tool_names = [t.name for t in tools.tools]
                assert "list_ec2_instances" in tool_names
                assert "describe_ec2_instance" in tool_names
                assert "restart_ec2_instance" in tool_names
                assert len(tools.tools) == 3

    @pytest.mark.asyncio
    async def test_monitoring_tool_discovery(self):
        """Connect to the Monitoring MCP server and discover tools."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command="python",
            args=["-m", "src.mcp_servers.monitoring.server"],
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

                tool_names = [t.name for t in tools.tools]
                assert "get_cpu_metrics" in tool_names
                assert "get_cpu_metrics_for_instances" in tool_names
                assert "get_memory_metrics" in tool_names
                assert "get_disk_usage" in tool_names

    @pytest.mark.asyncio
    async def test_teams_tool_discovery(self):
        """Connect to the Teams MCP server and discover tools."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command="python",
            args=["-m", "src.mcp_servers.teams.server"],
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

                tool_names = [t.name for t in tools.tools]
                assert "send_teams_message" in tool_names
                assert "create_incident_notification" in tool_names
