"""Web-app agent client — calls the deployed AgentCore runtime.

The web app is a **thin client**.  All agent logic (model config, tool
sanitisation, Nova workarounds, system prompt, retry logic) lives in the
deployed agent container (``deployments/agent/agent.py``).

Invocation modes
----------------
1. **Remote (production)** — ``AGENT_RUNTIME_ARN`` is set.  Calls the
   deployed agent via ``bedrock-agentcore:invoke_agent_runtime``.
   This is the correct architecture: the agent runs server-side and
   handles tool orchestration, model selection, and retries centrally.

2. **Local (development fallback)** — ``AGENT_RUNTIME_ARN`` is not set.
   Creates a local Strands Agent by importing from
   ``deployments/agent/agent.py``.  Useful when the container hasn't
   been deployed yet or for quick local iteration.

Environment variables
---------------------
AGENT_RUNTIME_ARN : str, optional
    ARN of the deployed AgentCore runtime.  When set, the web app
    forwards prompts via ``invoke_agent_runtime`` (recommended).
    Example: ``arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy``
AWS_REGION : str
    AWS region (default ``ap-southeast-2``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Load .env early so that env vars are available for all imports
# ---------------------------------------------------------------------------

_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip())

# Strip agent-registration vars so inline mode is used in local fallback
os.environ.pop("AGENT_ID", None)
os.environ.pop("AGENT_ALIAS_ID", None)

logger = logging.getLogger("web.agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENT_RUNTIME_ARN: str | None = os.environ.get("AGENT_RUNTIME_ARN")
AWS_REGION: str = os.environ.get("AWS_REGION", "ap-southeast-2")

# ---------------------------------------------------------------------------
# Remote mode — invoke_agent_runtime (production)
# ---------------------------------------------------------------------------


def _invoke_remote(prompt: str) -> Dict[str, Any]:
    """Call the deployed agent via Bedrock AgentCore ``invoke_agent_runtime``.

    The deployed agent container handles everything: model selection,
    tool name sanitisation, intent routing, retries, system prompt.
    We just send a prompt and read back the response.
    """
    import boto3

    try:
        client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)

        # AgentCore requires a unique session ID per conversation turn
        session_id = f"{uuid.uuid4()}-{uuid.uuid4()}"

        logger.info(
            "Invoking deployed agent (ARN=%s, prompt=%d chars)",
            AGENT_RUNTIME_ARN,
            len(prompt),
        )

        resp = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=json.dumps({"query": prompt.strip()}).encode(),
        )

        # Read the streaming response body
        body = resp.get("response")
        if hasattr(body, "read"):
            data = body.read().decode("utf-8")
        else:
            data = str(body)

        status_code = resp.get("statusCode", 200)

        # Try to parse as JSON (agent HTTP server returns JSON)
        try:
            parsed = json.loads(data)
            response_text = parsed.get("response", data)
            error = parsed.get("error")
        except (json.JSONDecodeError, TypeError):
            response_text = data
            error = None

        if status_code >= 400:
            return {
                "response": "",
                "error": error or f"Agent returned HTTP {status_code}",
            }

        if not response_text or not response_text.strip():
            return {"response": "", "error": "Agent returned an empty response"}

        logger.info("Agent responded (%d chars)", len(response_text))
        return {"response": response_text, "error": None}

    except Exception:
        logger.exception("Remote agent invocation failed")
        return {
            "response": "",
            "error": "Remote agent invocation failed — check server logs",
        }


# ---------------------------------------------------------------------------
# Local mode — fallback for development when agent is not deployed
# ---------------------------------------------------------------------------

_agent = None
_agent_lock = threading.Lock()
_invoke_lock = threading.Lock()


def _ensure_deploy_agent_importable() -> None:
    """Add ``deployments/agent/`` to sys.path so we can import from it."""
    deploy_dir = str(Path(__file__).resolve().parent.parent / "deployments" / "agent")
    if deploy_dir not in sys.path:
        sys.path.insert(0, deploy_dir)


def _create_local_agent():
    """Instantiate a local Strands Agent using the deployed agent module."""
    _ensure_deploy_agent_importable()

    from agent import (  # deployments/agent/agent.py
        MODEL_ID,
        create_agent,
        create_gateway_mcp_client,
    )

    logger.info("Creating LOCAL agent (MODEL_ID=%s)", MODEL_ID)
    mcp_client = create_gateway_mcp_client()
    return create_agent(mcp_client, http_mode=True, streaming=True)


def _get_agent():
    """Return the module-level agent singleton, creating it on first call."""
    global _agent
    if _agent is not None:
        return _agent

    with _agent_lock:
        if _agent is not None:
            return _agent
        _agent = _create_local_agent()
        return _agent


def _reset_agent() -> None:
    """Destroy the agent singleton so the next call creates a fresh one."""
    global _agent
    with _agent_lock:
        logger.info("Resetting agent singleton")
        _agent = None


def _extract_text(result: Any) -> str:
    """Best-effort plain-text extraction from a Strands AgentResult."""
    # 1. result.message — Bedrock Converse message dict
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        texts = [b["text"] for b in message.get("content", []) if isinstance(b, dict) and "text" in b]
        if texts:
            return "\n\n".join(texts)

    # 2. result.content
    content = getattr(result, "content", None)
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                texts.append(block["text"])
            elif isinstance(block, str):
                texts.append(block)
        if texts:
            return "\n\n".join(texts)

    # 3. result.text
    text = getattr(result, "text", None)
    if text and isinstance(text, str):
        return text

    # 4. Fallback
    fallback = str(result).strip()
    return fallback if fallback else ""


def _invoke_local(prompt: str) -> Dict[str, Any]:
    """Invoke the local Strands Agent (development fallback)."""
    _ensure_deploy_agent_importable()

    from agent import set_tool_route  # deployments/agent/agent.py

    # Set intent-based tool route BEFORE invoking
    set_tool_route(prompt)

    with _invoke_lock:
        try:
            agent = _get_agent()
            logger.info("Invoking LOCAL agent with prompt (%d chars)", len(prompt))

            result = agent(prompt)
            response_text = _extract_text(result)

            if not response_text:
                logger.warning("Agent returned an empty response")
                return {"response": "", "error": "Agent returned an empty response"}

            logger.info("Agent responded (%d chars)", len(response_text))
            return {"response": response_text, "error": None}

        except Exception as exc:
            error_msg = str(exc).lower()

            if "invalid sequence" in error_msg and "tooluse" in error_msg:
                logger.warning("Resetting agent after Nova tool-use error")
                _reset_agent()

            logger.exception("Agent invocation failed")
            return {
                "response": "",
                "error": (
                    "The model produced an invalid tool-use sequence. Please try again."
                    if "invalid sequence" in error_msg
                    else "Agent invocation failed — check server logs"
                ),
            }


# ---------------------------------------------------------------------------
# Public API — used by web/routes.py
# ---------------------------------------------------------------------------


def invoke(prompt: str) -> Dict[str, Any]:
    """Send *prompt* to the agent and return ``{"response": ..., "error": ...}``.

    Routes to the deployed AgentCore runtime when ``AGENT_RUNTIME_ARN``
    is set (production), otherwise falls back to a local agent instance
    (development).
    """
    if not prompt or not prompt.strip():
        return {"response": "", "error": "Empty prompt"}

    if AGENT_RUNTIME_ARN:
        logger.debug("Using REMOTE mode (AGENT_RUNTIME_ARN=%s)", AGENT_RUNTIME_ARN)
        return _invoke_remote(prompt)
    else:
        logger.debug("Using LOCAL mode (AGENT_RUNTIME_ARN not set)")
        return _invoke_local(prompt)
