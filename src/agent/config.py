"""
Environment-based configuration for the DevOps AI Agent.

Uses pydantic-settings to load values from environment variables
with sensible defaults for local development.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration loaded from environment variables."""

    # ── AWS ──────────────────────────────────────────────────────────────
    aws_region: str = "us-east-1"
    aws_profile: str | None = None  # Use default credential chain if None

    # ── Bedrock / AgentCore ──────────────────────────────────────────────
    bedrock_model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    agent_id: str | None = None  # Set after registering with AgentCore
    agent_alias_id: str | None = None

    # ── MCP Transport ────────────────────────────────────────────────────
    mcp_transport: str = "stdio"  # "stdio" | "sse"
    mcp_aws_infra_command: str = "python -m src.mcp_servers.aws_infra.server"
    mcp_monitoring_command: str = "python -m src.mcp_servers.monitoring.server"
    mcp_teams_command: str = "python -m src.mcp_servers.teams.server"
    mcp_sns_command: str = "python -m src.mcp_servers.sns.server"

    # ── Teams ────────────────────────────────────────────────────────────
    teams_webhook_url: str = ""

    # ── SNS ──────────────────────────────────────────────────────────────
    sns_topic_arn: str = ""

    alert_email: str = ""
    # ── Timeouts & Limits ────────────────────────────────────────────────
    tool_timeout_seconds: int = 30
    lambda_timeout_seconds: int = 300
    max_reasoning_turns: int = 15

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"

    # ── Web ──────────────────────────────────────────────────────────────
    web_host: str = "0.0.0.0"
    web_port: int = 5001
    web_debug: bool = True

    model_config = {
        "env_prefix": "",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# Singleton — import this everywhere
settings = Settings()
