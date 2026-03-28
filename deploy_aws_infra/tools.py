"""
AWS Infrastructure MCP tools.

Provides EC2 management tools: list, describe, and restart instances.
Each function is designed to be registered as an MCP tool.
"""

from __future__ import annotations

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
