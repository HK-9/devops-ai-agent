"""Strands Agent wrapper for the DevOps AI Agent Web UI.

Provides a single public function :func:`invoke` that lazily initialises a
Strands Agent connected to the AgentCore MCP Gateway and forwards prompts
to it.  The agent is a module-level singleton protected by a lock so that
concurrent Flask request threads don't race on initialisation.

Environment variables
---------------------
GATEWAY_URL : str
    Streamable-HTTP endpoint for the AgentCore MCP Gateway.
MODEL_ID : str
    Bedrock model identifier (default ``amazon.nova-pro-v1:0``).
AWS_REGION : str
    AWS region for Bedrock and the gateway (default ``ap-southeast-2``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict

from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.agent.agent import null_callback_handler
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("web.agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GATEWAY_URL: str = os.environ.get(
    "GATEWAY_URL",
    "https://devopsagentgatewayv3-ar4lmz2x6t.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp",
)

MODEL_ID: str = os.environ.get("MODEL_ID", "apac.anthropic.claude-sonnet-4-20250514-v1:0")

AWS_REGION: str = os.environ.get("AWS_REGION", "ap-southeast-2")

SYSTEM_PROMPT: str = (
    "You are a DevOps AI Agent with access to AWS infrastructure tools via MCP. "
    "Help the user with their request.  Be concise and actionable.  "
    "When diagnosing issues, always call diagnose_instance_tool first.  "
    "Include raw metric values and instance IDs in your answers."
)

# ---------------------------------------------------------------------------
# Nova-safe Bedrock model (normalises streaming chunks)
# ---------------------------------------------------------------------------


class _NovaBedrockModel(BedrockModel):
    """BedrockModel subclass that normalises Amazon Nova streaming chunks.

    Nova sends tool-use input deltas as pre-parsed *dicts*, but the Strands
    SDK event loop expects JSON *strings* it can concatenate.  This subclass
    intercepts the raw stream and serialises any dict tool inputs before they
    reach the SDK's ``handle_content_block_delta()``.
    """

    @staticmethod
    def _normalize_chunk(chunk: Dict[str, Any]) -> Dict[str, Any]:
        cbd = chunk.get("contentBlockDelta")
        if cbd is not None:
            tool_use = cbd.get("delta", {}).get("toolUse")
            if tool_use is not None and isinstance(tool_use.get("input"), dict):
                tool_use["input"] = json.dumps(tool_use["input"])
        return chunk

    def _stream(
        self,
        callback,
        messages,
        tool_specs=None,
        system_prompt_content=None,
        tool_choice=None,
    ):
        original_callback = callback

        def normalizing_callback(event=None):
            if event is not None:
                event = self._normalize_chunk(event)
            original_callback(event)

        super()._stream(
            normalizing_callback,
            messages,
            tool_specs,
            system_prompt_content,
            tool_choice,
        )


# ---------------------------------------------------------------------------
# SigV4 auth helper (lazy import — only needed when gateway uses IAM auth)
# ---------------------------------------------------------------------------


def _make_sigv4_auth():
    """Return a SigV4 httpx auth instance, or *None* if unavailable.

    The ``sigv4_auth`` module lives in ``deploy_agent/`` and may not be on
    ``sys.path`` when the web app runs locally.  We try a few import
    strategies before falling back to ``None`` (unauthenticated).
    """
    # 1. Try the deploy_agent package (works when it's installed / on path)
    try:
        from sigv4_auth import BotoSigV4Auth

        return BotoSigV4Auth(region=AWS_REGION, service="bedrock-agentcore")
    except ImportError:
        pass

    # 2. Try a relative import from the project root
    try:
        import importlib  # noqa: E401
        import pathlib
        import sys

        deploy_dir = str(pathlib.Path(__file__).resolve().parent.parent / "deployments" / "agent")
        if deploy_dir not in sys.path:
            sys.path.insert(0, deploy_dir)
        mod = importlib.import_module("sigv4_auth")
        return mod.BotoSigV4Auth(region=AWS_REGION, service="bedrock-agentcore")
    except Exception:
        pass

    # 3. Inline minimal implementation using botocore directly
    try:
        import httpx
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.session import Session as BotocoreSession

        class _InlineSigV4Auth(httpx.Auth):
            def __init__(self, region: str, service: str) -> None:
                self._region = region
                self._service = service
                self._session = BotocoreSession()

            def auth_flow(self, request: httpx.Request):
                credentials = self._session.get_credentials().get_frozen_credentials()
                url = str(request.url)
                body = request.content if request.content else b""
                headers = dict(request.headers)
                headers.pop("host", None)
                aws_request = AWSRequest(method=request.method, url=url, data=body, headers=headers)
                SigV4Auth(credentials, self._service, self._region).add_auth(aws_request)
                for key, value in aws_request.headers.items():
                    request.headers[key] = value
                yield request

        return _InlineSigV4Auth(region=AWS_REGION, service="bedrock-agentcore")
    except Exception:
        logger.warning("SigV4 auth unavailable — gateway calls will be unsigned")
        return None


# ---------------------------------------------------------------------------
# Agent singleton
# ---------------------------------------------------------------------------

_agent: Agent | None = None
_agent_lock = threading.Lock()
_invoke_lock = threading.Lock()


def _create_agent() -> Agent:
    """Instantiate the Strands Agent with MCP Gateway tools."""
    logger.info("Initialising Strands Agent (model=%s, gateway=%s)", MODEL_ID, GATEWAY_URL)

    # Build SigV4 auth (may be None for local dev without IAM)
    sigv4 = _make_sigv4_auth()

    # MCP client — connects to the AgentCore Gateway over streamable HTTP
    transport_kwargs: Dict[str, Any] = {"url": GATEWAY_URL}
    if sigv4 is not None:
        transport_kwargs["auth"] = sigv4

    mcp_client = MCPClient(lambda: streamablehttp_client(**transport_kwargs))

    # Bedrock model — use the Nova-safe subclass for Nova models
    model_cls = _NovaBedrockModel if "nova" in MODEL_ID.lower() else BedrockModel
    model = model_cls(
        region_name=AWS_REGION,
        model_id=MODEL_ID,
        max_tokens=4096,
        # Non-streaming avoids chunked-transfer pitfalls in synchronous
        # Flask request threads and simplifies response extraction.
        streaming=False,
    )

    agent = Agent(
        model=model,
        tools=[mcp_client],
        system_prompt=SYSTEM_PROMPT,
        # Suppress default console callback — we're running headless.
        callback_handler=null_callback_handler,
    )

    logger.info("Strands Agent initialised successfully")
    return agent


def _get_agent() -> Agent:
    """Return the module-level agent singleton, creating it on first call."""
    global _agent
    if _agent is not None:
        return _agent

    with _agent_lock:
        # Double-checked locking — another thread may have initialised it
        # while we were waiting on the lock.
        if _agent is not None:
            return _agent
        _agent = _create_agent()
        return _agent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def invoke(prompt: str) -> Dict[str, Any]:
    """Send *prompt* to the Strands Agent and return a result dict.

    Returns
    -------
    dict
        ``{"response": "<text>", "error": None}`` on success, or
        ``{"response": "", "error": "<message>"}`` on failure.

    The function is **thread-safe**: a reentrant lock serialises calls so
    that the underlying Bedrock conversation state is never corrupted by
    concurrent Flask request threads.
    """
    if not prompt or not prompt.strip():
        return {"response": "", "error": "Empty prompt"}

    # Serialise agent invocations — the Strands Agent maintains internal
    # conversation state and is not designed for concurrent calls.
    with _invoke_lock:
        try:
            agent = _get_agent()
            logger.info("Invoking agent with prompt (%d chars)", len(prompt))
            logger.debug("Prompt: %.500s", prompt)

            result = agent(prompt)

            # Extract text from the agent result.  The Strands SDK returns
            # an AgentResult whose ``message`` attribute is the final
            # assistant message dict (Bedrock Converse format).
            response_text = _extract_text(result)

            if not response_text:
                logger.warning("Agent returned an empty response")
                return {"response": "", "error": "Agent returned an empty response"}

            logger.info("Agent responded (%d chars)", len(response_text))
            logger.debug("Response: %.500s", response_text)
            return {"response": response_text, "error": None}

        except Exception:
            logger.exception("Agent invocation failed")
            return {"response": "", "error": "Agent invocation failed — check server logs for details"}


# ---------------------------------------------------------------------------
# Response extraction helpers
# ---------------------------------------------------------------------------


def _extract_text(result: Any) -> str:
    """Best-effort extraction of plain text from a Strands AgentResult.

    The SDK has evolved across versions so we try several access patterns
    to be resilient against minor API changes.
    """
    # 1. result may directly be convertible to str (some SDK versions)
    #    but we prefer structured extraction first.

    # 2. result.message — Bedrock Converse message dict
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content_blocks = message.get("content", [])
        texts = []
        for block in content_blocks:
            if isinstance(block, dict) and "text" in block:
                texts.append(block["text"])
        if texts:
            return "\n\n".join(texts)

    # 3. result.content — some SDK versions surface a content list
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

    # 4. result.text — convenience property in newer SDK versions
    text = getattr(result, "text", None)
    if text and isinstance(text, str):
        return text

    # 5. Fallback — str(result) often gives a usable summary
    fallback = str(result).strip()
    if fallback:
        return fallback

    return ""
