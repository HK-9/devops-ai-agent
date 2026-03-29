"""Flask application for the DevOps AI Agent Web UI.

Uses the Strands Agent (via web.agent) connected to the AgentCore MCP
Gateway — no dependency on the legacy ``src/`` package.
"""

import logging
import os
from pathlib import Path

# Force-load .env BEFORE any other imports
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip()

os.environ.pop("AGENT_ID", None)
os.environ.pop("AGENT_ALIAS_ID", None)

from flask import Flask  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.secret_key = "incident-agent-dev-key-change-in-production"

    from web.routes import register_routes
    register_routes(app)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(
        host=os.environ.get("WEB_HOST", "0.0.0.0"),
        port=int(os.environ.get("WEB_PORT", "5001")),
        debug=os.environ.get("WEB_DEBUG", "true").lower() == "true",
    )
 