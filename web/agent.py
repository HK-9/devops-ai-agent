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
    Bedrock model identifier (default ``amazon.nova-lite-v1:0``).
AWS_REGION : str
    AWS region for Bedrock and the gateway (default ``ap-southeast-2``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

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

MODEL_ID: str = os.environ.get("MODEL_ID", "amazon.nova-lite-v1:0")

AWS_REGION: str = os.environ.get("AWS_REGION", "ap-southeast-2")

SYSTEM_PROMPT: str = (
    "You are a DevOps AI Agent with access to AWS infrastructure tools via MCP. "
    "Help the user with their request.  Be concise and actionable.  "
    "When diagnosing issues, always call diagnose_instance_tool first.  "
    "Include raw metric values and instance IDs in your answers."
)

# ---------------------------------------------------------------------------
# Nova-safe Bedrock model (normalises streaming chunks + tool routing)
# ---------------------------------------------------------------------------

# Maximum retries for Nova "invalid tool-use sequence" errors
NOVA_MAX_RETRIES: int = int(os.environ.get("NOVA_MAX_RETRIES", "3"))
NOVA_RETRY_DELAY: float = float(os.environ.get("NOVA_RETRY_DELAY", "1.0"))

# ---------------------------------------------------------------------------
# Intent-based tool routing for Nova models
# ---------------------------------------------------------------------------
# Nova models cannot reliably handle more than ~5-6 tools at once.
# The MCP gateway exposes 18 tools across 5 targets.  We detect the user's
# intent and only forward the relevant subset of tools to Bedrock.
#
# Tool names from the gateway follow the pattern:
#   {target}___{tool_name}   (triple-underscore separator)
#
# The mapping below groups tool *substrings* by intent category.
# ---------------------------------------------------------------------------

TOOL_CATEGORIES: Dict[str, List[str]] = {
    "ec2": [
        "list_ec2_instances",
        "describe_ec2_instance",
        "restart_ec2_instance",
        "diagnose_instance",
    ],
    "monitoring": [
        "get_cpu_metrics",
        "get_memory_metrics",
        "get_disk_usage",
        "get_cpu_metrics_for_instances",
    ],
    "remediation": [
        "remediate_high_cpu",
        "remediate_high_memory",
        "remediate_disk_full",
        "run_ssm_command",
    ],
    "notification": [
        "send_teams_message",
        "create_incident_notification",
        "send_alert_with_failover",
    ],
    "approval": [
        "request_approval",
        "check_approval_status",
        "update_approval_status",
    ],
}

# Intent keywords → tool categories
INTENT_KEYWORDS: Dict[str, List[str]] = {
    "ec2": [
        "list",
        "instances",
        "ec2",
        "describe",
        "show",
        "which",
        "what",
        "all instances",
        "running",
        "stopped",
    ],
    "monitoring": [
        "cpu",
        "memory",
        "mem",
        "disk",
        "metric",
        "usage",
        "utilization",
        "monitor",
        "health",
    ],
    "remediation": [
        "remediate",
        "fix",
        "kill",
        "cleanup",
        "clean up",
        "free disk",
        "ssm",
        "run command",
        "execute",
        "shell",
    ],
    "notification": [
        "teams",
        "notify",
        "alert",
        "incident",
        "send message",
        "send alert",
        "send notification",
        "email",
    ],
    "approval": [
        "approval",
        "approve",
        "request approval",
    ],
}

# Default categories when no clear intent is detected
DEFAULT_CATEGORIES = ["ec2", "monitoring"]

# Thread-local storage to pass the tool-route hint from invoke() → _stream()
_tool_route = threading.local()


def _detect_categories(prompt: str) -> List[str]:
    """Detect which tool categories are relevant for *prompt*."""
    prompt_lower = prompt.lower()
    matched: List[str] = []

    for category, keywords in INTENT_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            matched.append(category)

    # Diagnose queries need both ec2 and monitoring tools
    if any(w in prompt_lower for w in ("diagnose", "troubleshoot", "health check")):
        for cat in ("ec2", "monitoring"):
            if cat not in matched:
                matched.append(cat)

    return matched or DEFAULT_CATEGORIES


def _get_allowed_tool_names(categories: List[str]) -> List[str]:
    """Return a flat list of tool-name substrings for the given categories."""
    names: List[str] = []
    for cat in categories:
        names.extend(TOOL_CATEGORIES.get(cat, []))
    return names


def _filter_tool_specs(
    tool_specs: Optional[List[dict]],
    allowed_names: Optional[List[str]],
) -> Optional[List[dict]]:
    """Return only those tool specs whose names match *allowed_names*.

    Matching is by substring so that gateway-prefixed names like
    ``aws-infra-target___list_ec2_instances_tool`` match against
    ``list_ec2_instances``.
    """
    if not tool_specs or not allowed_names:
        return tool_specs

    filtered: List[dict] = []
    for spec in tool_specs:
        tool_spec = spec.get("toolSpec", spec)
        name = tool_spec.get("name", "")
        if any(allowed in name for allowed in allowed_names):
            filtered.append(spec)

    # Safety: if filtering removed everything, keep the first 5 as fallback
    if not filtered and tool_specs:
        logger.warning("Tool filter matched nothing — using first 5 tools as fallback")
        filtered = tool_specs[:5]

    return filtered


def _simplify_schema(schema: dict, depth: int = 0, max_depth: int = 2) -> dict:
    """Recursively simplify a JSON Schema dict for Nova compatibility.

    Nova models can struggle with deeply nested schemas, ``$ref``,
    ``anyOf``, ``oneOf``, ``allOf``.  This function flattens the schema
    to *max_depth* levels and strips unsupported keywords.
    """
    if not isinstance(schema, dict):
        return schema

    simplified: dict = {}

    for key in ("type", "description", "default", "enum"):
        if key in schema:
            val = schema[key]
            if key == "enum" and isinstance(val, list) and len(val) > 10:
                val = val[:10]
            simplified[key] = val

    if depth >= max_depth:
        if simplified.get("type") in ("object", "array"):
            return {"type": "string", "description": simplified.get("description", "JSON value")}
        return simplified

    if "properties" in schema and isinstance(schema["properties"], dict):
        simplified["type"] = "object"
        simplified["properties"] = {
            k: _simplify_schema(v, depth + 1, max_depth) for k, v in schema["properties"].items()
        }
        if "required" in schema:
            simplified["required"] = schema["required"]

    if "items" in schema:
        simplified["type"] = "array"
        simplified["items"] = _simplify_schema(schema["items"], depth + 1, max_depth)

    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema and combinator not in simplified:
            branches = schema[combinator]
            if isinstance(branches, list) and branches:
                simplified.update(_simplify_schema(branches[0], depth, max_depth))
            break

    if "type" not in simplified:
        simplified["type"] = "string"

    return simplified


def _sanitize_tool_name(name: str) -> str:
    """Sanitise a tool name for Bedrock Converse API / Nova compatibility.

    The Converse API expects tool names matching ``[a-zA-Z][a-zA-Z0-9_]*``.
    MCP gateway names like ``aws-infra-target___list_ec2_instances_tool``
    contain **hyphens** which cause Nova to produce invalid tool-use
    sequences.  Replace hyphens (and any other non-alphanumeric/underscore
    characters) with underscores.
    """
    import re as _re

    sanitized = _re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Ensure it starts with a letter
    if sanitized and not sanitized[0].isalpha():
        sanitized = "t_" + sanitized
    return sanitized


def _sanitize_tool_specs(tool_specs: Optional[List[dict]]) -> tuple[Optional[List[dict]], Dict[str, str]]:
    """Return a sanitised deep-copy of *tool_specs* and a reverse name map.

    Returns
    -------
    (sanitized_specs, name_map)
        ``name_map`` maps **sanitised** names back to **original** names so
        that tool-call events from the model can be translated back to the
        names the Strands SDK tool registry expects.

    Transformations applied:
    1. Filters tools using thread-local route hint (set by :func:`invoke`).
    2. **Sanitises tool names** — replaces hyphens with underscores (root
       cause of Nova's "invalid tool-use sequence" error).
    3. **Removes ``outputSchema``** — not supported by Bedrock Converse API.
    4. Truncates long descriptions.
    5. Simplifies nested JSON Schemas via :func:`_simplify_schema`.
    """
    if not tool_specs:
        return tool_specs, {}

    # --- Step 1: intent-based filtering ---
    allowed_names = getattr(_tool_route, "allowed_tools", None)
    if allowed_names:
        tool_specs = _filter_tool_specs(tool_specs, allowed_names)
        logger.info(
            "Tool routing: %d tools selected (allowed patterns: %s)",
            len(tool_specs) if tool_specs else 0,
            allowed_names[:6],
        )

    name_map: Dict[str, str] = {}
    sanitized: List[dict] = []
    for spec in tool_specs or []:
        spec = copy.deepcopy(spec)
        tool_spec = spec.get("toolSpec", spec)

        # --- Sanitise tool name (hyphens → underscores) ---
        original_name = tool_spec.get("name", "")
        clean_name = _sanitize_tool_name(original_name)
        if clean_name != original_name:
            logger.debug("Sanitised tool name: %s → %s", original_name, clean_name)
            tool_spec["name"] = clean_name
            name_map[clean_name] = original_name

        # --- Remove outputSchema (not supported by Converse API) ---
        tool_spec.pop("outputSchema", None)

        # --- Truncate long descriptions ---
        desc = tool_spec.get("description", "")
        if len(desc) > 300:
            tool_spec["description"] = desc[:300] + "..."

        # --- Simplify input schema ---
        input_schema = tool_spec.get("inputSchema", {})
        json_schema = input_schema.get("json")
        if isinstance(json_schema, dict):
            input_schema["json"] = _simplify_schema(json_schema)

        sanitized.append(spec)

    return sanitized, name_map


class _NovaBedrockModel(BedrockModel):
    """BedrockModel subclass with Nova-specific workarounds.

    * **Normalises streaming chunks** — Nova sends tool-use input deltas
      as pre-parsed *dicts*; the Strands SDK expects JSON *strings*.
    * **Intent-based tool routing** — only the relevant subset of tools
      (4-6) is forwarded to Bedrock, avoiding the "invalid tool-use
      sequence" error Nova produces when presented with 18 tools.
    * **Automatic retry** — if the error still occurs, the call is retried
      up to ``NOVA_MAX_RETRIES`` times with exponential back-off.
    * **Graceful fallback** — after all retries, one final attempt is made
      without tools so the user gets a text answer instead of a 500.
    """

    @staticmethod
    def _is_tool_use_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "invalid sequence" in msg and "tooluse" in msg

    @staticmethod
    def _normalize_chunk(chunk: Dict[str, Any], name_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Normalise a streaming chunk for Nova compatibility.

        * Serialises dict tool-use inputs to JSON strings.
        * Restores original (hyphenated) tool names so the Strands SDK
          tool registry can find them.
        """
        # --- Restore original tool name in contentBlockStart ---
        if name_map:
            cbs = chunk.get("contentBlockStart")
            if cbs is not None:
                tu_start = cbs.get("start", {}).get("toolUse")
                if tu_start and tu_start.get("name") in name_map:
                    tu_start["name"] = name_map[tu_start["name"]]

        # --- Serialise dict tool-use inputs ---
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

        # ---- Sanitise + route tool specs for Nova ----
        sanitized_specs, name_map = _sanitize_tool_specs(tool_specs)

        def normalizing_callback(event=None):
            if event is not None:
                event = self._normalize_chunk(event, name_map)
            original_callback(event)

        if sanitized_specs is not None and tool_specs is not None:
            logger.debug(
                "Tool specs: %d original → %d after routing/sanitisation",
                len(tool_specs),
                len(sanitized_specs),
            )

        last_error: Exception | None = None

        for attempt in range(1, NOVA_MAX_RETRIES + 1):
            try:
                super()._stream(
                    normalizing_callback,
                    messages,
                    sanitized_specs,
                    system_prompt_content,
                    tool_choice,
                )
                return  # Success
            except Exception as exc:
                if not self._is_tool_use_error(exc):
                    raise

                last_error = exc
                delay = NOVA_RETRY_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Nova tool-use error (attempt %d/%d). Retrying in %.1fs …",
                    attempt,
                    NOVA_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

        # All retries exhausted — final attempt without tools
        logger.warning(
            "All %d retries failed. Final attempt without tools.",
            NOVA_MAX_RETRIES,
        )
        try:
            super()._stream(
                normalizing_callback,
                messages,
                tool_specs=None,
                system_prompt_content=system_prompt_content,
                tool_choice=None,
            )
        except Exception:
            logger.error("Fallback without tools also failed")
            raise last_error  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SigV4 auth helper (lazy import — only needed when gateway uses IAM auth)
# ---------------------------------------------------------------------------


def _make_sigv4_auth():
    """Return a SigV4 httpx auth instance, or *None* if unavailable."""
    # 1. Try the deploy_agent package
    try:
        from sigv4_auth import BotoSigV4Auth

        return BotoSigV4Auth(region=AWS_REGION, service="bedrock-agentcore")
    except ImportError:
        pass

    # 2. Try a relative import from the project root
    try:
        import importlib
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

    sigv4 = _make_sigv4_auth()

    transport_kwargs: Dict[str, Any] = {"url": GATEWAY_URL}
    if sigv4 is not None:
        transport_kwargs["auth"] = sigv4

    mcp_client = MCPClient(lambda: streamablehttp_client(**transport_kwargs))

    # Use Nova-safe subclass for Nova models, plain BedrockModel otherwise
    model_cls = _NovaBedrockModel if "nova" in MODEL_ID.lower() else BedrockModel
    model = model_cls(
        region_name=AWS_REGION,
        model_id=MODEL_ID,
        # Generous token budget — Nova can truncate mid-tool-call with low
        # limits, producing "invalid sequence" errors.
        max_tokens=8192,
        # Streaming is more resilient for Nova tool-use.
        streaming=True,
    )

    agent = Agent(
        model=model,
        tools=[mcp_client],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=null_callback_handler,
    )

    logger.info("Strands Agent initialised successfully")
    return agent


def _get_agent() -> Agent:
    global _agent
    if _agent is not None:
        return _agent

    with _agent_lock:
        if _agent is not None:
            return _agent
        _agent = _create_agent()
        return _agent


def _reset_agent() -> None:
    """Destroy the agent singleton so the next call creates a fresh one."""
    global _agent
    with _agent_lock:
        logger.info("Resetting agent singleton for fresh re-initialisation")
        _agent = None


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
    """
    if not prompt or not prompt.strip():
        return {"response": "", "error": "Empty prompt"}

    # --- Set intent-based tool route for Nova models ---
    if "nova" in MODEL_ID.lower():
        categories = _detect_categories(prompt)
        allowed = _get_allowed_tool_names(categories)
        _tool_route.allowed_tools = allowed
        logger.info(
            "Intent categories: %s → %d tool patterns",
            categories,
            len(allowed),
        )
    else:
        _tool_route.allowed_tools = None

    with _invoke_lock:
        try:
            agent = _get_agent()
            logger.info("Invoking agent with prompt (%d chars)", len(prompt))
            logger.debug("Prompt: %.500s", prompt)

            result = agent(prompt)

            response_text = _extract_text(result)

            if not response_text:
                logger.warning("Agent returned an empty response")
                return {"response": "", "error": "Agent returned an empty response"}

            logger.info("Agent responded (%d chars)", len(response_text))
            logger.debug("Response: %.500s", response_text)
            return {"response": response_text, "error": None}

        except Exception as exc:
            error_msg = str(exc).lower()

            if "invalid sequence" in error_msg and "tooluse" in error_msg:
                logger.warning("Resetting agent after persistent Nova tool-use error")
                _reset_agent()

            logger.exception("Agent invocation failed")
            return {
                "response": "",
                "error": (
                    "The model produced an invalid tool-use sequence. "
                    "This is a known Nova model limitation with many tools. "
                    "Please try again — the request will be retried automatically."
                    if "invalid sequence" in error_msg
                    else "Agent invocation failed — check server logs for details"
                ),
            }


# ---------------------------------------------------------------------------
# Response extraction helpers
# ---------------------------------------------------------------------------


def _extract_text(result: Any) -> str:
    """Best-effort extraction of plain text from a Strands AgentResult."""

    # 1. result.message — Bedrock Converse message dict
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content_blocks = message.get("content", [])
        texts = []
        for block in content_blocks:
            if isinstance(block, dict) and "text" in block:
                texts.append(block["text"])
        if texts:
            return "\n\n".join(texts)

    # 2. result.content — some SDK versions surface a content list
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

    # 3. result.text — convenience property in newer SDK versions
    text = getattr(result, "text", None)
    if text and isinstance(text, str):
        return text

    # 4. Fallback — str(result)
    fallback = str(result).strip()
    if fallback:
        return fallback

    return ""
