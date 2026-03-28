"""
AgentCore runtime management — update, wait, validate.
"""

from __future__ import annotations

import time

from .config import role_arn, ecr_uri
from .console import log, Colors
from .aws import ac_client


def update_runtime(runtime_id: str, ecr_repo: str, tag: str,
                   role: str, *, protocol: str | None = "MCP") -> bool:
    """Update a runtime using read-merge-write to preserve existing config.

    Fetches the current runtime configuration, overlays only the container
    image URI, and writes back the full config.  This prevents the
    update_agent_runtime API (which is a full-replace) from silently wiping
    fields like protocolConfiguration or authorizerConfiguration.

    Args:
        runtime_id: The AgentCore runtime ID.
        ecr_repo: ECR repository name.
        tag: Image tag to deploy.
        role: IAM role name for the runtime.
        protocol: Server protocol ("MCP" for MCP servers, None for agents).
    """
    image_uri = f"{ecr_uri(ecr_repo)}:{tag}"
    ac = ac_client()

    log(f"\n  Updating runtime {runtime_id}...", Colors.BOLD)
    try:
        # READ — fetch current config
        current = ac.get_agent_runtime(agentRuntimeId=runtime_id)

        # MERGE — overlay only what changes
        env_vars = current.get("environmentVariables", {})
        env_vars["DEPLOY_VERSION"] = tag

        update_params = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeArtifact": {
                "containerConfiguration": {"containerUri": image_uri}
            },
            "roleArn": current.get("roleArn", role_arn(role)),
            "networkConfiguration": current.get(
                "networkConfiguration", {"networkMode": "PUBLIC"}
            ),
            "environmentVariables": env_vars,
        }

        # Preserve protocolConfiguration (critical for MCP servers)
        proto = current.get("protocolConfiguration")
        if proto:
            update_params["protocolConfiguration"] = proto
        elif protocol:
            update_params["protocolConfiguration"] = {"serverProtocol": protocol}

        # Preserve authorizerConfiguration (critical for JWT auth)
        auth = current.get("authorizerConfiguration")
        if auth:
            update_params["authorizerConfiguration"] = auth

        # Preserve lifecycleConfiguration if present
        lifecycle = current.get("lifecycleConfiguration")
        if lifecycle:
            update_params["lifecycleConfiguration"] = lifecycle

        # WRITE — full-replace with merged config
        ac.update_agent_runtime(**update_params)
        log(f"  Update submitted", Colors.GREEN)
        return True
    except Exception as exc:
        log(f"  Update FAILED: {exc}", Colors.RED)
        return False


def wait_for_ready(runtime_id: str, timeout: int = 300) -> bool:
    """Poll a runtime until READY or FAILED."""
    ac = ac_client()
    start = time.time()
    last_status = ""

    while time.time() - start < timeout:
        resp = ac.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp["status"]

        if status != last_status:
            elapsed = int(time.time() - start)
            uri = (
                resp.get("agentRuntimeArtifact", {})
                .get("containerConfiguration", {})
                .get("containerUri", "")
            )
            log(f"  [{elapsed}s] {status}  image={uri}")
            last_status = status

        if status == "READY":
            return True
        if status == "FAILED":
            log(f"  FAILED: {resp.get('statusReasons', 'unknown')}", Colors.RED)
            return False

        time.sleep(10)

    log(f"  Timed out after {timeout}s (last: {last_status})", Colors.YELLOW)
    return False


def validate_runtime(runtime_id: str, *, expect_mcp: bool = True) -> bool:
    """Post-deploy validation: verify critical fields were not wiped."""
    ac = ac_client()
    resp = ac.get_agent_runtime(agentRuntimeId=runtime_id)

    ok = True

    if expect_mcp:
        proto = resp.get("protocolConfiguration", {})
        if proto.get("serverProtocol") != "MCP":
            log(f"  VALIDATION FAILED: protocolConfiguration is {proto} (expected MCP)", Colors.RED)
            ok = False

    auth = resp.get("authorizerConfiguration", {})
    if expect_mcp and not auth:
        log(f"  VALIDATION FAILED: authorizerConfiguration is empty", Colors.RED)
        ok = False

    if ok:
        parts = ["protocol=MCP" if expect_mcp else ""]
        parts += ["auth=present" if auth else ""]
        detail = ", ".join(p for p in parts if p)
        log(f"  Config validated ({detail})", Colors.GREEN)

    return ok
