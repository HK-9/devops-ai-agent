"""
Deploy the Approval Handler Lambda — AgentCore-native version.

Updates the existing `devops-agent-approval-handler` Lambda function
with the new code that invokes the AgentCore agent instead of
executing actions directly.

This replaces the old CDK-deployed inline Lambda code.

Usage:
    python scripts/deploy_approval_handler.py              # deploy
    python scripts/deploy_approval_handler.py --dry-run    # show what would change
    python scripts/deploy_approval_handler.py --setup      # create Lambda if missing
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

import boto3

# ── Configuration ────────────────────────────────────────────────────────

REGION         = "ap-southeast-2"
ACCOUNT        = "650251690796"
FUNCTION_NAME  = "devops-agent-approval-handler"
HANDLER        = "approval_handler.handler"
RUNTIME        = "python3.12"
TIMEOUT        = 180        # 3 minutes (agent invocation can take a while)
MEMORY         = 256
ROLE_NAME      = "devops-agent-approval-handler-role"

AGENT_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "runtime/devops_agent-AYHFY5ECcy"
)

# Source file for the Lambda
HANDLER_FILE = Path(__file__).resolve().parent.parent / "deploy_sns" / "approval_handler.py"

# ── Colours for terminal output ──────────────────────────────────────────

class C:
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"


def log(msg, color=""):
    print(f"{color}{msg}{C.RESET}")


# ── Lambda packaging ────────────────────────────────────────────────────

def _build_zip() -> bytes:
    """Package approval_handler.py into an in-memory zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(HANDLER_FILE, "approval_handler.py")
    return buf.getvalue()


# ── IAM Role ─────────────────────────────────────────────────────────────

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

PERMISSIONS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Logs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{FUNCTION_NAME}*",
        },
        {
            "Sid": "DynamoDB",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
            ],
            "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/devops-agent-approvals",
        },
        {
            "Sid": "SNSPublish",
            "Effect": "Allow",
            "Action": ["sns:Publish"],
            "Resource": f"arn:aws:sns:{REGION}:{ACCOUNT}:devops-agent-alerts",
        },
        {
            "Sid": "InvokeAgent",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeRuntime"],
            "Resource": AGENT_RUNTIME_ARN,
        },
        {
            "Sid": "EC2Actions",
            "Effect": "Allow",
            "Action": [
                "ec2:RebootInstances",
                "ec2:StopInstances",
                "ec2:StartInstances",
            ],
            "Resource": "*",
        },
        {
            "Sid": "SSMCommands",
            "Effect": "Allow",
            "Action": [
                "ssm:SendCommand",
                "ssm:GetCommandInvocation",
            ],
            "Resource": "*",
        },
        {
            "Sid": "SchedulerCleanup",
            "Effect": "Allow",
            "Action": ["scheduler:DeleteSchedule"],
            "Resource": f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/default/devops-approval-reminder-*",
        },
    ],
}


def ensure_role() -> str:
    """Create or update the IAM role. Returns the role ARN."""
    iam = boto3.client("iam", region_name=REGION)

    try:
        role = iam.get_role(RoleName=ROLE_NAME)
        role_arn = role["Role"]["Arn"]
        log(f"  IAM role exists: {ROLE_NAME}", C.GREEN)
    except iam.exceptions.NoSuchEntityException:
        log(f"  Creating IAM role: {ROLE_NAME}")
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Execution role for DevOps Agent approval handler Lambda",
        )
        role_arn = resp["Role"]["Arn"]
        log(f"  Created role: {role_arn}", C.GREEN)
        # Wait for propagation
        import time
        time.sleep(10)

    # Update inline policy
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="approval-handler-permissions",
        PolicyDocument=json.dumps(PERMISSIONS_POLICY),
    )
    log(f"  Updated permissions policy", C.GREEN)

    return role_arn


# ── Deploy ───────────────────────────────────────────────────────────────

def deploy(dry_run: bool = False, setup: bool = False):
    """Deploy or update the approval handler Lambda."""
    lm = boto3.client("lambda", region_name=REGION)

    log(f"\n{'=' * 60}", C.BOLD)
    log(f"  Deploying Approval Handler Lambda", C.BOLD)
    log(f"  Function: {FUNCTION_NAME}", C.BOLD)
    log(f"  Handler:  {HANDLER}", C.BOLD)
    log(f"  Agent:    {AGENT_RUNTIME_ARN}", C.BOLD)
    log(f"{'=' * 60}", C.BOLD)

    if dry_run:
        log("\n  [DRY RUN] No changes will be made.\n", C.YELLOW)
        try:
            fn = lm.get_function(FunctionName=FUNCTION_NAME)
            log(f"  Lambda exists: {fn['Configuration']['FunctionArn']}")
            log(f"  Runtime: {fn['Configuration']['Runtime']}")
            log(f"  Last modified: {fn['Configuration']['LastModified']}")
            env = fn['Configuration'].get('Environment', {}).get('Variables', {})
            log(f"  AGENT_RUNTIME_ARN: {env.get('AGENT_RUNTIME_ARN', '(not set)')}")
        except lm.exceptions.ResourceNotFoundException:
            log(f"  Lambda does NOT exist — use --setup to create it", C.YELLOW)
        return

    # Build zip
    log(f"\n  Building deployment package …")
    zip_bytes = _build_zip()
    log(f"  Package size: {len(zip_bytes):,} bytes", C.GREEN)

    # Check if function exists
    try:
        lm.get_function(FunctionName=FUNCTION_NAME)
        exists = True
    except lm.exceptions.ResourceNotFoundException:
        exists = False

    if not exists and not setup:
        log(f"\n  Lambda '{FUNCTION_NAME}' does not exist.", C.RED)
        log(f"  Use --setup to create it, or deploy the CDK stack first.", C.YELLOW)
        sys.exit(1)

    # Ensure IAM role
    log(f"\n  Ensuring IAM role …")
    role_arn = ensure_role()

    env_vars = {
        "APPROVALS_TABLE": "devops-agent-approvals",
        "SNS_TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:devops-agent-alerts",
        "AGENT_RUNTIME_ARN": AGENT_RUNTIME_ARN,
    }

    if exists:
        # Update function code
        log(f"\n  Updating Lambda code …")
        lm.update_function_code(
            FunctionName=FUNCTION_NAME,
            ZipFile=zip_bytes,
        )
        log(f"  ✓ Code updated", C.GREEN)

        # Wait for update to complete
        _wait_for_update(lm)

        # Update configuration
        log(f"  Updating Lambda configuration …")
        lm.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Runtime=RUNTIME,
            Handler=HANDLER,
            Timeout=TIMEOUT,
            MemorySize=MEMORY,
            Environment={"Variables": env_vars},
            Role=role_arn,
        )
        log(f"  ✓ Configuration updated", C.GREEN)
    else:
        # Create function
        log(f"\n  Creating Lambda function …")
        lm.create_function(
            FunctionName=FUNCTION_NAME,
            Runtime=RUNTIME,
            Handler=HANDLER,
            Role=role_arn,
            Code={"ZipFile": zip_bytes},
            Timeout=TIMEOUT,
            MemorySize=MEMORY,
            Environment={"Variables": env_vars},
            Description="Approval click handler — invokes AgentCore agent for execution",
        )
        log(f"  ✓ Lambda created", C.GREEN)

    # Verify
    _wait_for_update(lm)
    fn = lm.get_function(FunctionName=FUNCTION_NAME)
    log(f"\n  ✓ Deployed: {fn['Configuration']['FunctionArn']}", C.GREEN)
    log(f"  ✓ Handler:  {fn['Configuration']['Handler']}")
    log(f"  ✓ Runtime:  {fn['Configuration']['Runtime']}")
    env = fn['Configuration'].get('Environment', {}).get('Variables', {})
    log(f"  ✓ Agent ARN: {env.get('AGENT_RUNTIME_ARN', 'NOT SET')}")

    log(f"\n  {'=' * 60}", C.BOLD)
    log(f"  Approval handler deployed successfully!", C.GREEN)
    log(f"  {'=' * 60}\n", C.BOLD)


def _wait_for_update(lm, max_wait=30):
    """Wait for Lambda to finish updating."""
    import time
    for _ in range(max_wait):
        try:
            resp = lm.get_function(FunctionName=FUNCTION_NAME)
            state = resp["Configuration"].get("LastUpdateStatus", "Successful")
            if state in ("Successful", ""):
                return
            time.sleep(1)
        except Exception:
            time.sleep(1)


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Deploy the Approval Handler Lambda (AgentCore-native)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without deploying")
    parser.add_argument("--setup", action="store_true",
                        help="Create the Lambda if it doesn't exist")
    args = parser.parse_args()
    deploy(dry_run=args.dry_run, setup=args.setup)


if __name__ == "__main__":
    main()
 