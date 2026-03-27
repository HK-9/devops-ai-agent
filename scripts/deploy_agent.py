"""
Deploy the DevOps Agent to Bedrock AgentCore via container deployment.

Pipeline steps:
  1. Authenticate Docker to ECR
  2. Build the container image (linux/amd64)
  3. Push to ECR
  4. Create or update the AgentCore runtime
  5. Wait for READY status

Usage:
    python scripts/deploy_agent.py
    python scripts/deploy_agent.py --local   # build + run locally only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

import boto3

# ── Configuration ────────────────────────────────────────────────────────

REGION = "ap-southeast-2"
ACCOUNT = "650251690796"
AGENT_NAME = "devops_agent"
ECR_REPO = f"bedrock_agentcore-{AGENT_NAME}"
ECR_URI = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/devops-agent-runner"
DEPLOY_DIR = "deploy_agent"
GATEWAY_URL = (
    "https://devopsagentgatewayv2-hvvsllrsvw"
    ".gateway.bedrock-agentcore.{}.amazonaws.com/mcp".format(REGION)
)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, printing it first."""
    print(f"\n> {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"ERROR: command exited with code {result.returncode}")
        sys.exit(result.returncode)
    return result


def ecr_login():
    """Authenticate Docker to ECR."""
    print("\n=== Step 1: ECR Login ===")
    pw = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", REGION],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin",
         f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"],
        input=pw.stdout, text=True, check=True,
    )
    print("ECR login successful.")


def build_image(tag: str) -> str:
    """Build the Docker image for linux/amd64."""
    print("\n=== Step 2: Build Container Image ===")
    full_tag = f"{ECR_URI}:{tag}"
    run([
        "docker", "build",
        "--platform", "linux/amd64",
        "-t", full_tag,
        "-t", f"{ECR_URI}:latest",
        DEPLOY_DIR,
    ])
    print(f"Built: {full_tag}")
    return full_tag


def push_image(tag: str):
    """Push the image to ECR."""
    print("\n=== Step 3: Push to ECR ===")
    full_tag = f"{ECR_URI}:{tag}"
    run(["docker", "push", full_tag])
    run(["docker", "push", f"{ECR_URI}:latest"])
    print(f"Pushed: {full_tag}")


def deploy_runtime(tag: str):
    """Create or update the AgentCore runtime."""
    print("\n=== Step 4: Deploy to AgentCore Runtime ===")
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    image_uri = f"{ECR_URI}:{tag}"

    # Check if runtime already exists
    existing_id = None
    try:
        runtimes = ac.list_agent_runtimes()
        for rt in runtimes.get("agentRuntimeSummaries", []):
            if rt.get("agentRuntimeName") == AGENT_NAME:
                existing_id = rt["agentRuntimeId"]
                break
    except Exception:
        pass

    runtime_params = dict(
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        roleArn=ROLE_ARN,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables={
            "GATEWAY_URL": GATEWAY_URL,
            "AWS_REGION": REGION,
            "MODEL_ID": "amazon.nova-lite-v1:0",
            "LOG_LEVEL": "INFO",
        },
    )

    if existing_id:
        print(f"Updating existing runtime: {existing_id}")
        ac.update_agent_runtime(agentRuntimeId=existing_id, **runtime_params)
        runtime_id = existing_id
    else:
        print(f"Creating new runtime: {AGENT_NAME}")
        resp = ac.create_agent_runtime(
            agentRuntimeName=AGENT_NAME,
            description="DevOps AI Agent - Strands agent with MCP Gateway tools",
            **runtime_params,
        )
        runtime_id = resp["agentRuntimeId"]
        print(f"Created runtime: {runtime_id}")

    return runtime_id


def wait_for_ready(runtime_id: str, timeout: int = 300):
    """Poll runtime status until READY or timeout."""
    print("\n=== Step 5: Waiting for Runtime READY ===")
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    start = time.time()

    while time.time() - start < timeout:
        resp = ac.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp["status"]
        print(f"  Status: {status} ({int(time.time() - start)}s)")

        if status == "READY":
            print(f"\nRuntime {runtime_id} is READY!")
            arn = resp.get("agentRuntimeArn", "")
            print(f"  ARN: {arn}")
            return True
        elif status == "FAILED":
            reason = resp.get("statusReasons", ["unknown"])
            print(f"\nRuntime FAILED: {reason}")
            return False

        time.sleep(10)

    print(f"\nTimeout after {timeout}s — status was: {status}")
    return False


def run_local(tag: str):
    """Build and run the container locally for testing."""
    print("\n=== Running Locally ===")
    full_tag = f"{ECR_URI}:{tag}"
    run([
        "docker", "run", "--rm", "-it",
        "--platform", "linux/arm64",
        "-e", f"GATEWAY_URL={GATEWAY_URL}",
        "-e", f"AWS_REGION={REGION}",
        "-e", "MODEL_ID=amazon.nova-lite-v1:0",
        "-v", f"{__import__('os').path.expanduser('~')}/.aws:/home/bedrock_agentcore/.aws:ro",
        full_tag,
    ])


def main():
    parser = argparse.ArgumentParser(description="Deploy DevOps Agent to AgentCore")
    parser.add_argument("--local", action="store_true", help="Build and run locally only")
    parser.add_argument("--tag", default=None, help="Image tag (default: timestamp)")
    args = parser.parse_args()

    if not args.tag:
        args.tag = time.strftime("%Y%m%d-%H%M%S")

    if args.local:
        build_image(args.tag)
        run_local(args.tag)
    else:
        ecr_login()
        build_image(args.tag)
        push_image(args.tag)
        runtime_id = deploy_runtime(args.tag)
        wait_for_ready(runtime_id)


if __name__ == "__main__":
    main()
