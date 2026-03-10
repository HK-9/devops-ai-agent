"""Flask application for the DevOps AI Agent Web UI."""

import logging
import os
from pathlib import Path

# Force-load .env BEFORE any settings import so .env values
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

from src.agent.config import settings  # noqa: E402

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
        host=settings.web_host,
        port=settings.web_port,
        debug=settings.web_debug,
    )
