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
    python scripts/deploy_mcp_servers.py deploy --tag v2
    python scripts/deploy_mcp_servers.py status
    python scripts/deploy_mcp_servers.py logs monitoring --minutes 10
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

# ── Configuration ────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "650251690796")
PLATFORM = "linux/arm64"  # AgentCore runtimes require arm64

# Each MCP server: deploy_dir_name -> (runtime_name, runtime_id, ecr_repo)
SERVERS = {
    "aws_infra": {
        "deploy_dir": "deploy_aws_infra",
        "runtime_name": "mcp_server",
        "runtime_id": "mcp_server-4kjU5oAHWM",
        "ecr_repo": "bedrock_agentcore-mcp_server",
        "role": "aws-infra-server",
    },
    "monitoring": {
        "deploy_dir": "deploy_monitoring",
        "runtime_name": "monitoring_server",
        "runtime_id": "monitoring_server-CI86d62MYP",
        "ecr_repo": "bedrock_agentcore-monitoring_server",
        "role": "aws-infra-server",
    },
    "sns": {
        "deploy_dir": "deploy_sns",
        "runtime_name": "sns_server",
        "runtime_id": "sns_server-2f8klN8rTF",
        "ecr_repo": "bedrock_agentcore-sns_server",
        "role": "aws-infra-server",
    },
    "teams": {
        "deploy_dir": "deploy_teams",
        "runtime_name": "teams_server",
        "runtime_id": "teams_server-hbrhm38Ef3",
        "ecr_repo": "bedrock_agentcore-teams_server",
        "role": "aws-infra-server",
    },
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ecr_uri(server: dict) -> str:
    return f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{server['ecr_repo']}"


def _role_arn(server: dict) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{server['role']}"


def _log_group(runtime_id: str) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


# ── Helpers ──────────────────────────────────────────────────────────────

class Colors:
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


def _log(msg: str, color: str = ""):
    print(f"{color}{msg}{Colors.RESET}" if color else msg)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    _log(f"  $ {' '.join(cmd)}", Colors.CYAN)
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        _log(f"  ERROR: exit code {result.returncode}", Colors.RED)
        return result
    return result


def _ac():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


# ── Pipeline Steps ───────────────────────────────────────────────────────

def ecr_login():
    _log("\n[ECR] Authenticating Docker...", Colors.BOLD)
    pw = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", REGION],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin",
         f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"],
        input=pw.stdout, text=True, check=True,
        capture_output=True,
    )
    _log("  Authenticated.", Colors.GREEN)


def build_server(name: str, server: dict, tag: str) -> bool:
    deploy_path = PROJECT_ROOT / server["deploy_dir"]
    ecr = _ecr_uri(server)
    full_tag = f"{ecr}:{tag}"

    _log(f"\n  Building {name}...", Colors.BOLD)
    result = _run([
        "docker", "build",
        "--platform", PLATFORM,
        "-t", full_tag,
        "-t", f"{ecr}:latest",
        str(deploy_path),
    ])
    if result.returncode != 0:
        _log(f"  Build FAILED for {name}", Colors.RED)
        return False
    _log(f"  Built: {full_tag}", Colors.GREEN)
    return True


def push_server(name: str, server: dict, tag: str) -> bool:
    ecr = _ecr_uri(server)
    full_tag = f"{ecr}:{tag}"

    _log(f"\n  Pushing {name}...", Colors.BOLD)
    r1 = _run(["docker", "push", full_tag])
    r2 = _run(["docker", "push", f"{ecr}:latest"])
    if r1.returncode != 0 or r2.returncode != 0:
        _log(f"  Push FAILED for {name}", Colors.RED)
        return False
    _log(f"  Pushed: {full_tag}", Colors.GREEN)
    return True


def update_runtime(name: str, server: dict, tag: str) -> bool:
    ecr = _ecr_uri(server)
    image_uri = f"{ecr}:{tag}"
    rid = server["runtime_id"]
    ac = _ac()

    _log(f"\n  Updating runtime {name} ({rid})...", Colors.BOLD)
    try:
        ac.update_agent_runtime(
            agentRuntimeId=rid,
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": image_uri}
            },
            roleArn=_role_arn(server),
            networkConfiguration={"networkMode": "PUBLIC"},
        )
        _log(f"  Update submitted for {name}", Colors.GREEN)
        return True
    except Exception as exc:
        _log(f"  Update FAILED for {name}: {exc}", Colors.RED)
        return False


def wait_for_ready(name: str, server: dict, timeout: int = 300) -> bool:
    rid = server["runtime_id"]
    ac = _ac()
    start = time.time()
    last_status = ""

    while time.time() - start < timeout:
        resp = ac.get_agent_runtime(agentRuntimeId=rid)
        status = resp["status"]

        if status != last_status:
            elapsed = int(time.time() - start)
            uri = resp.get("agentRuntimeArtifact", {}).get(
                "containerConfiguration", {}
            ).get("containerUri", "")
            _log(f"  [{elapsed}s] {name}: {status}  image={uri}")
            last_status = status

        if status == "READY":
            return True
        if status == "FAILED":
            _log(f"  {name} FAILED: {resp.get('statusReasons', 'unknown')}", Colors.RED)
            return False

        time.sleep(10)

    _log(f"  {name} timed out after {timeout}s (last: {last_status})", Colors.YELLOW)
    return False


# ── Commands ─────────────────────────────────────────────────────────────

def cmd_deploy(args):
    """Deploy all or selected MCP servers."""
    # Determine which servers to deploy
    if args.servers:
        names = [s.strip() for s in args.servers.split(",")]
        for n in names:
            if n not in SERVERS:
                _log(f"Unknown server: {n}. Available: {', '.join(SERVERS)}", Colors.RED)
                sys.exit(1)
    else:
        names = list(SERVERS.keys())

    tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
    _log(f"\n{'=' * 60}", Colors.BOLD)
    _log(f"  Deploying MCP Servers: {', '.join(names)}", Colors.BOLD)
    _log(f"  Tag: {tag}  Platform: {PLATFORM}", Colors.BOLD)
    _log(f"{'=' * 60}", Colors.BOLD)

    # Pre-flight: check docker
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        _log("ERROR: Docker is not running. Start Docker Desktop first.", Colors.RED)
        sys.exit(1)

    # Step 1: ECR login (once)
    ecr_login()

    # Step 2: Build all
    _log(f"\n{'=' * 60}", Colors.BOLD)
    _log("  PHASE: Build", Colors.BOLD)
    _log(f"{'=' * 60}", Colors.BOLD)
    build_results = {}
    for name in names:
        ok = build_server(name, SERVERS[name], tag)
        build_results[name] = ok

    failed_builds = [n for n, ok in build_results.items() if not ok]
    if failed_builds:
        _log(f"\nBuild failed for: {', '.join(failed_builds)}", Colors.RED)
        _log("Fix the errors and retry. Successfully built servers were NOT pushed.", Colors.YELLOW)
        sys.exit(1)

    # Step 3: Push all
    _log(f"\n{'=' * 60}", Colors.BOLD)
    _log("  PHASE: Push to ECR", Colors.BOLD)
    _log(f"{'=' * 60}", Colors.BOLD)
    push_results = {}
    for name in names:
        ok = push_server(name, SERVERS[name], tag)
        push_results[name] = ok

    failed_pushes = [n for n, ok in push_results.items() if not ok]
    if failed_pushes:
        _log(f"\nPush failed for: {', '.join(failed_pushes)}", Colors.RED)
        sys.exit(1)

    # Step 4: Update runtimes
    _log(f"\n{'=' * 60}", Colors.BOLD)
    _log("  PHASE: Update AgentCore Runtimes", Colors.BOLD)
    _log(f"{'=' * 60}", Colors.BOLD)
    for name in names:
        update_runtime(name, SERVERS[name], tag)

    # Step 5: Wait for all to be READY
    _log(f"\n{'=' * 60}", Colors.BOLD)
    _log("  PHASE: Waiting for READY", Colors.BOLD)
    _log(f"{'=' * 60}", Colors.BOLD)
    ready_results = {}
    for name in names:
        ok = wait_for_ready(name, SERVERS[name])
        ready_results[name] = ok

    # Summary
    _log(f"\n{'=' * 60}", Colors.BOLD)
    _log("  DEPLOYMENT SUMMARY", Colors.BOLD)
    _log(f"{'=' * 60}", Colors.BOLD)
    all_ok = True
    for name in names:
        status = "READY" if ready_results.get(name) else "FAILED"
        color = Colors.GREEN if status == "READY" else Colors.RED
        _log(f"  {name:15s} {status}", color)
        if status != "READY":
            all_ok = False

    if all_ok:
        _log(f"\n  All {len(names)} servers deployed successfully!", Colors.GREEN)
    else:
        _log(f"\n  Some servers failed. Check logs:", Colors.RED)
        _log(f"    python scripts/deploy_mcp_servers.py logs <server> --minutes 10", Colors.YELLOW)
        sys.exit(1)


def cmd_status(args):
    """Show status of all MCP server runtimes."""
    ac = _ac()
    _log(f"\n{'=' * 60}", Colors.BOLD)
    _log("  MCP Server Runtime Status", Colors.BOLD)
    _log(f"{'=' * 60}", Colors.BOLD)

    for name, server in SERVERS.items():
        try:
            resp = ac.get_agent_runtime(agentRuntimeId=server["runtime_id"])
            status = resp["status"]
            uri = resp.get("agentRuntimeArtifact", {}).get(
                "containerConfiguration", {}
            ).get("containerUri", "n/a")
            tag = uri.rsplit(":", 1)[-1] if ":" in uri else "n/a"

            color = Colors.GREEN if status == "READY" else Colors.YELLOW
            _log(f"\n  {name}", Colors.BOLD)
            _log(f"    ID:     {server['runtime_id']}")
            _log(f"    Status: {status}", color)
            _log(f"    Tag:    {tag}")
            _log(f"    Image:  {uri}")
        except Exception as exc:
            _log(f"\n  {name}", Colors.BOLD)
            _log(f"    Error: {exc}", Colors.RED)


def cmd_logs(args):
    """Tail CloudWatch logs for a specific MCP server."""
    name = args.server
    if name not in SERVERS:
        _log(f"Unknown server: {name}. Available: {', '.join(SERVERS)}", Colors.RED)
        sys.exit(1)

    server = SERVERS[name]
    logs_client = boto3.client("logs", region_name=REGION)
    log_group = _log_group(server["runtime_id"])
    minutes = args.minutes or 5
    end_time = int(time.time() * 1000)
    start_time = end_time - (minutes * 60 * 1000)

    _log(f"\nTailing {name} logs ({log_group}, last {minutes} min)\n", Colors.BOLD)

    try:
        events = logs_client.filter_log_events(
            logGroupName=log_group,
            startTime=start_time,
            endTime=end_time,
            limit=100,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        _log("Log group not found. The runtime may not have started yet.", Colors.YELLOW)
        return

    count = 0
    for evt in events.get("events", []):
        msg = evt["message"]
        try:
            data = json.loads(msg)
            body = data.get("body", "")
            sev = data.get("severityText", "")
        except (json.JSONDecodeError, TypeError):
            body = msg
            sev = ""

        if "GET /ping" in body:
            continue

        ts = time.strftime("%H:%M:%S", time.localtime(evt["timestamp"] / 1000))
        print(f"[{ts}] {sev:5s} {body[:500]}")
        count += 1

    if count == 0:
        _log("(no non-healthcheck log entries found)", Colors.YELLOW)


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MCP Servers — Deployment Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  deploy_mcp_servers.py deploy                         # deploy all 4 servers
  deploy_mcp_servers.py deploy --servers aws_infra,sns # deploy specific ones
  deploy_mcp_servers.py deploy --tag v2                # explicit tag
  deploy_mcp_servers.py status                         # check all runtimes
  deploy_mcp_servers.py logs monitoring --minutes 10   # tail server logs
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # deploy
    p_deploy = sub.add_parser("deploy", help="Build, push, and deploy MCP servers")
    p_deploy.add_argument("--servers", default=None,
                          help="Comma-separated list (default: all). Options: aws_infra,monitoring,sns,teams")
    p_deploy.add_argument("--tag", default=None, help="Image tag (default: auto timestamp)")
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
