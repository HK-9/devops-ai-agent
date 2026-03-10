"""Flask application for the DevOps AI Agent Web UI."""

import logging
from flask import Flask

from src.agent.config import settings

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
