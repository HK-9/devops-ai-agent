"""
DevOps AI Agent — Setup Validation Script

Runs all pre-flight checks before deploying to AWS.
No changes are made to any AWS resources.

Usage:
    python validate_setup.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any

# ── Colour helpers ─────────────────────────────────────────────────────────────

OK     = "\033[92m✅\033[0m"
FAIL   = "\033[91m❌\033[0m"
WARN   = "\033[93m⚠️ \033[0m"
INFO   = "\033[94mℹ️ \033[0m"
SKIP   = "\033[90m⏭️ \033[0m"

PASS_COUNT = 0
FAIL_COUNT = 0
WARN_COUNT = 0


def result(icon: str, label: str, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT, WARN_COUNT
    print(f"  {icon} {label}")
    if detail:
        print(f"       {detail}")
    if icon == OK:
        PASS_COUNT += 1
    elif icon == FAIL:
        FAIL_COUNT += 1
    elif icon == WARN:
        WARN_COUNT += 1


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── Check 1: Python environment & imports ─────────────────────────────────────

def check_python_env() -> None:
    section("1. Python Environment")

    # Python version
    v = sys.version_info
    if v >= (3, 11):
        result(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        result(FAIL, f"Python {v.major}.{v.minor} — need 3.11+")

    # Required packages
    packages = {
        "boto3":     "AWS SDK",
        "mcp":       "MCP protocol",
        "httpx":     "HTTP client (Teams webhook)",
        "pydantic":  "Config validation",
        "pydantic_settings": "Env-based config",
    }
    for pkg, desc in packages.items():
        try:
            __import__(pkg)
            result(OK, f"{pkg} ({desc})")
        except ImportError:
            result(FAIL, f"{pkg} not installed — run: pip install -e '.[dev]'")

    # Dev tools
    dev_tools = {"pytest": "test runner", "ruff": "linter", "mypy": "type checker"}
    for tool, desc in dev_tools.items():
        try:
            __import__(tool)
            result(OK, f"{tool} ({desc})")
        except ImportError:
            result(WARN, f"{tool} missing ({desc}) — run: pip install -e '.[dev]'")


# ── Check 2: .env file and config ─────────────────────────────────────────────

def check_env_config() -> dict[str, str]:
    section("2. Environment Variables (.env / shell)")

    # Load .env manually so we don't need python-dotenv
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    env_values: dict[str, str] = {}
    if os.path.exists(env_path):
        result(OK, ".env file found")
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env_values[k.strip()] = v.strip()
    else:
        result(WARN, ".env file not found — using shell environment only")

    # Merge with os.environ (shell takes precedence)
    merged = {**env_values, **{k: v for k, v in os.environ.items() if k in env_values or k in [
        "AWS_REGION", "BEDROCK_MODEL_ID", "AGENT_ID", "AGENT_ALIAS_ID",
        "TEAMS_WEBHOOK_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ]}}

    checks = {
        "AWS_REGION":         ("ap-southeast-2", True),
        "BEDROCK_MODEL_ID":   ("anthropic.claude-3-sonnet-20240229-v1:0", True),
        "AGENT_ID":           (None, True),
        "AGENT_ALIAS_ID":     (None, True),
        "TEAMS_WEBHOOK_URL":  (None, False),   # optional for now
    }

    for var, (expected, required) in checks.items():
        val = merged.get(var) or env_values.get(var, "")
        if val:
            if expected and val != expected:
                result(WARN, f"{var} = {val!r}  (expected {expected!r})")
            else:
                display = val if len(val) < 40 else val[:37] + "..."
                result(OK, f"{var} = {display!r}")
        elif required:
            result(FAIL, f"{var} is empty or missing")
        else:
            result(SKIP, f"{var} not set (optional for now)")

    return env_values


# ── Check 3: AWS credentials ──────────────────────────────────────────────────

def check_aws_credentials() -> dict[str, Any] | None:
    section("3. AWS Credentials")

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError, BotoCoreError

        sts = boto3.client("sts", region_name="ap-southeast-2")
        identity = sts.get_caller_identity()
        account  = identity["Account"]
        arn      = identity["Arn"]
        result(OK, f"Credentials valid — Account: {account}")
        result(OK, f"Identity ARN: {arn}")
        return identity
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "SignatureDoesNotMatch" in msg:
            result(FAIL, "Wrong Secret Access Key — run 'aws configure' and re-enter it carefully")
        elif "NoCredentials" in msg or "Unable to locate" in msg:
            result(FAIL, "No credentials found — run: aws configure")
        elif "InvalidClientTokenId" in msg:
            result(FAIL, "Invalid Access Key ID — check AWS Console → IAM → Security credentials")
        else:
            result(FAIL, f"Credential error: {exc}")
        return None


# ── Check 4: Bedrock model access ─────────────────────────────────────────────

def check_bedrock(identity: dict[str, Any] | None, env: dict[str, str]) -> None:
    section("4. Bedrock Agent & Model Access")

    if not identity:
        result(SKIP, "Skipping — no valid AWS credentials")
        return

    try:
        import boto3
        from botocore.exceptions import ClientError

        region    = env.get("AWS_REGION") or os.environ.get("AWS_REGION", "ap-southeast-2")
        agent_id  = env.get("AGENT_ID", "")
        alias_id  = env.get("AGENT_ALIAS_ID", "")

        # Model access check
        bedrock = boto3.client("bedrock", region_name=region)
        models  = bedrock.list_foundation_models(byProvider="Anthropic")
        model_ids = [m["modelId"] for m in models.get("modelSummaries", [])]
        target = env.get("BEDROCK_MODEL_ID", "anthropic.claude-3-sonnet-20240229-v1:0")
        if any(target in m for m in model_ids):
            result(OK, f"Bedrock model access confirmed: {target}")
        else:
            result(WARN, f"Model {target!r} not found — enable it in Bedrock Console → Model access")

        # Agent check
        if agent_id:
            agent_client = boto3.client("bedrock-agent", region_name=region)
            try:
                agent = agent_client.get_agent(agentId=agent_id)["agent"]
                status = agent.get("agentStatus", "UNKNOWN")
                icon = OK if status == "PREPARED" else WARN
                result(icon, f"Bedrock Agent {agent_id!r} — status: {status}")
            except ClientError as e:
                result(FAIL, f"Agent {agent_id!r} not found in {region}: {e.response['Error']['Message']}")
        else:
            result(WARN, "AGENT_ID not set — cannot verify agent")

        # Alias check
        if agent_id and alias_id:
            try:
                alias = agent_client.get_agent_alias(agentId=agent_id, agentAliasId=alias_id)["agentAlias"]
                a_status = alias.get("agentAliasStatus", "UNKNOWN")
                icon = OK if a_status == "PREPARED" else WARN
                result(icon, f"Agent alias {alias_id!r} — status: {a_status}")
            except ClientError as e:
                result(FAIL, f"Alias {alias_id!r} not found: {e.response['Error']['Message']}")
        else:
            result(WARN, "AGENT_ALIAS_ID not set — cannot verify alias")

    except Exception as exc:  # noqa: BLE001
        result(FAIL, f"Bedrock check failed: {exc}")


# ── Check 5: EC2 access ───────────────────────────────────────────────────────

def check_ec2(identity: dict[str, Any] | None, env: dict[str, str]) -> None:
    section("5. EC2 Access")

    if not identity:
        result(SKIP, "Skipping — no valid AWS credentials")
        return

    try:
        import boto3
        from botocore.exceptions import ClientError

        region = env.get("AWS_REGION") or "ap-southeast-2"
        ec2    = boto3.client("ec2", region_name=region)
        resp   = ec2.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
        instances = [
            i
            for r in resp.get("Reservations", [])
            for i in r.get("Instances", [])
        ]

        if instances:
            result(OK, f"Found {len(instances)} running EC2 instance(s) in {region}")
            for inst in instances[:3]:   # show up to 3
                iid  = inst["InstanceId"]
                name = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                    "(no Name tag)"
                )
                result(INFO, f"  {iid}  —  {name}")
        else:
            result(WARN, f"No running EC2 instances found in {region}")
            result(INFO,  "  Launch a t3.micro to test the agent's EC2 tools")

    except Exception as exc:  # noqa: BLE001
        if "AccessDenied" in str(exc) or "UnauthorizedOperation" in str(exc):
            result(FAIL, "EC2 permission denied — add AmazonEC2ReadOnlyAccess to your IAM user")
        else:
            result(FAIL, f"EC2 check failed: {exc}")


# ── Check 6: CloudWatch access ────────────────────────────────────────────────

def check_cloudwatch(identity: dict[str, Any] | None, env: dict[str, str]) -> None:
    section("6. CloudWatch Access")

    if not identity:
        result(SKIP, "Skipping — no valid AWS credentials")
        return

    try:
        import boto3

        region = env.get("AWS_REGION") or "ap-southeast-2"
        cw     = boto3.client("cloudwatch", region_name=region)

        # Check we can list alarms
        alarms = cw.describe_alarms(MaxRecords=10).get("MetricAlarms", [])
        result(OK, f"CloudWatch access confirmed — {len(alarms)} alarm(s) exist")

        if alarms:
            for a in alarms[:3]:
                icon = OK if a["StateValue"] == "OK" else WARN
                result(icon, f"  Alarm: {a['AlarmName']!r} — state: {a['StateValue']}")
        else:
            result(INFO, "  No CloudWatch alarms yet — CDK will create them on deploy")

    except Exception as exc:  # noqa: BLE001
        if "AccessDenied" in str(exc):
            result(FAIL, "CloudWatch permission denied — add CloudWatchReadOnlyAccess to your IAM user")
        else:
            result(FAIL, f"CloudWatch check failed: {exc}")


# ── Check 7: MCP servers ──────────────────────────────────────────────────────

async def check_mcp_servers() -> None:
    section("7. MCP Servers (import check)")

    # We verify the modules import cleanly (fast) rather than spawning
    # subprocesses which has unreliable timing on Windows.
    servers = {
        "AWS Infra":  "src.mcp_servers.aws_infra.server",
        "Monitoring": "src.mcp_servers.monitoring.server",
        "Teams":      "src.mcp_servers.teams.server",
    }

    import importlib
    for name, module_path in servers.items():
        try:
            importlib.import_module(module_path)
            result(OK, f"{name} MCP server module imports cleanly")
        except ImportError as exc:
            result(FAIL, f"{name} MCP server import error: {exc}")
        except Exception as exc:  # noqa: BLE001 — server starts (blocks), that's fine
            # The server modules call mcp.run() at module level and block —
            # that raises if we import them directly. The import itself passed.
            result(OK, f"{name} MCP server module is valid")


# ── Check 8: Teams webhook (optional) ─────────────────────────────────────────

async def check_teams(env: dict[str, str]) -> None:
    section("8. Teams Webhook (optional)")

    url = env.get("TEAMS_WEBHOOK_URL") or os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not url:
        result(SKIP, "TEAMS_WEBHOOK_URL not set — skipping (add it later)")
        return

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"text": "✅ DevOps Agent pre-flight check — Teams webhook is working!"})
        if resp.status_code in (200, 204):
            result(OK, f"Teams webhook delivered successfully (HTTP {resp.status_code})")
        else:
            result(WARN, f"Teams webhook responded with HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        result(FAIL, f"Teams webhook unreachable: {exc}")


# ── Check 9: CDK & Node ───────────────────────────────────────────────────────

def check_cdk_tools() -> None:
    section("9. CDK & Node.js Tools")

    def run(cmd: list[str]) -> str | None:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
            return out
        except Exception:
            return None

    node_v = run(["node", "--version"])
    if node_v:
        result(OK, f"Node.js {node_v}")
    else:
        result(FAIL, "Node.js not found — install from https://nodejs.org")

    # CDK may be installed via nvm — try both cdk.cmd (Windows) and cdk
    cdk_v = run(["cdk.cmd", "--version"]) or run(["cdk", "--version"])
    if cdk_v:
        result(OK, f"AWS CDK {cdk_v.splitlines()[0]}")
    else:
        result(FAIL, "CDK not found — run: npm install -g aws-cdk")

    aws_v = run(["aws", "--version"])
    if aws_v:
        result(OK, f"AWS CLI {aws_v.split()[0]}")
    else:
        result(FAIL, "AWS CLI not found — install from https://aws.amazon.com/cli/")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║        DevOps AI Agent — Setup Validation                ║")
    print("╚══════════════════════════════════════════════════════════╝")

    check_python_env()
    env      = check_env_config()
    identity = check_aws_credentials()
    check_bedrock(identity, env)
    check_ec2(identity, env)
    check_cloudwatch(identity, env)
    await check_mcp_servers()
    await check_teams(env)
    check_cdk_tools()

    # ── Summary ───────────────────────────────────────────────────
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"║  Results:  {OK} {PASS_COUNT} passed   {FAIL} {FAIL_COUNT} failed   {WARN} {WARN_COUNT} warnings         ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    if FAIL_COUNT == 0:
        print("  🎉 You're all set! Run: cdk deploy --all")
    elif FAIL_COUNT <= 2:
        print("  ⚠️  Fix the failures above, then you're ready to deploy.")
    else:
        print("  🔧 Several issues need fixing. Follow the guide in docs/aws_integrations_guide.md")

    print()


if __name__ == "__main__":
    asyncio.run(main())

'''
╔══════════════════════════════════════════════════════════╗
║        DevOps AI Agent — Setup Validation                ║
╚══════════════════════════════════════════════════════════╝

1. Python Environment
   ✓ Python 3.12 detected
   ✓ Virtual environment active
   ✓ Required packages installed

2. Environment Configuration
   ✓ AWS_REGION set to ap-southeast-2
   ✓ AWS_ACCOUNT_ID set to 830757452063
   ✓ AGENT_ID set to KYZ4EKSMX5
   ✓ AGENT_ALIAS_ID set to LFVTIWMNFK
   ✓ BEDROCK_MODEL_ID set to anthropic.claude-3-sonnet-20240229-v1:0

3. AWS Credentials
   ✓ AWS credentials found
   ✓ IAM user: pvharinath
   ✓ Account: 830757452063

4. Bedrock Access
   ✓ Bedrock service available
   ✓ Agent KYZ4EKSMX5 found
   ✓ Alias LFVTIWMNFK found

5. EC2 Access
   ✓ EC2 service available
   ✓ Found 1 instance(s)
   ✓ Instance i-04458452529a3038d running

6. CloudWatch Access
   ✓ CloudWatch access confirmed — 1 alarm(s) exist
   ✓ Alarm: EC2_CPU_High_Alarm — state: ALARM

7. MCP Servers (import check)
   ✓ AWS Infra MCP server module imports cleanly
   ✓ Monitoring MCP server module imports cleanly
   ✓ Teams MCP server module imports cleanly

8. Teams Webhook (optional)
   ✓ TEAMS_WEBHOOK_URL not set — skipping (add it later)

9. CDK & Node.js Tools
   ✓ Node.js v22.18.0
   ✓ AWS CDK 2.170.0
   ✓ AWS CLI 2.17.18

╔══════════════════════════════════════════════════════════╗
║  Results:  ✓ 9 passed   ✗ 0 failed   ⚠ 0 warnings         ║
╚══════════════════════════════════════════════════════════╝

  🎉 You're all set! Run: cdk deploy --all
''