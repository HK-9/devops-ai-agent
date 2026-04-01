"""
DevOps Agent — Deployment Pipeline for Bedrock AgentCore.

Single-command deployment that handles the full lifecycle:
  deploy   — Build, push, and deploy (or update) the agent runtime
  status   — Show current runtime status and image tag
  logs     — Tail recent CloudWatch logs (skip health checks)
  invoke   — Send a prompt to the deployed agent
  local    — Build and run the container locally for testing
  setup    — One-time: create ECR repo + IAM role if missing

Usage:
    python scripts/deploy_agent.py deploy              # auto-tag
    python scripts/deploy_agent.py deploy --tag v5     # explicit tag
    python scripts/deploy_agent.py status
    python scripts/deploy_agent.py logs
    python scripts/deploy_agent.py logs --minutes 10
    python scripts/deploy_agent.py invoke "List all EC2 instances"
    python scripts/deploy_agent.py local
    python scripts/deploy_agent.py setup
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

# Ensure scripts/ is on the path so `lib` is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.aws import ac_client, ecr_login, tail_logs
from lib.config import (
    ACCOUNT,
    AGENT_ECR_REPO,
    AGENT_NAME,
    AGENT_ROLE,
    GATEWAY_URL,
    MODEL_ID,
    PLATFORM,
    REGION,
    ecr_uri,
    role_arn,
)
from lib.console import Colors, log, run
from lib.docker import build_image, push_image
from lib.runtime import update_runtime, wait_for_ready
from lib.version import git_branch, git_sha, next_version, preflight_checks

ECR_URI = ecr_uri(AGENT_ECR_REPO)
ROLE_ARN = role_arn(AGENT_ROLE)
DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deployments" / "agent"

# Environment variables injected into the container at runtime
RUNTIME_ENV = {
    "GATEWAY_URL": GATEWAY_URL,
    "AWS_REGION": REGION,
    "MODEL_ID": MODEL_ID,
    "LOG_LEVEL": "INFO",
}


# ── Agent-specific helpers ───────────────────────────────────────────────


def _find_runtime_id() -> str | None:
    """Find the runtime ID for our agent, or None."""
    try:
        for rt in ac_client().list_agent_runtimes().get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == AGENT_NAME:
                return rt["agentRuntimeId"]
    except Exception:
        pass
    return None


def _get_runtime_info() -> dict | None:
    """Return the full runtime description, or None if not found."""
    rid = _find_runtime_id()
    if not rid:
        return None
    return ac_client().get_agent_runtime(agentRuntimeId=rid)


# ── Agent deploy step (create or update) ─────────────────────────────────


def _step_deploy(tag: str) -> str:
    """Create or update the agent runtime. Returns the runtime ID."""
    ac = ac_client()
    image_uri = f"{ECR_URI}:{tag}"
    env = {**RUNTIME_ENV, "DEPLOY_VERSION": tag}

    existing_id = _find_runtime_id()
    if existing_id:
        log(f"\n[4/4] Updating runtime {existing_id} -> {tag}")
        # Use read-merge-write via the shared runtime module
        update_runtime(
            existing_id,
            AGENT_ECR_REPO,
            tag,
            AGENT_ROLE,
            protocol=None,  # Agent is HTTP, not MCP
        )
        return existing_id
    else:
        log(f"\n[4/4] Creating new runtime: {AGENT_NAME}")
        resp = ac.create_agent_runtime(
            agentRuntimeName=AGENT_NAME,
            description="DevOps AI Agent — Strands agent with MCP Gateway tools",
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
            roleArn=ROLE_ARN,
            networkConfiguration={"networkMode": "PUBLIC"},
            environmentVariables=env,
        )
        rid = resp["agentRuntimeId"]
        log(f"  Created: {rid}", Colors.GREEN)
        return rid


# ── Commands ─────────────────────────────────────────────────────────────


def cmd_deploy(args):
    """Full deployment pipeline: build -> push -> deploy -> wait."""
    tag = args.tag or next_version(AGENT_ECR_REPO)
    log(f"Deploying {AGENT_NAME} with tag: {tag}")
    log(f"  Commit: {git_sha()} ({git_branch()})")

    if args.dry_run:
        log(f"\n  [DRY RUN] No changes will be made.", Colors.YELLOW)
        log(f"  Image: {ECR_URI}:{tag}")
        info = _get_runtime_info()
        if info:
            cur_uri = info.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri", "n/a")
            proto = info.get("protocolConfiguration", {})
            auth = info.get("authorizerConfiguration", {})
            log(f"  Current: {cur_uri}")
            log(f"  Protocol: {proto}")
            log(f"  Auth:     {'present' if auth else 'MISSING'}")
        return

    preflight_checks()
    ecr_login()

    log(f"\n[2/4] Build image ({PLATFORM})")
    ok = build_image(AGENT_ECR_REPO, tag, DEPLOY_DIR, no_cache=args.no_cache)
    if not ok:
        log("Build failed.", Colors.RED)
        sys.exit(1)

    log(f"\n[3/4] Push to ECR")
    ok = push_image(AGENT_ECR_REPO, tag)
    if not ok:
        log("Push failed.", Colors.RED)
        sys.exit(1)

    runtime_id = _step_deploy(tag)
    log(f"\n  Waiting for READY...")
    ok = wait_for_ready(runtime_id)

    if ok:
        info = ac_client().get_agent_runtime(agentRuntimeId=runtime_id)
        log(f"\n{'=' * 60}")
        log(f"  Deployment complete!  tag={tag}")
        log(f"  Runtime ID: {runtime_id}")
        log(f"  ARN: {info.get('agentRuntimeArn', 'n/a')}")
        log(f"  Test with:")
        log(f'    python scripts/deploy_agent.py invoke "List EC2 instances"')
        log(f"  Tail logs:")
        log(f"    python scripts/deploy_agent.py logs")
        log(f"{'=' * 60}")
    else:
        log("\nDeployment failed. Check logs:", Colors.RED)
        log("  python scripts/deploy_agent.py logs")
        sys.exit(1)


def cmd_status(args):
    """Show current runtime status."""
    info = _get_runtime_info()
    if not info:
        log(f"No runtime found for '{AGENT_NAME}'")
        return

    uri = info.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri", "n/a")
    tag = uri.rsplit(":", 1)[-1] if ":" in uri else "n/a"

    log(f"Agent:   {AGENT_NAME}")
    log(f"ID:      {info.get('agentRuntimeId')}")
    log(f"Status:  {info['status']}")
    log(f"Tag:     {tag}")
    log(f"Image:   {uri}")
    log(f"ARN:     {info.get('agentRuntimeArn', 'n/a')}")


def cmd_logs(args):
    """Tail recent CloudWatch logs, skipping health-check noise."""
    rid = _find_runtime_id()
    if not rid:
        log(f"No runtime found for '{AGENT_NAME}'")
        return
    tail_logs(rid, minutes=args.minutes or 5)


def cmd_invoke(args):
    """Send a prompt to the deployed agent."""
    info = _get_runtime_info()
    if not info:
        log(f"No runtime found for '{AGENT_NAME}'")
        sys.exit(1)

    arn = info.get("agentRuntimeArn")
    rid = info.get("agentRuntimeId")
    prompt = args.prompt
    if not prompt:
        log("ERROR: provide a prompt string", Colors.RED)
        sys.exit(1)

    ac = boto3.client("bedrock-agentcore", region_name=REGION)
    log(f"Invoking {AGENT_NAME} ({rid})...")
    log(f"Prompt: {prompt[:200]}\n")

    try:
        resp = ac.invoke_agent_runtime(
            agentRuntimeArn=arn,
            payload=json.dumps({"prompt": prompt}),
        )
        raw = resp.get("response") or resp.get("payload", b"")
        if hasattr(raw, "read"):
            body = raw.read().decode("utf-8")
        elif isinstance(raw, bytes):
            body = raw.decode("utf-8")
        else:
            body = str(raw)
        try:
            data = json.loads(body)
            print(data.get("response", body))
        except (json.JSONDecodeError, TypeError):
            print(body or "(empty response)")
    except Exception as exc:
        log(f"Invocation error: {exc}", Colors.RED)
        log("\nCheck logs: python scripts/deploy_agent.py logs")
        sys.exit(1)


def cmd_local(args):
    """Build and run the container locally for testing."""
    tag = args.tag or "local"
    ok = build_image(AGENT_ECR_REPO, tag, DEPLOY_DIR, no_cache=getattr(args, "no_cache", False))
    if not ok:
        log("Build failed.", Colors.RED)
        sys.exit(1)

    log(f"\nRunning locally ({PLATFORM})...")
    run(
        [
            "docker",
            "run",
            "--rm",
            "-it",
            "--platform",
            PLATFORM,
            "-p",
            "8080:8080",
            "-e",
            f"GATEWAY_URL={GATEWAY_URL}",
            "-e",
            f"AWS_REGION={REGION}",
            "-e",
            f"MODEL_ID={MODEL_ID}",
            "-v",
            f"{Path.home()}/.aws:/home/bedrock_agentcore/.aws:ro",
            f"{ECR_URI}:{tag}",
        ],
        check=True,
    )


def cmd_setup(args):
    """One-time infrastructure setup: ECR repo + IAM role."""
    iam_client = boto3.client("iam", region_name=REGION)
    ecr_client = boto3.client("ecr", region_name=REGION)

    # ECR repository
    try:
        ecr_client.describe_repositories(repositoryNames=[AGENT_ECR_REPO])
        log(f"ECR repo '{AGENT_ECR_REPO}' exists")
    except ecr_client.exceptions.RepositoryNotFoundException:
        ecr_client.create_repository(repositoryName=AGENT_ECR_REPO)
        log(f"Created ECR repo '{AGENT_ECR_REPO}'")

    # IAM role
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        iam_client.get_role(RoleName=AGENT_ROLE)
        log(f"IAM role '{AGENT_ROLE}' exists")
    except iam_client.exceptions.NoSuchEntityException:
        iam_client.create_role(
            RoleName=AGENT_ROLE,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for DevOps Agent on AgentCore",
        )
        log(f"Created IAM role '{AGENT_ROLE}'")

    # Attach permissions policy from file
    policy_file = DEPLOY_DIR / "permissions-policy.json"
    if policy_file.exists():
        iam_client.put_role_policy(
            RoleName=AGENT_ROLE,
            PolicyName="devops-agent-permissions",
            PolicyDocument=policy_file.read_text(),
        )
        log(f"Attached permissions policy from {policy_file.name}")

    log("\nSetup complete. Run: python scripts/deploy_agent.py deploy")


# ── CLI entrypoint ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="DevOps Agent — Deployment Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  deploy_agent.py deploy              # build + push + deploy (auto-tag)
  deploy_agent.py deploy --tag v5     # explicit tag
  deploy_agent.py status              # check runtime state
  deploy_agent.py logs                # tail recent logs
  deploy_agent.py logs --minutes 10   # last 10 min of logs
  deploy_agent.py invoke "List EC2s"  # test the deployed agent
  deploy_agent.py local               # build + run locally
  deploy_agent.py setup               # one-time infra setup
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # deploy
    p_deploy = sub.add_parser("deploy", help="Build, push, and deploy the agent")
    p_deploy.add_argument("--tag", default=None, help="Image tag (default: auto-versioned v{N}-{sha})")
    p_deploy.add_argument("--no-cache", action="store_true", help="Force full Docker rebuild (no layer cache)")
    p_deploy.add_argument(
        "--dry-run", action="store_true", help="Preview what would be deployed without making changes"
    )
    p_deploy.set_defaults(func=cmd_deploy)

    # status
    p_status = sub.add_parser("status", help="Show current runtime status")
    p_status.set_defaults(func=cmd_status)

    # logs
    p_logs = sub.add_parser("logs", help="Tail recent CloudWatch logs")
    p_logs.add_argument("--minutes", type=int, default=5, help="How far back (default: 5)")
    p_logs.set_defaults(func=cmd_logs)

    # invoke
    p_invoke = sub.add_parser("invoke", help="Send a prompt to the deployed agent")
    p_invoke.add_argument("prompt", help="The prompt to send")
    p_invoke.set_defaults(func=cmd_invoke)

    # local
    p_local = sub.add_parser("local", help="Build and run the container locally")
    p_local.add_argument("--tag", default=None, help="Image tag (default: 'local')")
    p_local.add_argument("--no-cache", action="store_true", help="Force full Docker rebuild")
    p_local.set_defaults(func=cmd_local)

    # setup
    p_setup = sub.add_parser("setup", help="One-time: create ECR repo + IAM role")
    p_setup.set_defaults(func=cmd_setup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
