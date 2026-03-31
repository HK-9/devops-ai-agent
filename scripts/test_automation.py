#!/usr/bin/env python3
"""
E2E Automation Test for DevOps AI Agent.

Tests the full pipeline: lower thresholds → stress instance → alarm fires →
EventBridge invokes agent → agent diagnoses → MINOR auto-fix / MAJOR approval → email.

MINOR vs MAJOR:
  MINOR → Agent finds 1 offending process → auto-kills it → sends "AUTO-FIXED" email
  MAJOR → Agent finds 4+ processes → sends approval email with APPROVE/REJECT links

Usage:
    python scripts/test_automation.py -i INSTANCE_ID --alarm-type cpu --scenario minor
    python scripts/test_automation.py -i INSTANCE_ID --alarm-type cpu --scenario major
    python scripts/test_automation.py -i INSTANCE_ID --alarm-type memory --scenario minor
    python scripts/test_automation.py -i INSTANCE_ID --alarm-type all --scenario both
    python scripts/test_automation.py -i INSTANCE_ID --restore
    python scripts/test_automation.py -i INSTANCE_ID --thresholds-only
    python scripts/test_automation.py -i INSTANCE_ID --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Load .env ────────────────────────────────────────────────────────────

def _load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

# ── Config ───────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
AGENT_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/devops_agent-AYHFY5ECcy"
)
LOG_GROUP = "/aws/bedrock-agentcore/runtimes/devops_agent-AYHFY5ECcy-DEFAULT"

# Original thresholds (CDK defaults)
ORIG = {"cpu": 90, "memory": 85, "disk": 90}

# Alarm definitions keyed by type
ALARM_DEFS = {
    "cpu":    {"ns": "AWS/EC2",  "metric": "CPUUtilization",  "extra_dims": []},
    "memory": {"ns": "CWAgent", "metric": "mem_used_percent", "extra_dims": []},
    "disk":   {"ns": "CWAgent", "metric": "disk_used_percent",
               "extra_dims": [{"Name": "path", "Value": "/"}]},
}

# ── Colors ───────────────────────────────────────────────────────────────

B, GR, YL, RD, CY, END = "\033[1m", "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[0m"

def log(msg, color=""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{END}" if color else f"[{ts}] {msg}")

def header(msg):
    print(f"\n{B}{'='*60}\n  {msg}\n{'='*60}{END}\n")

# ── AWS Client cache ────────────────────────────────────────────────────

_clients = {}
def _aws(svc):
    if svc not in _clients:
        _clients[svc] = boto3.client(svc, region_name=REGION)
    return _clients[svc]

def _alarm_name(iid, atype):
    return f"devops-agent-high-{atype}-{iid[-8:]}"


# ═══════════════════════════════════════════════════════════════════════
# 1. ALARM MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

def put_alarms(iid, thresholds, period=60, evals=1, dry_run=False):
    """Create/update all three CloudWatch alarms."""
    header(f"Setting Alarms → CPU={thresholds['cpu']}% MEM={thresholds['memory']}% DISK={thresholds['disk']}%")
    for atype, defn in ALARM_DEFS.items():
        name = _alarm_name(iid, atype)
        thr = thresholds[atype]
        dims = [{"Name": "InstanceId", "Value": iid}] + defn["extra_dims"]
        if dry_run:
            log(f"  [DRY RUN] {name} → {thr}%", YL); continue
        try:
            _aws("cloudwatch").put_metric_alarm(
                AlarmName=name, Namespace=defn["ns"], MetricName=defn["metric"],
                Dimensions=dims, Threshold=thr, Period=period,
                EvaluationPeriods=evals, Statistic="Average",
                ComparisonOperator="GreaterThanThreshold", TreatMissingData="missing",
                AlarmDescription=f"{defn['metric']} > {thr}% for {iid}",
            )
            log(f"  ✓ {name} → {thr}%  ({period}s period, {evals} eval)", GR)
        except ClientError as e:
            log(f"  ✗ {name}: {e}", RD)


def show_alarms(iid):
    header("Current Alarm States")
    for atype in ALARM_DEFS:
        name = _alarm_name(iid, atype)
        try:
            resp = _aws("cloudwatch").describe_alarms(AlarmNames=[name])
            alarms = resp.get("MetricAlarms", [])
            if alarms:
                a = alarms[0]
                state = a["StateValue"]
                color = RD if state == "ALARM" else GR if state == "OK" else YL
                log(f"  {atype:8} {name}  [{color}{state}{END}]  threshold={a.get('Threshold','?')}%")
            else:
                log(f"  {atype:8} {name}  [NOT FOUND]", RD)
        except ClientError:
            log(f"  {atype:8} {name}  [ERROR]", RD)


# ═══════════════════════════════════════════════════════════════════════
# 2. STRESS COMMANDS
# ═══════════════════════════════════════════════════════════════════════
#
# CRITICAL for MINOR → must create EXACTLY ONE process in `ps aux`:
#   ✗ `timeout X yes`     → 2 processes (timeout + yes)     → agent sees "multiple" → MAJOR
#   ✗ `stress-ng --vm 1`  → 2 processes (parent + worker)   → agent sees "multiple" → MAJOR
#   ✓ `yes > /dev/null`   → 1 process                       → agent sees "single"   → MINOR
#   ✓ `python3 -c "..."`  → 1 process                       → agent sees "single"   → MINOR
#
# MAJOR → 4+ separate processes so agent clearly sees "multiple offenders".

def _stress_cmd(alarm_type, scenario, duration):
    """Return shell command for the given alarm_type × scenario."""

    if alarm_type == "cpu":
        if scenario == "minor":
            # SINGLE 'yes' process — no timeout wrapper (creates 2 PIDs).
            # A background sleep+kill cleans up but uses 0% CPU → agent ignores it.
            return f"""
echo "[MINOR CPU] Starting single 'yes' process for {duration}s..."
yes > /dev/null 2>&1 &
PID=$!
echo "Single process PID: $PID"
sleep {duration}
kill $PID 2>/dev/null
echo "CPU stress done"
"""
        else:
            return f"""
which stress-ng >/dev/null 2>&1 || sudo yum install -y stress-ng 2>/dev/null || sudo apt-get install -y stress-ng 2>/dev/null
echo "[MAJOR CPU] Spawning 4 stress-ng processes for {duration}s..."
for i in 1 2 3 4; do stress-ng --cpu 1 --cpu-load 85 --timeout {duration}s & done
echo "PIDs:"; pgrep -a stress-ng
wait
echo "CPU stress done"
"""

    elif alarm_type == "memory":
        if scenario == "minor":
            # Single python3 process allocating ~300MB — exactly ONE PID in ps aux.
            return f"""
echo "[MINOR MEM] Single python3 process allocating 300MB for {duration}s..."
python3 -c "x = bytearray(300*1024*1024); import time; time.sleep({duration})" &
PID=$!
echo "Single process PID: $PID"
wait $PID 2>/dev/null
echo "Memory stress done"
"""
        else:
            return f"""
echo "[MAJOR MEM] Spawning 4 python3 memory consumers for {duration}s..."
for i in 1 2 3 4; do
  python3 -c "x=bytearray(200*1024*1024); import time; time.sleep({duration})" &
done
echo "PIDs:"; pgrep -af bytearray
wait
echo "Memory stress done"
"""

    elif alarm_type == "disk":
        if scenario == "minor":
            return f"""
echo "[MINOR DISK] Writing 1×512MB file..."
dd if=/dev/zero of=/tmp/_disk_stress_1 bs=1M count=512 conv=fsync 2>&1
echo "Holding for {duration}s..."
sleep {duration}
rm -f /tmp/_disk_stress_1
echo "Disk stress done"
"""
        else:
            return f"""
echo "[MAJOR DISK] Writing 4×512MB files..."
for i in 1 2 3 4; do dd if=/dev/zero of=/tmp/_disk_stress_$i bs=1M count=512 conv=fsync 2>&1 & done
wait
echo "Holding for {duration}s..."
sleep {duration}
rm -f /tmp/_disk_stress_*
echo "Disk stress done"
"""

    return "echo 'Unknown alarm type'"


def run_stress(iid, alarm_type, scenario, duration, dry_run=False):
    """Send stress command(s) to the instance via SSM."""
    types = ["cpu", "memory", "disk"] if alarm_type == "all" else [alarm_type]

    for at in types:
        header(f"Stress: {at.upper()} × {scenario.upper()} on {iid} ({duration}s)")
        cmd = _stress_cmd(at, scenario, duration)

        if dry_run:
            log(f"  [DRY RUN] Would run {scenario.upper()} {at} stress", YL)
            continue

        try:
            resp = _aws("ssm").send_command(
                InstanceIds=[iid], DocumentName="AWS-RunShellScript",
                Parameters={"commands": [cmd]}, TimeoutSeconds=duration + 120,
                Comment=f"E2E Test - {scenario.upper()} {at.upper()} Stress",
            )
            log(f"  SSM Command: {resp['Command']['CommandId']}", CY)
            if scenario == "minor":
                log(f"  MINOR: 1 process → agent should AUTO-FIX (kill PID)", CY)
            else:
                log(f"  MAJOR: 4 processes → agent should REQUEST APPROVAL", YL)
        except ClientError as e:
            log(f"  SSM Error: {e}", RD)


# ═══════════════════════════════════════════════════════════════════════
# 3. OBSERVE AGENT (via CloudWatch Logs)
# ═══════════════════════════════════════════════════════════════════════

_LOG_SUPPRESS = frozenset({
    "otel-rt-logs", "health", "/health", "GET /health",
    "Configuration of configurator not loaded",
    "Found credentials from IAM Role", "botocore.credentials",
})

def tail_logs(seconds=60):
    """Poll CloudWatch Logs to observe agent activity after EventBridge invokes it."""
    logs_client = boto3.client("logs", region_name=REGION)
    start_ms = int((time.time() - 5) * 1000)
    deadline = time.time() + seconds
    seen = set()

    log(f"\n  Tailing agent logs for {seconds}s (Ctrl+C to stop)...", CY)
    print(f"  {'-'*55}")

    while time.time() < deadline:
        try:
            resp = logs_client.filter_log_events(
                logGroupName=LOG_GROUP, startTime=start_ms, interleaved=True)
            for ev in resp.get("events", []):
                if ev["eventId"] in seen:
                    continue
                seen.add(ev["eventId"])
                ts = datetime.fromtimestamp(ev["timestamp"] / 1000).strftime("%H:%M:%S")
                raw = ev["message"]
                # Parse OTEL JSON envelope
                try:
                    obj = json.loads(raw)
                    body = obj.get("body", raw)
                    sev = obj.get("severityText", "")
                    scope = obj.get("scope", {}).get("name", "")
                except (json.JSONDecodeError, AttributeError):
                    body, sev, scope = raw, "", ""
                if any(s in body or s in scope for s in _LOG_SUPPRESS):
                    continue
                sc = RD if sev in ("ERROR", "FATAL") else YL if sev == "WARN" else ""
                msg = body.rstrip()
                if msg:
                    print(f"  {sc}[{ts}] [{sev or 'INFO'}] {msg}{END if sc else ''}")
                start_ms = ev["timestamp"] + 1
        except KeyboardInterrupt:
            log("\n  Stopped tailing.", YL); break
        except Exception as e:
            if "ResourceNotFoundException" in str(type(e).__name__):
                log(f"  Log group not found: {LOG_GROUP}", YL); break
            log(f"  Log error: {e}", YL)
        time.sleep(5)

    print(f"  {'-'*55}")
    log("  Done tailing.", GR)


# ═══════════════════════════════════════════════════════════════════════
# 4. WAIT FOR ALARM
# ═══════════════════════════════════════════════════════════════════════

def wait_alarm(name, timeout=300):
    """Poll alarm state until ALARM or timeout."""
    header(f"Waiting for Alarm: {name}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = _aws("cloudwatch").describe_alarms(AlarmNames=[name])
            alarms = resp.get("MetricAlarms", [])
            if not alarms:
                log(f"  Alarm {name} not found!", RD); return False
            state = alarms[0].get("StateValue", "UNKNOWN")
            if state == "ALARM":
                log(f"  ✓ ALARM triggered! ({name})", GR); return True
            log(f"  State: {state}  ({int(deadline - time.time())}s left)", YL)
        except ClientError as e:
            log(f"  Error: {e}", RD)
        time.sleep(15)
    log(f"  Timeout — alarm didn't trigger in {timeout}s", RD)
    return False


# ═══════════════════════════════════════════════════════════════════════
# 5. MANUAL AGENT INVOKE (--skip-stress only)
# ═══════════════════════════════════════════════════════════════════════

def invoke_agent(iid, alarm_type="cpu", dry_run=False):
    """Manually invoke agent with simulated alarm event. Only for --skip-stress."""
    header(f"Invoking Agent (manual) — {alarm_type.upper()}")
    event = {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": _alarm_name(iid, alarm_type),
            "state": {"value": "ALARM",
                      "reason": "Threshold Crossed: datapoint > threshold (10.0)",
                      "timestamp": datetime.now(timezone.utc).isoformat()},
            "previousState": {"value": "OK"},
            "configuration": {"metrics": [{"id": "m1", "metricStat": {
                "metric": {"namespace": "AWS/EC2", "name": "CPUUtilization",
                           "dimensions": {"InstanceId": iid}},
                "stat": "Average", "period": 60}}]},
        },
    }
    prompt = (f"A CloudWatch alarm has fired. Please investigate and take "
              f"appropriate action.\n\nAlarm Event:\n{json.dumps(event, indent=2)}")
    if dry_run:
        log("  [DRY RUN] Would invoke agent", YL); return None
    try:
        resp = _aws("bedrock-agentcore").invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=f"{uuid.uuid4()}-{uuid.uuid4()}",
            payload=json.dumps({"query": prompt}).encode(),
        )
        body = resp.get("response")
        data = body.read().decode() if hasattr(body, "read") else str(body)
        log(f"  Status: {resp.get('statusCode', 'N/A')}", GR)
        for line in data[:2000].split("\n"):
            print(f"  {line}")
        return data
    except Exception as e:
        log(f"  Error: {e}", RD)
        return None


# ═══════════════════════════════════════════════════════════════════════
# 6. MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(
        description="E2E test for DevOps AI Agent (CPU/Memory/Disk × MINOR/MAJOR)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python scripts/test_automation.py -i i-0327d856931d3b38f --alarm-type cpu --scenario minor
    python scripts/test_automation.py -i i-0327d856931d3b38f --alarm-type cpu --scenario major
    python scripts/test_automation.py -i i-0327d856931d3b38f --alarm-type memory --scenario minor
    python scripts/test_automation.py -i i-0327d856931d3b38f --alarm-type all --scenario both
    python scripts/test_automation.py -i i-0327d856931d3b38f --restore
    python scripts/test_automation.py -i i-0327d856931d3b38f --dry-run
""")
    p.add_argument("--instance-id", "-i", required=True)
    p.add_argument("--threshold", "-t", type=int, default=10, help="Test threshold %% (default: 10)")
    p.add_argument("--alarm-type", choices=["cpu", "memory", "disk", "all"], default="cpu")
    p.add_argument("--scenario", choices=["minor", "major", "both"], default="major")
    p.add_argument("--duration", "-d", type=int, default=200, help="Stress duration seconds (default: 200)")
    p.add_argument("--skip-stress", action="store_true", help="Skip stress, invoke agent manually")
    p.add_argument("--thresholds-only", action="store_true", help="Just lower thresholds and exit")
    p.add_argument("--restore", action="store_true", help="Restore original thresholds and exit")
    p.add_argument("--no-restore", action="store_true", help="Keep thresholds low after test")
    p.add_argument("--dry-run", action="store_true", help="Show what would happen without doing it")
    args = p.parse_args()
    iid = args.instance_id

    scenario_label = {"minor": "MINOR (auto-fix)", "major": "MAJOR (approval)",
                      "both": "BOTH (minor → major)"}[args.scenario]

    header("DevOps Agent — E2E Automation Test")
    log(f"  Instance:   {iid}", CY)
    log(f"  Alarm Type: {args.alarm_type}", CY)
    log(f"  Scenario:   {scenario_label}", CY)
    log(f"  Threshold:  {args.threshold}%", CY)
    log(f"  Duration:   {args.duration}s", CY)

    # ── Restore mode ──────────────────────────────────────────────────
    if args.restore:
        put_alarms(iid, ORIG, period=300, evals=2)
        show_alarms(iid)
        return

    show_alarms(iid)

    # ── Lower thresholds (only for tested alarm types) ────────────────
    test_thresholds = {
        "cpu":    args.threshold if args.alarm_type in ("cpu", "all")    else ORIG["cpu"],
        "memory": args.threshold if args.alarm_type in ("memory", "all") else ORIG["memory"],
        "disk":   args.threshold if args.alarm_type in ("disk", "all")   else ORIG["disk"],
    }
    put_alarms(iid, test_thresholds, period=60, evals=1, dry_run=args.dry_run)

    if args.thresholds_only:
        log("\n  Thresholds lowered. Run stress manually or wait for breach.", GR)
        show_alarms(iid)
        return

    # ── Run scenarios ─────────────────────────────────────────────────
    scenarios = ["minor", "major"] if args.scenario == "both" else [args.scenario]
    results = {}

    for idx, scenario in enumerate(scenarios):
        if len(scenarios) > 1:
            header(f"SCENARIO {idx+1}/{len(scenarios)}: {scenario.upper()}")

        if not args.skip_stress:
            run_stress(iid, args.alarm_type, scenario, args.duration, args.dry_run)

            if not args.dry_run:
                # Wait for CloudWatch to pick up metrics
                cw_wait = 60 if scenario == "minor" else 90
                log(f"\n  Waiting {cw_wait}s for CloudWatch to pick up metrics...", YL)
                time.sleep(cw_wait)

                # Check alarm(s) fired
                types = ["cpu", "memory", "disk"] if args.alarm_type == "all" else [args.alarm_type]
                for at in types:
                    triggered = wait_alarm(_alarm_name(iid, at), timeout=300)
                    if not triggered:
                        log(f"  {at.upper()} alarm didn't fire — agent may not be invoked", YL)

                # Tail agent logs (EventBridge invokes agent automatically when alarm fires)
                log_time = 45 if scenario == "minor" else 120
                log(f"\n  ✓ EventBridge invokes agent automatically (production flow).", GR)
                log(f"    Tailing logs for {log_time}s...", CY)
                tail_logs(seconds=log_time)
                results[scenario] = "EventBridge-triggered"
        else:
            # No real alarm → invoke agent manually with simulated event
            types = ["cpu", "memory", "disk"] if args.alarm_type == "all" else [args.alarm_type]
            for at in types:
                results[scenario] = invoke_agent(iid, at, args.dry_run)
                if len(types) > 1 and not args.dry_run:
                    time.sleep(15)

        show_alarms(iid)

        # Pause between scenarios when running both
        if len(scenarios) > 1 and scenario == "minor" and not args.dry_run:
            log("\n  Pausing 30s before MAJOR scenario...", YL)
            time.sleep(30)

    # ── Restore thresholds ────────────────────────────────────────────
    if not args.no_restore and not args.dry_run:
        delay = 10 if args.scenario == "minor" else 20
        log(f"\n  Restoring thresholds in {delay}s (Ctrl+C to keep low)...", YL)
        try:
            time.sleep(delay)
            put_alarms(iid, ORIG, period=300, evals=2)
        except KeyboardInterrupt:
            log("\n  Keeping low thresholds.", YL)

    # ── Summary ───────────────────────────────────────────────────────
    header("TEST SUMMARY")
    log(f"  Instance:   {iid}", CY)
    log(f"  Alarm Type: {args.alarm_type}", CY)
    log(f"  Scenario:   {scenario_label}", CY)
    print()

    for scenario in results:
        log(f"  {scenario.upper():6}  ✓ Triggered", GR)
        if scenario == "minor":
            log(f"    Expected: Agent finds 1 process → auto-kills PID → sends 'AUTO-FIXED' email", CY)
            log(f"    Verify:   1 email, NO approval links, killed PID mentioned in body", GR)
        else:
            log(f"    Expected: Agent finds 4+ processes → requests approval → 2 emails", CY)
            log(f"    Verify:   Email 1 = APPROVE/REJECT links  |  Email 2 = diagnostic summary", YL)
        print()

    log(f"  Tail logs manually:", YL)
    log(f"    aws logs tail {LOG_GROUP} --follow --region {REGION}", CY)


if __name__ == "__main__":
    main()
 