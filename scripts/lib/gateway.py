"""
Gateway target management — sync, create, update, recreate.
"""

from __future__ import annotations

import time

from .config import (
    GATEWAY_ID,
    GATEWAY_TARGETS,
    GATEWAY_CREDENTIAL_CONFIG,
    runtime_endpoint,
)
from .console import log, Colors
from .aws import ac_client


def sync_gateway_targets(server_configs: dict[str, dict]) -> bool:
    """Sync gateway targets for the given servers to trigger tool re-discovery.

    Args:
        server_configs: Mapping of server name -> server config dict.
                        Each config must have 'runtime_id'.
    """
    ac = ac_client()

    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  PHASE: Gateway Target Sync", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)

    # Fetch current targets
    resp = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    existing = {t["name"]: t for t in resp.get("items", [])}

    results = {}
    for name, server in server_configs.items():
        target_name = GATEWAY_TARGETS.get(name)
        if not target_name:
            continue

        endpoint = runtime_endpoint(server["runtime_id"])

        if target_name in existing:
            target = existing[target_name]
            tid = target["targetId"]

            if target["status"] in ("FAILED", "UPDATE_UNSUCCESSFUL"):
                # Delete and recreate failed targets
                log(f"\n  Recreating failed target {target_name} ({tid})...", Colors.BOLD)
                ac.delete_gateway_target(
                    gatewayIdentifier=GATEWAY_ID, targetId=tid,
                )
                time.sleep(5)
                r = ac.create_gateway_target(
                    gatewayIdentifier=GATEWAY_ID,
                    name=target_name,
                    targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}},
                    credentialProviderConfigurations=GATEWAY_CREDENTIAL_CONFIG,
                )
                results[name] = r["targetId"]
                log(f"  Recreated: {r['targetId']}", Colors.GREEN)
            else:
                # Update existing healthy target to trigger re-sync
                log(f"\n  Syncing {target_name} ({tid})...", Colors.BOLD)
                try:
                    ac.update_gateway_target(
                        gatewayIdentifier=GATEWAY_ID,
                        targetId=tid,
                        name=target_name,
                        targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}},
                        credentialProviderConfigurations=GATEWAY_CREDENTIAL_CONFIG,
                    )
                except Exception as exc:
                    # Fallback: delete and recreate to ensure credentials are preserved
                    log(f"  Update failed ({exc}), recreating...", Colors.YELLOW)
                    ac.delete_gateway_target(
                        gatewayIdentifier=GATEWAY_ID, targetId=tid,
                    )
                    time.sleep(5)
                    r = ac.create_gateway_target(
                        gatewayIdentifier=GATEWAY_ID,
                        name=target_name,
                        targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}},
                        credentialProviderConfigurations=GATEWAY_CREDENTIAL_CONFIG,
                    )
                    tid = r["targetId"]
                    log(f"  Recreated: {tid}", Colors.GREEN)
                results[name] = tid
        else:
            # Create new target
            log(f"\n  Creating target {target_name}...", Colors.BOLD)
            r = ac.create_gateway_target(
                gatewayIdentifier=GATEWAY_ID,
                name=target_name,
                targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}},
                credentialProviderConfigurations=GATEWAY_CREDENTIAL_CONFIG,
            )
            results[name] = r["targetId"]
            log(f"  Created: {r['targetId']}", Colors.GREEN)

    if not results:
        log("  No targets to sync.", Colors.YELLOW)
        return True

    # Wait for all targets to become READY
    log("\n  Waiting for targets to sync...", Colors.BOLD)
    start = time.time()
    timeout = 120

    while time.time() - start < timeout:
        time.sleep(15)
        resp = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        targets_by_id = {t["targetId"]: t for t in resp.get("items", [])}

        all_ready = True
        for sname, tid in results.items():
            t = targets_by_id.get(tid, {})
            status = t.get("status", "UNKNOWN")
            if status == "READY":
                continue
            elif status in ("FAILED", "UPDATE_UNSUCCESSFUL"):
                detail = ac.get_gateway_target(
                    gatewayIdentifier=GATEWAY_ID, targetId=tid,
                )
                reasons = detail.get("statusReasons", [])
                log(f"  {GATEWAY_TARGETS[sname]} FAILED: {reasons}", Colors.RED)
                return False
            else:
                all_ready = False

        if all_ready:
            log("\n  All gateway targets synced.", Colors.GREEN)
            return True

        elapsed = int(time.time() - start)
        log(f"  [{elapsed}s] Waiting...")

    log(f"  Gateway sync timed out after {timeout}s", Colors.YELLOW)
    return False
