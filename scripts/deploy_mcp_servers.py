"""
MCP Servers — Deployment Pipeline for Bedrock AgentCore.

Deploys all four MCP servers (aws_infra, monitoring, sns, teams) to
AgentCore in a single command.  Each server is built as a Docker
container, pushed to ECR, and the AgentCore runtime is updated.

Subcommands:
  deploy      Build, push, and deploy all (or specific) MCP servers
  status      Show current status of all MCP server runtimes
  logs        Tail CloudWatch logs for a specific server

Usage:
    python scripts/deploy_mcp_servers.py deploy
    python scripts/deploy_mcp_servers.py deploy --servers aws_infra,sns
    python scripts/deploy_mcp_servers.py deploy --tag v5-abc1234
    python scripts/deploy_mcp_servers.py deploy --dry-run
    python scripts/deploy_mcp_servers.py deploy --skip-gateway
    python scripts/deploy_mcp_servers.py status
    python scripts/deploy_mcp_servers.py logs monitoring --minutes 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure scripts/ is on the path so `lib` is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import (
    MCP_SERVERS,
    GATEWAY_ID,
    PLATFORM,
    ecr_uri,
)
from lib.console import log, Colors
from lib.aws import ac_client, ecr_login, tail_logs
from lib.docker import build_image, push_image
from lib.runtime import update_runtime, wait_for_ready, validate_runtime
from lib.gateway import sync_gateway_targets
from lib.version import git_sha, git_branch, next_version, preflight_checks

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Commands ─────────────────────────────────────────────────────────────

def cmd_deploy(args):
    """Deploy all or selected MCP servers."""
    # Determine which servers to deploy
    if args.servers:
        names = [s.strip() for s in args.servers.split(",")]
        for n in names:
            if n not in MCP_SERVERS:
                log(f"Unknown server: {n}. Available: {', '.join(MCP_SERVERS)}", Colors.RED)
                sys.exit(1)
    else:
        names = list(MCP_SERVERS.keys())

    # Auto-version from ECR
    if args.tag:
        tag = args.tag
    else:
        ref_repo = MCP_SERVERS[names[0]]["ecr_repo"]
        tag = next_version(ref_repo)

    log(f"\n{'=' * 60}", Colors.BOLD)
    log(f"  Deploying MCP Servers: {', '.join(names)}", Colors.BOLD)
    log(f"  Tag:      {tag}", Colors.BOLD)
    log(f"  Platform: {PLATFORM}", Colors.BOLD)
    log(f"  Commit:   {git_sha()} ({git_branch()})", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)

    # Dry-run: show what would happen, then exit
    if args.dry_run:
        log("\n  [DRY RUN] No changes will be made.\n", Colors.YELLOW)
        ac = ac_client()
        for name in names:
            server = MCP_SERVERS[name]
            rid = server["runtime_id"]
            uri = ecr_uri(server["ecr_repo"])
            image = f"{uri}:{tag}"

            log(f"  {name}:", Colors.BOLD)
            log(f"    Image:   {image}")

            try:
                current = ac.get_agent_runtime(agentRuntimeId=rid)
                cur_uri = (
                    current.get("agentRuntimeArtifact", {})
                    .get("containerConfiguration", {})
                    .get("containerUri", "n/a")
                )
                proto = current.get("protocolConfiguration", {})
                auth = current.get("authorizerConfiguration", {})
                log(f"    Current: {cur_uri}")
                log(f"    Protocol: {proto}")
                log(f"    Auth:     {'present' if auth else 'MISSING'}")
            except Exception as exc:
                log(f"    Could not fetch current config: {exc}", Colors.RED)
            log("")
        return

    preflight_checks()
    ecr_login()

    # Build
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  PHASE: Build", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)
    build_results = {}
    for name in names:
        server = MCP_SERVERS[name]
        build_dir = PROJECT_ROOT / server["deploy_dir"]
        log(f"\n  [{name}]", Colors.BOLD)
        ok = build_image(server["ecr_repo"], tag, build_dir)
        build_results[name] = ok

    failed = [n for n, ok in build_results.items() if not ok]
    if failed:
        log(f"\nBuild failed for: {', '.join(failed)}", Colors.RED)
        sys.exit(1)

    # Update IAM inline policies for deployed servers
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  PHASE: Update IAM Role Policies", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)
    import boto3
    iam = boto3.client("iam", region_name="ap-southeast-2")
    for name in names:
        server = MCP_SERVERS[name]
        policy_file = PROJECT_ROOT / server["deploy_dir"] / "permissions-policy.json"
        if policy_file.exists():
            log(f"\n  [{name}]", Colors.BOLD)
            try:
                iam.put_role_policy(
                    RoleName=server["role"],
                    PolicyName=f"{name}-server-permissions",
                    PolicyDocument=policy_file.read_text(),
                )
                log(f"  ✓ Applied {policy_file.name} → {server['role']}", Colors.GREEN)
            except Exception as exc:
                log(f"  ✗ IAM policy update failed: {exc}", Colors.RED)
        else:
            log(f"\n  [{name}] No permissions-policy.json found — skipping", Colors.YELLOW)

    # Push
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  PHASE: Push to ECR", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)
    push_results = {}
    for name in names:
        server = MCP_SERVERS[name]
        log(f"\n  [{name}]", Colors.BOLD)
        ok = push_image(server["ecr_repo"], tag)
        push_results[name] = ok

    failed = [n for n, ok in push_results.items() if not ok]
    if failed:
        log(f"\nPush failed for: {', '.join(failed)}", Colors.RED)
        sys.exit(1)

    # Update runtimes (read-merge-write)
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  PHASE: Update AgentCore Runtimes", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)
    for name in names:
        server = MCP_SERVERS[name]
        log(f"\n  [{name}]", Colors.BOLD)
        update_runtime(
            server["runtime_id"], server["ecr_repo"], tag,
            server["role"], protocol="MCP",
        )

    # Wait for READY
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  PHASE: Waiting for READY", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)
    ready_results = {}
    for name in names:
        server = MCP_SERVERS[name]
        log(f"\n  [{name}]", Colors.BOLD)
        ok = wait_for_ready(server["runtime_id"])
        ready_results[name] = ok

    # Post-deploy validation
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  PHASE: Post-Deploy Validation", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)
    for name in names:
        if ready_results.get(name):
            server = MCP_SERVERS[name]
            log(f"\n  [{name}]", Colors.BOLD)
            validate_runtime(server["runtime_id"], expect_mcp=True)

    # Gateway target sync
    all_ready = all(ready_results.get(n) for n in names)
    if all_ready and not args.skip_gateway:
        deploy_configs = {n: MCP_SERVERS[n] for n in names}
        gw_ok = sync_gateway_targets(deploy_configs)
    elif args.skip_gateway:
        log("\n  Gateway sync skipped (--skip-gateway)", Colors.YELLOW)
        gw_ok = True
    else:
        gw_ok = False

    # Summary
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  DEPLOYMENT SUMMARY", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)
    log(f"  Tag:    {tag}", Colors.BOLD)
    log(f"  Commit: {git_sha()} ({git_branch()})", Colors.BOLD)
    log("")

    all_ok = True
    for name in names:
        status = "READY" if ready_results.get(name) else "FAILED"
        color = Colors.GREEN if status == "READY" else Colors.RED
        log(f"  {name:15s} {status}", color)
        if status != "READY":
            all_ok = False

    if not args.skip_gateway:
        gw_status = "SYNCED" if gw_ok else "FAILED"
        gw_color = Colors.GREEN if gw_ok else Colors.RED
        log(f"  {'gateway':15s} {gw_status}", gw_color)

    if all_ok and gw_ok:
        log(f"\n  All {len(names)} servers deployed successfully!", Colors.GREEN)
    else:
        log(f"\n  Some steps failed. Check logs:", Colors.RED)
        log(f"    python scripts/deploy_mcp_servers.py logs <server> --minutes 10", Colors.YELLOW)
        sys.exit(1)


def cmd_status(args):
    """Show status of all MCP server runtimes."""
    ac = ac_client()
    log(f"\n{'=' * 60}", Colors.BOLD)
    log("  MCP Server Runtime Status", Colors.BOLD)
    log(f"{'=' * 60}", Colors.BOLD)

    for name, server in MCP_SERVERS.items():
        try:
            resp = ac.get_agent_runtime(agentRuntimeId=server["runtime_id"])
            status = resp["status"]
            uri = resp.get("agentRuntimeArtifact", {}).get(
                "containerConfiguration", {}
            ).get("containerUri", "n/a")
            tag = uri.rsplit(":", 1)[-1] if ":" in uri else "n/a"
            proto = resp.get("protocolConfiguration", {}).get("serverProtocol", "n/a")
            auth = "present" if resp.get("authorizerConfiguration") else "MISSING"

            color = Colors.GREEN if status == "READY" else Colors.YELLOW
            log(f"\n  {name}", Colors.BOLD)
            log(f"    ID:       {server['runtime_id']}")
            log(f"    Status:   {status}", color)
            log(f"    Tag:      {tag}")
            log(f"    Image:    {uri}")
            log(f"    Protocol: {proto}")
            log(f"    Auth:     {auth}")
        except Exception as exc:
            log(f"\n  {name}", Colors.BOLD)
            log(f"    Error: {exc}", Colors.RED)

    # Gateway targets
    log(f"\n  {'─' * 40}", Colors.BOLD)
    log("  Gateway Targets", Colors.BOLD)
    try:
        resp = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        for t in resp.get("items", []):
            status = t["status"]
            color = Colors.GREEN if status == "READY" else Colors.RED
            log(f"    {t['name']:25s} {status} ({t['targetId']})", color)
    except Exception as exc:
        log(f"    Error: {exc}", Colors.RED)


def cmd_logs(args):
    """Tail CloudWatch logs for a specific MCP server."""
    name = args.server
    if name not in MCP_SERVERS:
        log(f"Unknown server: {name}. Available: {', '.join(MCP_SERVERS)}", Colors.RED)
        sys.exit(1)

    server = MCP_SERVERS[name]
    tail_logs(server["runtime_id"], minutes=args.minutes or 5)


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MCP Servers — Deployment Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  deploy_mcp_servers.py deploy                         # deploy all, auto-version
  deploy_mcp_servers.py deploy --servers aws_infra,sns # deploy specific ones
  deploy_mcp_servers.py deploy --tag v5-abc1234        # explicit tag
  deploy_mcp_servers.py deploy --dry-run               # preview changes
  deploy_mcp_servers.py deploy --skip-gateway          # skip target sync
  deploy_mcp_servers.py status                         # check all runtimes
  deploy_mcp_servers.py logs monitoring --minutes 10   # tail server logs
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # deploy
    p_deploy = sub.add_parser("deploy", help="Build, push, and deploy MCP servers")
    p_deploy.add_argument("--servers", default=None,
                          help="Comma-separated list (default: all). Options: aws_infra,monitoring,sns,teams")
    p_deploy.add_argument("--tag", default=None,
                          help="Image tag (default: auto-versioned v{N}-{sha})")
    p_deploy.add_argument("--dry-run", action="store_true",
                          help="Preview what would be deployed without making changes")
    p_deploy.add_argument("--skip-gateway", action="store_true",
                          help="Skip gateway target sync after deployment")
    p_deploy.set_defaults(func=cmd_deploy)

    # status
    p_status = sub.add_parser("status", help="Show status of all MCP server runtimes")
    p_status.set_defaults(func=cmd_status)

    # logs
    p_logs = sub.add_parser("logs", help="Tail CloudWatch logs for a server")
    p_logs.add_argument("server", help="Server name: aws_infra, monitoring, sns, teams")
    p_logs.add_argument("--minutes", type=int, default=5, help="How far back (default: 5)")
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
