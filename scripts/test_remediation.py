#!/usr/bin/env python3
"""
DevOps AI Agent — Remediation Test Script

Mock CloudWatch alarm state changes to trigger the full remediation pipeline
WITHOUT waiting for real metric thresholds.

Two modes:
  1. MOCK-ONLY  — Force alarm state to ALARM via set_alarm_state, which
                   triggers EventBridge → Lambda → Agent automatically.
  2. STRESS+MOCK — SSH into the instance via SSM to start real stress
                    processes, THEN force the alarm so the agent sees
                    actual offending processes when it diagnoses.

Scenarios:
  MINOR — Single offending process → Agent should auto-fix (kill PID)
          and send "AUTO-FIXED" notification.
  MAJOR — 4 offending processes → Agent should request human approval
          via email with APPROVE/REJECT links.

Usage:
    # MINOR CPU — start 1 stress process, then mock-trigger alarm
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type cpu --scenario minor

    # MAJOR CPU — start 4 stress processes, then mock-trigger alarm
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type cpu --scenario major

    # Mock-only (no SSH stress, just force alarm state)
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type cpu --scenario minor --mock-only

    # MINOR memory
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type memory --scenario minor

    # MAJOR memory
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type memory --scenario major

    # Disk
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type disk --scenario minor

    # Dry run — show what would happen without doing anything
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type cpu --scenario minor --dry-run

    # Direct agent invoke (bypass Lambda, call agent directly with mock alarm)
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --type cpu --scenario minor --direct

    # Tail agent logs after test
    python scripts/test_remediation.py --logs

    # Restore alarm states to OK after testing
    python scripts/test_remediation.py --instance i-0327d856931d3b38f --restore
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Bootstrap ────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).resolve().parent))

_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ───────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
AGENT_RUNTIME_ARN = os.environ.get(
    "AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy",
)
LOG_GROUP = "/aws/bedrock-agentcore/runtimes/devops_agent-AYHFY5ECcy-DEFAULT"

# Stress durations (seconds)
STRESS_DURATION_MINOR = 120
STRESS_DURATION_MAJOR = 240

# ── Colors ───────────────────────────────────────────────────────────────

BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
DIM = "\033[2m"
END = "\033[0m"


def log(msg, color=""):
    ts = datetime.now().strftime("%H:%M:%S")
    if color:
        print(f"{color}[{ts}] {msg}{END}")
    else:
        print(f"[{ts}] {msg}")


def header(msg):
    print(f"\n{BOLD}{'=' * 64}")
    print(f"  {msg}")
    print(f"{'=' * 64}{END}\n")


def success(msg):
    log(msg, GREEN)


def warn(msg):
    log(msg, YELLOW)


def error(msg):
    log(msg, RED)


# ── AWS Clients ──────────────────────────────────────────────────────────

_clients: dict = {}


def aws(service: str):
    if service not in _clients:
        _clients[service] = boto3.client(service, region_name=REGION)
    return _clients[service]


# ── Alarm helpers ────────────────────────────────────────────────────────


def alarm_name(instance_id: str, alarm_type: str) -> str:
    """CloudWatch alarm name following the project convention."""
    suffix = instance_id[-8:]
    return f"devops-agent-high-{alarm_type}-{suffix}"


def get_alarm_state(name: str) -> dict | None:
    """Fetch current alarm info."""
    try:
        resp = aws("cloudwatch").describe_alarms(AlarmNames=[name])
        alarms = resp.get("MetricAlarms", [])
        return alarms[0] if alarms else None
    except ClientError as e:
        error(f"Failed to get alarm {name}: {e}")
        return None


def force_alarm_state(name: str, state: str = "ALARM", reason: str = "", alarm_type: str = "cpu") -> bool:
    """Force a CloudWatch alarm to the given state.

    set_alarm_state triggers EventBridge alarm state change events,
    which in turn invoke the Lambda → Agent pipeline.

    The reason text is crafted to look exactly like a real CloudWatch
    alarm breach — no "mock" or "test" language — so the agent produces
    production-ready emails.
    """
    metric_info = {
        "cpu": {"datapoints": [98.5, 97.2, 99.1], "threshold": 90.0, "metric": "CPUUtilization"},
        "memory": {"datapoints": [96.8, 95.4, 97.1], "threshold": 85.0, "metric": "mem_used_percent"},
        "disk": {"datapoints": [94.5, 93.8, 94.2], "threshold": 90.0, "metric": "disk_used_percent"},
    }
    info = metric_info.get(alarm_type, metric_info["cpu"])

    if not reason:
        reason = (
            f"Threshold Crossed: 3 out of 3 datapoints "
            f"[{info['datapoints'][0]}, {info['datapoints'][1]}, {info['datapoints'][2]}] "
            f"were greater than the threshold ({info['threshold']})"
        )
    try:
        aws("cloudwatch").set_alarm_state(
            AlarmName=name,
            StateValue=state,
            StateReason=reason,
            StateReasonData=json.dumps(
                {
                    "version": "1.0",
                    "queryDate": datetime.now(timezone.utc).isoformat(),
                    "startDate": datetime.now(timezone.utc).isoformat(),
                    "statistic": "Average",
                    "period": 60,
                    "recentDatapoints": info["datapoints"],
                    "threshold": info["threshold"],
                }
            ),
        )
        success(f"  Alarm {name} → {state}")
        return True
    except ClientError as e:
        error(f"  Failed to set alarm state: {e}")
        return False


def restore_alarm(instance_id: str) -> None:
    """Restore all alarms for an instance back to OK."""
    header(f"Restoring alarms for {instance_id}")
    for atype in ("cpu", "memory", "disk"):
        name = alarm_name(instance_id, atype)
        alarm = get_alarm_state(name)
        if not alarm:
            warn(f"  {name} — not found, skipping")
            continue
        if alarm["StateValue"] == "OK":
            log(f"  {name} — already OK")
            continue
        force_alarm_state(name, "OK", "Restored to OK after remediation testing")
    success("All alarms restored to OK")


# ── Stress commands via SSM ──────────────────────────────────────────────


def _stress_cmd(alarm_type: str, scenario: str, duration: int) -> str:
    """Build the shell command to run on the instance."""

    if alarm_type == "cpu":
        if scenario == "minor":
            return f"""\
echo "[MINOR CPU] Starting single stress process for {duration}s..."
# Install stress-ng if missing
which stress-ng >/dev/null 2>&1 || sudo yum install -y stress-ng 2>/dev/null || sudo apt-get install -y stress-ng 2>/dev/null
# Single CPU stress — exactly 1 worker process
nohup stress-ng --cpu 1 --cpu-load 95 --timeout {duration}s > /dev/null 2>&1 &
PID=$(pgrep -n stress-ng)
echo "MINOR: 1 stress process started (PID: $PID)"
echo "Agent should AUTO-FIX by killing this PID"
"""
        else:
            return f"""\
echo "[MAJOR CPU] Starting 4 stress processes for {duration}s..."
which stress-ng >/dev/null 2>&1 || sudo yum install -y stress-ng 2>/dev/null || sudo apt-get install -y stress-ng 2>/dev/null
for i in 1 2 3 4; do
    nohup stress-ng --cpu 1 --cpu-load 85 --timeout {duration}s > /dev/null 2>&1 &
done
echo "MAJOR: 4 stress processes started"
pgrep -a stress-ng
echo "Agent should REQUEST APPROVAL (too many processes to auto-fix)"
"""

    elif alarm_type == "memory":
        if scenario == "minor":
            return f"""\
echo "[MINOR MEM] Starting single memory consumer for {duration}s..."
nohup python3 -c "
x = bytearray(300 * 1024 * 1024)  # 300MB
import time; time.sleep({duration})
" > /dev/null 2>&1 &
PID=$!
echo "MINOR: 1 memory process started (PID: $PID)"
echo "Agent should AUTO-FIX by killing this PID"
"""
        else:
            return f"""\
echo "[MAJOR MEM] Starting 4 memory consumers for {duration}s..."
for i in 1 2 3 4; do
    nohup python3 -c "
x = bytearray(200 * 1024 * 1024)  # 200MB each
import time; time.sleep({duration})
" > /dev/null 2>&1 &
done
echo "MAJOR: 4 memory processes started"
pgrep -af bytearray
echo "Agent should REQUEST APPROVAL"
"""

    elif alarm_type == "disk":
        if scenario == "minor":
            return f"""\
echo "[MINOR DISK] Writing 512MB file to fill disk..."
dd if=/dev/zero of=/tmp/_test_disk_fill_1 bs=1M count=512 conv=fsync 2>&1
echo "MINOR: 512MB written to /tmp/_test_disk_fill_1"
echo "Agent should AUTO-FIX by cleaning up"
echo "File will auto-cleanup in {duration}s..."
nohup bash -c "sleep {duration}; rm -f /tmp/_test_disk_fill_*" > /dev/null 2>&1 &
"""
        else:
            return f"""\
echo "[MAJOR DISK] Writing 4x512MB files to fill disk..."
for i in 1 2 3 4; do
    dd if=/dev/zero of=/tmp/_test_disk_fill_$i bs=1M count=512 conv=fsync 2>&1 &
done
wait
echo "MAJOR: 2GB written to /tmp/_test_disk_fill_*"
echo "Agent should REQUEST APPROVAL (severe disk usage)"
echo "Files will auto-cleanup in {duration}s..."
nohup bash -c "sleep {duration}; rm -f /tmp/_test_disk_fill_*" > /dev/null 2>&1 &
"""

    return "echo 'Unknown alarm type'"


def run_stress(instance_id: str, alarm_type: str, scenario: str, dry_run: bool = False) -> bool:
    """Send stress commands to the instance via SSM."""
    duration = STRESS_DURATION_MINOR if scenario == "minor" else STRESS_DURATION_MAJOR
    cmd = _stress_cmd(alarm_type, scenario, duration)

    header(f"Starting {scenario.upper()} {alarm_type.upper()} stress on {instance_id}")

    if dry_run:
        warn("  [DRY RUN] Would send SSM command:")
        for line in cmd.strip().split("\n"):
            print(f"    {DIM}{line}{END}")
        return True

    try:
        resp = aws("ssm").send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [cmd]},
            TimeoutSeconds=duration + 120,
            Comment=f"Remediation test — {scenario.upper()} {alarm_type.upper()}",
        )
        cmd_id = resp["Command"]["CommandId"]
        success(f"  SSM Command sent: {cmd_id}")

        # Wait briefly for SSM to start
        log("  Waiting 5s for stress processes to start...")
        time.sleep(5)

        # Check command status
        try:
            output = aws("ssm").get_command_invocation(
                CommandId=cmd_id,
                InstanceId=instance_id,
            )
            status = output.get("Status", "Unknown")
            stdout = output.get("StandardOutputContent", "")
            if stdout:
                for line in stdout.strip().split("\n"):
                    log(f"    {line}", CYAN)
            if status in ("Success", "InProgress"):
                success(f"  Stress command status: {status}")
            else:
                warn(f"  Stress command status: {status}")
        except ClientError:
            log("  (SSM output not yet available — stress is likely still starting)")

        return True

    except ClientError as e:
        error(f"  SSM Error: {e}")
        return False


# ── Direct agent invocation ──────────────────────────────────────────────


def build_mock_alarm_event(instance_id: str, alarm_type: str) -> dict:
    """Build a mock CloudWatch alarm EventBridge event."""
    aname = alarm_name(instance_id, alarm_type)

    metric_map = {
        "cpu": {"namespace": "AWS/EC2", "name": "CPUUtilization", "value": 98.2},
        "memory": {"namespace": "CWAgent", "name": "mem_used_percent", "value": 96.8},
        "disk": {"namespace": "CWAgent", "name": "disk_used_percent", "value": 94.5},
    }
    metric = metric_map.get(alarm_type, metric_map["cpu"])

    return {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": aname,
            "state": {
                "value": "ALARM",
                "reason": (
                    f"Threshold Crossed: 3 out of 3 datapoints "
                    f"[{metric['value']}, {metric['value'] - 1}, {metric['value'] + 0.5}] "
                    f"were greater than the threshold (90.0)"
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "previousState": {"value": "OK"},
            "configuration": {
                "metrics": [
                    {
                        "id": "m1",
                        "metricStat": {
                            "metric": {
                                "namespace": metric["namespace"],
                                "name": metric["name"],
                                "dimensions": {"InstanceId": instance_id},
                            },
                            "stat": "Average",
                            "period": 60,
                        },
                    }
                ],
            },
        },
    }


def invoke_agent_direct(instance_id: str, alarm_type: str, dry_run: bool = False) -> str | None:
    """Invoke the deployed agent directly with a mock alarm event.

    This bypasses EventBridge and Lambda — sends the alarm event
    directly to the agent via invoke_agent_runtime.
    """
    event = build_mock_alarm_event(instance_id, alarm_type)
    prompt = (
        f"A CloudWatch alarm has fired. Please investigate and take "
        f"appropriate action.\n\n"
        f"Alarm Event:\n{json.dumps(event, indent=2)}"
    )

    header(f"Direct Agent Invocation — {alarm_type.upper()} alarm on {instance_id}")

    if dry_run:
        warn("  [DRY RUN] Would invoke agent with prompt:")
        print(f"    {DIM}{prompt[:300]}...{END}")
        return None

    try:
        client = aws("bedrock-agentcore")
        session_id = f"{uuid.uuid4()}-{uuid.uuid4()}"

        log(f"  Invoking agent (ARN: ...{AGENT_RUNTIME_ARN[-20:]})")
        log(f"  Alarm: {alarm_name(instance_id, alarm_type)}")

        resp = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=json.dumps({"query": prompt}).encode(),
        )

        body = resp.get("response")
        if hasattr(body, "read"):
            data = body.read().decode("utf-8")
        else:
            data = str(body)

        status = resp.get("statusCode", 200)

        # Parse response
        try:
            parsed = json.loads(data)
            response_text = parsed.get("response", data)
        except (json.JSONDecodeError, TypeError):
            response_text = data

        if status >= 400:
            error(f"  Agent returned HTTP {status}")
            error(f"  {response_text[:500]}")
            return None

        success(f"  Agent responded ({len(response_text)} chars)")
        print()
        # Print response with indentation
        for line in response_text.split("\n"):
            print(f"  {CYAN}{line}{END}")
        print()

        # Analyze response
        resp_lower = response_text.lower()
        if "auto-fixed" in resp_lower or "killed" in resp_lower or "remediat" in resp_lower:
            success("  ✅ RESULT: Agent AUTO-FIXED the issue (MINOR path)")
        elif "approval" in resp_lower or "approve" in resp_lower:
            success("  ✅ RESULT: Agent requested APPROVAL (MAJOR path)")
        elif "no issue" in resp_lower or "healthy" in resp_lower or "normal" in resp_lower:
            warn("  ⚠️  RESULT: Agent found no issue (stress may not have started yet)")
        else:
            log("  ℹ️  RESULT: Review agent response above")

        return response_text

    except ClientError as e:
        error(f"  Agent invocation failed: {e}")
        return None
    except Exception as e:
        error(f"  Unexpected error: {e}")
        return None


# ── Log tailing ──────────────────────────────────────────────────────────


def tail_logs(minutes: int = 5) -> None:
    """Tail recent agent CloudWatch logs."""
    header(f"Agent Logs (last {minutes} min)")

    try:
        client = aws("logs")
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (minutes * 60 * 1000)

        resp = client.filter_log_events(
            logGroupName=LOG_GROUP,
            startTime=start_ms,
            endTime=end_ms,
            limit=200,
            interleaved=True,
        )

        events = resp.get("events", [])
        if not events:
            warn("  No log events found")
            return

        for event in events:
            msg = event.get("message", "").strip()
            # Skip health check noise
            if "GET /health" in msg or "GET / " in msg:
                continue
            # Skip binary noise
            if msg.startswith("\x16") or "Bad request version" in msg:
                continue

            ts = datetime.fromtimestamp(event["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M:%S")

            # Color-code log levels
            if "ERROR" in msg:
                print(f"  {RED}[{ts}] {msg[:200]}{END}")
            elif "WARN" in msg:
                print(f"  {YELLOW}[{ts}] {msg[:200]}{END}")
            elif any(kw in msg for kw in ("SUCCESS", "AUTO-FIXED", "EXECUTED")):
                print(f"  {GREEN}[{ts}] {msg[:200]}{END}")
            elif any(kw in msg for kw in ("Invoking", "Intent", "Tool")):
                print(f"  {CYAN}[{ts}] {msg[:200]}{END}")
            else:
                print(f"  [{ts}] {msg[:200]}")

    except ClientError as e:
        error(f"  Failed to read logs: {e}")


# ── Main test flows ──────────────────────────────────────────────────────


def run_test(
    instance_id: str,
    alarm_type: str,
    scenario: str,
    mock_only: bool = False,
    direct: bool = False,
    dry_run: bool = False,
) -> None:
    """Execute a full remediation test."""
    aname = alarm_name(instance_id, alarm_type)

    header(
        f"REMEDIATION TEST\n"
        f"  Instance:  {instance_id}\n"
        f"  Type:      {alarm_type.upper()}\n"
        f"  Scenario:  {scenario.upper()} "
        f"({'1 process → auto-fix' if scenario == 'minor' else '4 processes → approval'})\n"
        f"  Mode:      {'MOCK-ONLY' if mock_only else 'DIRECT' if direct else 'STRESS + MOCK'}\n"
        f"  Dry run:   {dry_run}"
    )

    # Step 0: Verify alarm exists
    alarm = get_alarm_state(aname)
    if not alarm:
        error(f"  Alarm {aname} not found! Create it first with test_automation.py or CDK.")
        return
    log(f"  Alarm {aname} found (current state: {alarm['StateValue']}, threshold: {alarm.get('Threshold')})")

    # Step 1: Start stress processes (unless mock-only)
    if not mock_only and not direct:
        ok = run_stress(instance_id, alarm_type, scenario, dry_run)
        if not ok:
            error("Failed to start stress. Aborting.")
            return
        # Give stress a moment to ramp up
        if not dry_run:
            log("  Waiting 10s for stress to ramp up...")
            time.sleep(10)

    # Step 2: Trigger the alarm
    if direct:
        # Bypass EventBridge/Lambda — call agent directly
        invoke_agent_direct(instance_id, alarm_type, dry_run)
    else:
        # Force alarm state → EventBridge → Lambda → Agent
        header(f"Forcing alarm {aname} → ALARM")
        if dry_run:
            warn(f"  [DRY RUN] Would set {aname} to ALARM state")
        else:
            ok = force_alarm_state(aname, alarm_type=alarm_type)
            if not ok:
                error("Failed to trigger alarm. Aborting.")
                return

            log("")
            success("  ✅ Alarm triggered! The pipeline is now running:")
            log(f"     CloudWatch ({aname})")
            log(f"       → EventBridge rule")
            log(f"       → Lambda handler")
            log(f"       → Agent (invoke_agent_runtime)")
            log(f"       → Agent diagnoses, remediates, notifies")
            log("")

            if scenario == "minor":
                log("  Expected outcome (MINOR):", BOLD)
                log("    1. Agent calls diagnose_instance_tool")
                log("    2. Finds single offending process")
                log("    3. Calls remediate_high_{}_tool to kill it".format(alarm_type))
                log("    4. Sends AUTO-FIXED notification via email/Teams")
            else:
                log("  Expected outcome (MAJOR):", BOLD)
                log("    1. Agent calls diagnose_instance_tool")
                log("    2. Finds multiple offending processes")
                log("    3. Calls request_approval_tool")
                log("    4. Email sent with APPROVE / REJECT links")
                log("    5. Sends diagnostic summary notification")

            log("")
            log("  Check results:", BOLD)
            log("    • Agent logs:  python scripts/test_remediation.py --logs")
            log("    • Email:       Check your inbox for the notification")
            log(f"    • Alarm state: python scripts/test_remediation.py --instance {instance_id} --restore")


# ── CLI ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="DevOps AI Agent — Remediation Test Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # MINOR CPU — stress + mock alarm (full pipeline)
  %(prog)s -i i-0327d856931d3b38f --type cpu --scenario minor

  # MAJOR CPU — 4 stress processes + mock alarm
  %(prog)s -i i-0327d856931d3b38f --type cpu --scenario major

  # Mock-only — just force alarm state (no SSH stress)
  %(prog)s -i i-0327d856931d3b38f --type cpu --scenario minor --mock-only

  # Direct — call agent directly (bypass Lambda)
  %(prog)s -i i-0327d856931d3b38f --type cpu --scenario minor --direct

  # Memory + Disk tests
  %(prog)s -i i-0327d856931d3b38f --type memory --scenario minor
  %(prog)s -i i-0327d856931d3b38f --type disk --scenario major

  # Tail agent logs
  %(prog)s --logs
  %(prog)s --logs --minutes 10

  # Restore alarms to OK
  %(prog)s -i i-0327d856931d3b38f --restore

  # Dry run
  %(prog)s -i i-0327d856931d3b38f --type cpu --scenario minor --dry-run
""",
    )

    parser.add_argument(
        "-i",
        "--instance",
        help="EC2 instance ID to test (e.g. i-0327d856931d3b38f)",
    )
    parser.add_argument(
        "--type",
        choices=["cpu", "memory", "disk"],
        default="cpu",
        help="Alarm type to trigger (default: cpu)",
    )
    parser.add_argument(
        "--scenario",
        choices=["minor", "major"],
        default="minor",
        help="MINOR (1 process → auto-fix) or MAJOR (4 processes → approval)",
    )
    parser.add_argument(
        "--mock-only",
        action="store_true",
        help="Only mock the alarm state — don't SSH to start stress processes",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Invoke agent directly (bypass EventBridge + Lambda)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without executing",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore all alarms for the instance to OK state",
    )
    parser.add_argument(
        "--logs",
        action="store_true",
        help="Tail recent agent CloudWatch logs",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=5,
        help="Minutes of logs to tail (default: 5)",
    )

    args = parser.parse_args()

    # Handle log-only mode
    if args.logs:
        tail_logs(minutes=args.minutes)
        return

    # All other modes require an instance ID
    if not args.instance:
        parser.error("--instance / -i is required (except for --logs)")

    # Restore mode
    if args.restore:
        restore_alarm(args.instance)
        return

    # Run the test
    run_test(
        instance_id=args.instance,
        alarm_type=args.type,
        scenario=args.scenario,
        mock_only=args.mock_only,
        direct=args.direct,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
