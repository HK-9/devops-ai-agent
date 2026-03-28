"""
CloudWatch Monitoring MCP tools.

Provides metrics retrieval tools for CPU, memory, and disk usage.
Each function is designed to be registered as an MCP tool.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aws_helpers import get_client, safe_boto_call, setup_logging

logger = setup_logging("mcp.monitoring")


# ── Tool Implementations ────────────────────────────────────────────────


async def get_cpu_metrics(
    instance_id: str,
    period: int = 300,
    minutes: int = 60,
) -> dict[str, Any]:
    """Get CPU utilization metrics for a single EC2 instance."""
    cw = get_client("cloudwatch")
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)

    response = safe_boto_call(
        cw.get_metric_statistics,
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Average", "Maximum", "Minimum"],
    )
    if "error" in response:
        return response

    datapoints = sorted(response.get("Datapoints", []), key=lambda d: d["Timestamp"])
    formatted = [
        {
            "timestamp": dp["Timestamp"].isoformat(),
            "average": round(dp["Average"], 2),
            "maximum": round(dp["Maximum"], 2),
            "minimum": round(dp["Minimum"], 2),
        }
        for dp in datapoints
    ]

    summary = _summarize(formatted)
    logger.info("CPU metrics for %s: avg=%.1f%%, peak=%.1f%%", instance_id, summary["average"], summary["peak"])

    return {
        "instance_id": instance_id,
        "metric": "CPUUtilization",
        "period_seconds": period,
        "time_range_minutes": minutes,
        "datapoints": formatted,
        "summary": summary,
    }


async def get_cpu_metrics_for_instances(
    instance_ids: list[str],
    period: int = 300,
    minutes: int = 60,
) -> dict[str, Any]:
    """Batch CPU metrics for multiple instances."""
    results: dict[str, Any] = {}
    for iid in instance_ids:
        results[iid] = await get_cpu_metrics(iid, period=period, minutes=minutes)

    logger.info("Batch CPU metrics for %d instances", len(instance_ids))
    return {"results": results, "instance_count": len(instance_ids)}


async def get_memory_metrics(
    instance_id: str,
    period: int = 300,
    minutes: int = 60,
) -> dict[str, Any]:
    """Get memory utilization metrics (requires CloudWatch Agent)."""
    cw = get_client("cloudwatch")
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)

    response = safe_boto_call(
        cw.get_metric_statistics,
        Namespace="CWAgent",
        MetricName="mem_used_percent",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Average", "Maximum"],
    )
    if "error" in response:
        return response

    datapoints = sorted(response.get("Datapoints", []), key=lambda d: d["Timestamp"])
    if not datapoints:
        return {
            "instance_id": instance_id,
            "metric": "mem_used_percent",
            "datapoints": [],
            "note": "No data — ensure CloudWatch Agent is installed and publishing mem_used_percent.",
        }

    formatted = [
        {
            "timestamp": dp["Timestamp"].isoformat(),
            "average": round(dp["Average"], 2),
            "maximum": round(dp["Maximum"], 2),
        }
        for dp in datapoints
    ]

    return {
        "instance_id": instance_id,
        "metric": "mem_used_percent",
        "datapoints": formatted,
        "summary": _summarize(formatted),
    }


async def get_disk_usage(
    instance_id: str,
    mount_path: str = "/",
    period: int = 300,
    minutes: int = 60,
) -> dict[str, Any]:
    """Get disk usage metrics (requires CloudWatch Agent)."""
    cw = get_client("cloudwatch")
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)

    response = safe_boto_call(
        cw.get_metric_statistics,
        Namespace="CWAgent",
        MetricName="disk_used_percent",
        Dimensions=[
            {"Name": "InstanceId", "Value": instance_id},
            {"Name": "path", "Value": mount_path},
        ],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Average", "Maximum"],
    )
    if "error" in response:
        return response

    datapoints = sorted(response.get("Datapoints", []), key=lambda d: d["Timestamp"])
    if not datapoints:
        return {
            "instance_id": instance_id,
            "metric": "disk_used_percent",
            "mount_path": mount_path,
            "datapoints": [],
            "note": "No data — ensure CloudWatch Agent is publishing disk_used_percent.",
        }

    formatted = [
        {
            "timestamp": dp["Timestamp"].isoformat(),
            "average": round(dp["Average"], 2),
            "maximum": round(dp["Maximum"], 2),
        }
        for dp in datapoints
    ]

    return {
        "instance_id": instance_id,
        "metric": "disk_used_percent",
        "mount_path": mount_path,
        "datapoints": formatted,
        "summary": _summarize(formatted),
    }


# ── Helpers ──────────────────────────────────────────────────────────────


def _summarize(datapoints: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute a quick summary from formatted datapoint dicts."""
    if not datapoints:
        return {"average": 0.0, "peak": 0.0, "latest": 0.0, "datapoint_count": 0}

    averages = [dp["average"] for dp in datapoints]
    peaks = [dp.get("maximum", dp["average"]) for dp in datapoints]

    return {
        "average": round(sum(averages) / len(averages), 2),
        "peak": round(max(peaks), 2),
        "latest": averages[-1],
        "datapoint_count": len(datapoints),
    }
