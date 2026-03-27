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
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

# ── Configuration ────────────────────────────────────────────────────────
# All tuneable values in one place. Override via env vars if needed.

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "650251690796")
AGENT_NAME = "devops_agent"

ECR_REPO = f"bedrock_agentcore-{AGENT_NAME}"
ECR_URI = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}"
ROLE_NAME = "devops-agent-runner"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy_agent"
PLATFORM = "linux/arm64"

GATEWAY_URL = os.environ.get(
    "GATEWAY_URL",
    f"https://devopsagentgatewayv2-hvvsllrsvw"
    f".gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp",
)
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-lite-v1:0")

# Environment variables injected into the container at runtime
RUNTIME_ENV = {
    "GATEWAY_URL": GATEWAY_URL,
    "AWS_REGION": REGION,
    "MODEL_ID": MODEL_ID,
    "LOG_LEVEL": "INFO",
}


# ── Helpers ──────────────────────────────────────────────────────────────

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  ERROR: exit code {result.returncode}")
        sys.exit(result.returncode)
    return result


def _ac():
    """Return a bedrock-agentcore-control client."""
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def _find_runtime_id() -> str | None:
    """Find the runtime ID for our agent, or None."""
    try:
        for rt in _ac().list_agent_runtimes().get("agentRuntimes", []):
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
    return _ac().get_agent_runtime(agentRuntimeId=rid)


def _log_group(runtime_id: str) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


# ── Pipeline Steps ───────────────────────────────────────────────────────

def step_ecr_login():
    print("\n[1/4] ECR Login")
    pw = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", REGION],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin",
         f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"],
        input=pw.stdout, text=True, check=True,
    )
    print("  Authenticated.")


def step_build(tag: str) -> str:
    print(f"\n[2/4] Build image ({PLATFORM})")
    full_tag = f"{ECR_URI}:{tag}"
    _run([
        "docker", "build",
        "--platform", PLATFORM,
        "-t", full_tag,
        "-t", f"{ECR_URI}:latest",
        str(DEPLOY_DIR),
    ])
    return full_tag


def step_push(tag: str):
    print("\n[3/4] Push to ECR")
    _run(["docker", "push", f"{ECR_URI}:{tag}"])
    _run(["docker", "push", f"{ECR_URI}:latest"])


def step_deploy(tag: str) -> str:
    print("\n[4/4] Deploy to AgentCore")
    ac = _ac()
    image_uri = f"{ECR_URI}:{tag}"

    runtime_params = dict(
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        roleArn=ROLE_ARN,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables=RUNTIME_ENV,
    )

    existing_id = _find_runtime_id()
    if existing_id:
        print(f"  Updating runtime {existing_id} -> {tag}")
        ac.update_agent_runtime(agentRuntimeId=existing_id, **runtime_params)
        return existing_id
    else:
        print(f"  Creating new runtime: {AGENT_NAME}")
        resp = ac.create_agent_runtime(
            agentRuntimeName=AGENT_NAME,
            description="DevOps AI Agent — Strands agent with MCP Gateway tools",
            **runtime_params,
        )
        rid = resp["agentRuntimeId"]
        print(f"  Created: {rid}")
        return rid


def step_wait(runtime_id: str, timeout: int = 300) -> bool:
    print("\n  Waiting for READY...")
    ac = _ac()
    start = time.time()
    last_status = ""

    while time.time() - start < timeout:
        resp = ac.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp["status"]
        uri = resp.get("agentRuntimeArtifact", {}).get(
            "containerConfiguration", {}
        ).get("containerUri", "")

        if status != last_status:
            elapsed = int(time.time() - start)
            print(f"  [{elapsed}s] {status}  image={uri}")
            last_status = status

        if status == "READY":
            arn = resp.get("agentRuntimeArn", "")
            print(f"\n  Runtime READY")
            print(f"  ID:  {runtime_id}")
            print(f"  ARN: {arn}")
            print(f"  Image: {uri}")
            return True
        if status == "FAILED":
            print(f"\n  FAILED: {resp.get('statusReasons', 'unknown')}")
            return False

        time.sleep(10)

    print(f"\n  Timeout after {timeout}s (last status: {last_status})")
    return False


# ── Commands ─────────────────────────────────────────────────────────────

def cmd_deploy(args):
    """Full deployment pipeline: build -> push -> deploy -> wait."""
    tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
    print(f"Deploying {AGENT_NAME} with tag: {tag}")

    # Pre-flight: check docker is running
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: Docker is not running. Start Docker Desktop first.")
        sys.exit(1)

    step_ecr_login()
    step_build(tag)
    step_push(tag)
    runtime_id = step_deploy(tag)
    ok = step_wait(runtime_id)

    if ok:
        print(f"\n{'=' * 60}")
        print(f"  Deployment complete!  tag={tag}")
        print(f"  Test with:")
        print(f'    python scripts/deploy_agent.py invoke "List EC2 instances"')
        print(f"  Tail logs:")
        print(f"    python scripts/deploy_agent.py logs")
        print(f"{'=' * 60}")
    else:
        print("\nDeployment failed. Check logs:")
        print(f"  python scripts/deploy_agent.py logs")
        sys.exit(1)


def cmd_status(args):
    """Show current runtime status."""
    info = _get_runtime_info()
    if not info:
        print(f"No runtime found for '{AGENT_NAME}'")
        return

    uri = info.get("agentRuntimeArtifact", {}).get(
        "containerConfiguration", {}
    ).get("containerUri", "n/a")
    tag = uri.rsplit(":", 1)[-1] if ":" in uri else "n/a"

    print(f"Agent:   {AGENT_NAME}")
    print(f"ID:      {info.get('agentRuntimeId')}")
    print(f"Status:  {info['status']}")
    print(f"Tag:     {tag}")
    print(f"Image:   {uri}")
    print(f"ARN:     {info.get('agentRuntimeArn', 'n/a')}")


def cmd_logs(args):
    """Tail recent CloudWatch logs, skipping health-check noise."""
    rid = _find_runtime_id()
    if not rid:
        print(f"No runtime found for '{AGENT_NAME}'")
        return

    logs_client = boto3.client("logs", region_name=REGION)
    log_group = _log_group(rid)
    minutes = args.minutes or 5
    end_time = int(time.time() * 1000)
    start_time = end_time - (minutes * 60 * 1000)

    print(f"Tailing {log_group} (last {minutes} min)\n")
    try:
        events = logs_client.filter_log_events(
            logGroupName=log_group,
            startTime=start_time,
            endTime=end_time,
            limit=100,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        print("Log group not found yet. The runtime may not have started.")
        return

    count = 0
    for evt in events.get("events", []):
        msg = evt["message"]
        # Parse OTEL JSON envelope
        try:
            data = json.loads(msg)
            body = data.get("body", "")
            sev = data.get("severityText", "")
        except (json.JSONDecodeError, TypeError):
            body = msg
            sev = ""

        # Skip health check noise
        if "GET /ping" in body:
            continue

        ts = time.strftime(
            "%H:%M:%S", time.localtime(evt["timestamp"] / 1000)
        )
        print(f"[{ts}] {sev:5s} {body[:500]}")
        count += 1

    if count == 0:
        print("(no non-healthcheck log entries found)")


def cmd_invoke(args):
    """Send a prompt to the deployed agent."""
    rid = _find_runtime_id()
    if not rid:
        print(f"No runtime found for '{AGENT_NAME}'")
        sys.exit(1)

    prompt = args.prompt
    if not prompt:
        print("ERROR: provide a prompt string")
        sys.exit(1)

    ac = boto3.client("bedrock-agentcore", region_name=REGION)
    print(f"Invoking {AGENT_NAME} ({rid})...")
    print(f"Prompt: {prompt[:200]}\n")

    try:
        resp = ac.invoke_agent_runtime(
            agentRuntimeId=rid,
            payload=json.dumps({"prompt": prompt}),
        )
        body = resp.get("payload", b"").read().decode("utf-8")
        try:
            data = json.loads(body)
            print(data.get("response", body))
        except (json.JSONDecodeError, TypeError):
            print(body)
    except Exception as exc:
        print(f"Invocation error: {exc}")
        print("\nCheck logs: python scripts/deploy_agent.py logs")
        sys.exit(1)


def cmd_local(args):
    """Build and run the container locally for testing."""
    tag = args.tag or "local"
    step_build(tag)

    print(f"\nRunning locally ({PLATFORM})...")
    _run([
        "docker", "run", "--rm", "-it",
        "--platform", PLATFORM,
        "-p", "8080:8080",
        "-e", f"GATEWAY_URL={GATEWAY_URL}",
        "-e", f"AWS_REGION={REGION}",
        "-e", f"MODEL_ID={MODEL_ID}",
        "-v", f"{Path.home()}/.aws:/home/bedrock_agentcore/.aws:ro",
        f"{ECR_URI}:{tag}",
    ])


def cmd_setup(args):
    """One-time infrastructure setup: ECR repo + IAM role."""
    iam_client = boto3.client("iam", region_name=REGION)
    ecr_client = boto3.client("ecr", region_name=REGION)

    # ECR repository
    try:
        ecr_client.describe_repositories(repositoryNames=[ECR_REPO])
        print(f"ECR repo '{ECR_REPO}' exists")
    except ecr_client.exceptions.RepositoryNotFoundException:
        ecr_client.create_repository(repositoryName=ECR_REPO)
        print(f"Created ECR repo '{ECR_REPO}'")

    # IAM role
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    try:
        iam_client.get_role(RoleName=ROLE_NAME)
        print(f"IAM role '{ROLE_NAME}' exists")
    except iam_client.exceptions.NoSuchEntityException:
        iam_client.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for DevOps Agent on AgentCore",
        )
        print(f"Created IAM role '{ROLE_NAME}'")

    # Attach permissions policy from file
    policy_file = DEPLOY_DIR / "permissions-policy.json"
    if policy_file.exists():
        iam_client.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName="devops-agent-permissions",
            PolicyDocument=policy_file.read_text(),
        )
        print(f"Attached permissions policy from {policy_file.name}")

    print("\nSetup complete. Run: python scripts/deploy_agent.py deploy")


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
    p_deploy.add_argument("--tag", default=None, help="Image tag (default: auto timestamp)")
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
    p_local.set_defaults(func=cmd_local)

    # setup
    p_setup = sub.add_parser("setup", help="One-time: create ECR repo + IAM role")
    p_setup.set_defaults(func=cmd_setup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
