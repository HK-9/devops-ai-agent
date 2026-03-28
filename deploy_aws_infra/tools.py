"""
AWS Infrastructure MCP tools.

Provides EC2 management tools: list, describe, restart instances,
and SSM RunCommand for remote diagnostics / remediation.
Each function is designed to be registered as an MCP tool.
"""

from __future__ import annotations

import time
from typing import Any

from config import settings
from aws_helpers import get_client, safe_boto_call, setup_logging

logger = setup_logging("mcp.aws-infra")


# ── Tool Implementations ────────────────────────────────────────────────


async def list_ec2_instances(
    state_filter: str = "running",
    tag_filters: str | dict[str, str] | None = None,
    max_results: int = 50,
) -> dict[str, Any]:
    """List EC2 instances, optionally filtered by state and tags.

    Args:
        state_filter: Instance state — "running", "stopped", "all".
        tag_filters:  Optional comma-separated key=value pairs, e.g. 'Name=web,Env=prod'.
        max_results:  Maximum number of instances to return.

    Returns:
        Dict with ``instances`` list, each containing id, type, state,
        launch_time, public_ip, private_ip, name, and tags.
    """
    ec2 = get_client("ec2")

    # Parse tag_filters: accept string "Key=Value,Key2=Value2" or dict
    parsed_tags: dict[str, str] = {}
    if isinstance(tag_filters, str) and tag_filters.strip():
        for pair in tag_filters.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                parsed_tags[k.strip()] = v.strip()
    elif isinstance(tag_filters, dict):
        parsed_tags = tag_filters

    filters: list[dict[str, Any]] = []
    if state_filter and state_filter != "all":
        filters.append({"Name": "instance-state-name", "Values": [state_filter]})
    if parsed_tags:
        for key, value in parsed_tags.items():
            filters.append({"Name": f"tag:{key}", "Values": [value]})

    kwargs: dict[str, Any] = {"MaxResults": min(max_results, 1000)}
    if filters:
        kwargs["Filters"] = filters

    response = safe_boto_call(ec2.describe_instances, **kwargs)
    if "error" in response:
        return response

    instances = []
    for reservation in response.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            name = ""
            for tag in inst.get("Tags", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]
                    break

            instances.append({
                "instance_id": inst["InstanceId"],
                "instance_type": inst.get("InstanceType", ""),
                "state": inst["State"]["Name"],
                "launch_time": str(inst.get("LaunchTime", "")),
                "public_ip": inst.get("PublicIpAddress", "N/A"),
                "private_ip": inst.get("PrivateIpAddress", "N/A"),
                "name": name,
                "availability_zone": inst.get("Placement", {}).get("AvailabilityZone", ""),
                "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
            })

    logger.info("Listed %d EC2 instances (filter=%s)", len(instances), state_filter)
    return {"instances": instances, "count": len(instances)}


async def describe_ec2_instance(instance_id: str) -> dict[str, Any]:
    """Get detailed information about a single EC2 instance.

    Args:
        instance_id: The EC2 instance ID (e.g. ``i-0abc123def456``).

    Returns:
        Dict with full instance details, or an error dict.
    """
    ec2 = get_client("ec2")
    response = safe_boto_call(ec2.describe_instances, InstanceIds=[instance_id])
    if "error" in response:
        return response

    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        return {"error": True, "message": f"Instance {instance_id} not found"}

    inst = reservations[0]["Instances"][0]
    name = ""
    for tag in inst.get("Tags", []):
        if tag["Key"] == "Name":
            name = tag["Value"]
            break

    result = {
        "instance_id": inst["InstanceId"],
        "instance_type": inst.get("InstanceType", ""),
        "state": inst["State"]["Name"],
        "launch_time": str(inst.get("LaunchTime", "")),
        "public_ip": inst.get("PublicIpAddress", "N/A"),
        "private_ip": inst.get("PrivateIpAddress", "N/A"),
        "name": name,
        "availability_zone": inst.get("Placement", {}).get("AvailabilityZone", ""),
        "vpc_id": inst.get("VpcId", ""),
        "subnet_id": inst.get("SubnetId", ""),
        "security_groups": [
            {"id": sg["GroupId"], "name": sg["GroupName"]}
            for sg in inst.get("SecurityGroups", [])
        ],
        "iam_role": inst.get("IamInstanceProfile", {}).get("Arn", "N/A"),
        "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
        "ebs_volumes": [
            {
                "device": bdm["DeviceName"],
                "volume_id": bdm.get("Ebs", {}).get("VolumeId", ""),
                "status": bdm.get("Ebs", {}).get("Status", ""),
            }
            for bdm in inst.get("BlockDeviceMappings", [])
        ],
    }

    logger.info("Described instance %s (state=%s)", instance_id, result["state"])
    return result


async def restart_ec2_instance(instance_id: str) -> dict[str, Any]:
    """Restart (stop then start) an EC2 instance.

    Args:
        instance_id: The instance to restart.

    Returns:
        Dict with the new state transitions, or an error dict.
    """
    ec2 = get_client("ec2")

    # Stop
    logger.info("Stopping instance %s …", instance_id)
    stop_resp = safe_boto_call(ec2.stop_instances, InstanceIds=[instance_id])
    if "error" in stop_resp:
        return {"action": "stop", **stop_resp}

    # Wait for stopped state
    waiter = ec2.get_waiter("instance_stopped")
    try:
        waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": 30})
    except Exception as exc:
        return {"error": True, "message": f"Timeout waiting for stop: {exc}"}

    # Start
    logger.info("Starting instance %s …", instance_id)
    start_resp = safe_boto_call(ec2.start_instances, InstanceIds=[instance_id])
    if "error" in start_resp:
        return {"action": "start", **start_resp}

    logger.info("Instance %s restart initiated", instance_id)
    return {
        "instance_id": instance_id,
        "action": "restart",
        "stop_response": _state_changes(stop_resp),
        "start_response": _state_changes(start_resp),
    }


# ── Helpers ──────────────────────────────────────────────────────────────


def _state_changes(resp: dict[str, Any]) -> list[dict[str, str]]:
    """Extract state transitions from a stop/start response."""
    changes = []
    for sc in resp.get("StoppingInstances", resp.get("StartingInstances", [])):
        changes.append({
            "instance_id": sc["InstanceId"],
            "previous_state": sc["PreviousState"]["Name"],
            "current_state": sc["CurrentState"]["Name"],
        })
    return changes


# ── SSM RunCommand Tools ─────────────────────────────────────────────────────────


async def run_ssm_command(
    instance_id: str,
    command: str,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Run a shell command on an EC2 instance via AWS Systems Manager.

    The instance must have the SSM Agent running and an IAM role with
    ``AmazonSSMManagedInstanceCore`` attached.

    Args:
        instance_id:     Target EC2 instance ID.
        command:         Shell command to execute (e.g. ``top -bn1 | head -20``).
        timeout_seconds: Max wait time for the command to finish.

    Returns:
        Dict with ``status``, ``stdout``, ``stderr``, and ``command_id``.
    """
    ssm = get_client("ssm")

    send_resp = safe_boto_call(
        ssm.send_command,
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]},
        TimeoutSeconds=timeout_seconds,
        Comment=f"DevOps Agent: {command[:80]}",
    )
    if "error" in send_resp:
        return send_resp

    command_id = send_resp["Command"]["CommandId"]
    logger.info("SSM command %s sent to %s: %s", command_id, instance_id, command[:120])

    deadline = time.time() + timeout_seconds
    status = "Pending"
    while time.time() < deadline:
        time.sleep(2)
        inv_resp = safe_boto_call(
            ssm.get_command_invocation,
            CommandId=command_id,
            InstanceId=instance_id,
        )
        if "error" in inv_resp:
            if "InvocationDoesNotExist" in inv_resp.get("message", ""):
                continue
            return inv_resp

        status = inv_resp.get("Status", "Pending")
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            stdout = inv_resp.get("StandardOutputContent", "")
            stderr = inv_resp.get("StandardErrorContent", "")
            logger.info(
                "SSM command %s on %s finished: %s (stdout=%d bytes)",
                command_id, instance_id, status, len(stdout),
            )
            return {
                "command_id": command_id,
                "instance_id": instance_id,
                "status": status,
                "stdout": stdout[:4000],
                "stderr": stderr[:2000],
            }

    return {
        "command_id": command_id,
        "instance_id": instance_id,
        "status": "Timeout",
        "error": True,
        "message": f"Command did not complete within {timeout_seconds}s",
    }


async def diagnose_instance(instance_id: str) -> dict[str, Any]:
    """Run a suite of diagnostic commands on an EC2 instance via SSM.

    Collects: top processes (CPU + memory), disk usage, and active
    connections.  Returns a combined diagnostic report.

    Args:
        instance_id: Target EC2 instance ID.

    Returns:
        Dict with ``diagnostics`` containing output from each check.
    """
    checks = {
        "top_cpu": "ps aux --sort=-%cpu | head -15",
        "top_memory": "ps aux --sort=-%mem | head -15",
        "disk_usage": "df -h",
        "memory_info": "free -h",
        "uptime_load": "uptime",
        "active_connections": "ss -tunap | head -30",
    }

    diagnostics: dict[str, Any] = {}
    for name, cmd in checks.items():
        result = await run_ssm_command(instance_id, cmd, timeout_seconds=30)
        diagnostics[name] = {
            "command": cmd,
            "status": result.get("status", "unknown"),
            "output": result.get("stdout", result.get("message", "")),
            "error": result.get("stderr", ""),
        }

    logger.info("Diagnostics completed for %s (%d checks)", instance_id, len(diagnostics))
    return {"instance_id": instance_id, "diagnostics": diagnostics}


async def remediate_high_cpu(instance_id: str, pid: str) -> dict[str, Any]:
    """Kill a runaway process on an instance by PID.

    Args:
        instance_id: Target EC2 instance ID.
        pid:         Process ID to kill.

    Returns:
        Result of the kill command.
    """
    logger.info("Killing PID %s on %s", pid, instance_id)
    return await run_ssm_command(instance_id, f"sudo kill -9 {pid}", timeout_seconds=15)


async def remediate_disk_full(instance_id: str) -> dict[str, Any]:
    """Clean up common disk space consumers on an instance.

    Removes old logs (>7 days), package caches, and temp files.

    Args:
        instance_id: Target EC2 instance ID.

    Returns:
        Result showing space freed.
    """
    cleanup_script = (
        "echo '=== Before ===' && df -h / && "
        "sudo find /var/log -name '*.gz' -mtime +7 -delete 2>/dev/null; "
        "sudo find /tmp -type f -mtime +2 -delete 2>/dev/null; "
        "sudo apt-get clean 2>/dev/null || sudo yum clean all 2>/dev/null; "
        "sudo journalctl --vacuum-time=3d 2>/dev/null; "
        "echo '=== After ===' && df -h /"
    )
    logger.info("Running disk cleanup on %s", instance_id)
    return await run_ssm_command(instance_id, cleanup_script, timeout_seconds=60)


async def remediate_high_memory(instance_id: str, pid: str) -> dict[str, Any]:
    """Kill a memory-hogging process on an instance by PID.

    Args:
        instance_id: Target EC2 instance ID.
        pid:         Process ID to kill.

    Returns:
        Result of the kill + memory status after.
    """
    script = f"sudo kill -9 {pid} && sleep 2 && free -h"
    logger.info("Killing memory-hog PID %s on %s", pid, instance_id)
    return await run_ssm_command(instance_id, script, timeout_seconds=15)
