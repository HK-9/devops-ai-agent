"""
Unit tests for Teams notification MCP tools.

Uses httpx mock to avoid real webhook calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.agent.config import settings
from src.mcp_servers.teams.tools import (
    create_incident_notification,
    send_teams_message,
)


@pytest.fixture
def mock_webhook_url(monkeypatch):
    """Set a fake Teams webhook URL via settings (loaded at import time)."""
    monkeypatch.setattr(
        settings,
        "teams_webhook_url",
        "https://outlook.office.com/webhook/test-hook",
    )


@pytest.mark.unit
class TestSendTeamsMessage:
    """Tests for the send_teams_message tool."""

    @pytest.mark.asyncio
    async def test_send_message_success(self, mock_webhook_url):
        with patch("src.utils.teams_webhook._post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": True, "status_code": 200}
            result = await send_teams_message("Server CPU is elevated")
            assert result["ok"] is True
            mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_no_url(self, monkeypatch):
        monkeypatch.setattr(settings, "teams_webhook_url", "")
        result = await send_teams_message("Test message")
        assert result["ok"] is False
        assert "not configured" in result.get("error", "")


@pytest.mark.unit
class TestCreateIncidentNotification:
    """Tests for the create_incident_notification tool."""

    @pytest.mark.asyncio
    async def test_create_notification_success(self, mock_webhook_url):
        with patch("src.utils.teams_webhook._post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": True, "status_code": 200}
            result = await create_incident_notification(
                severity="CRITICAL",
                instance_id="i-0abc123",
                alarm_name="high-cpu",
                metric_value="CPU 92.3%",
                summary="CPU spiked on prod instance",
                actions_taken="Investigating",
            )
            assert result["ok"] is True
            assert result["severity"] == "CRITICAL"
