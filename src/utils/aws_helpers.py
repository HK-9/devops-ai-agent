"""
Shared AWS helper utilities.

Provides a centralised boto3 session/client factory, retry helpers,
and structured JSON logging setup used throughout the project.
"""

from __future__ import annotations

import json
import logging
import sys
from functools import cache
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from src.agent.config import settings

# ── Logging ──────────────────────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Emit structured JSON log lines for CloudWatch Logs."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


def setup_logging(name: str = "devops-agent") -> logging.Logger:
    """Configure and return the root application logger."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        if settings.log_format == "json":
            handler.setFormatter(JSONFormatter())
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s")
            )
        logger.addHandler(handler)

    return logger


# ── Boto3 helpers ────────────────────────────────────────────────────────

_RETRY_CONFIG = BotoConfig(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=settings.tool_timeout_seconds,
)


@cache
def get_boto_session() -> boto3.Session:
    """Return a cached boto3 session honouring the configured profile/region."""
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.aws_profile:
        kwargs["profile_name"] = settings.aws_profile
    return boto3.Session(**kwargs)


def get_client(service: str) -> Any:
    """Return a boto3 client with built-in retry config.

    Usage:
        ec2 = get_client("ec2")
        cw  = get_client("cloudwatch")
    """
    return get_boto_session().client(service, config=_RETRY_CONFIG)  # type: ignore[call-overload]


def safe_boto_call(func: Any, **kwargs: Any) -> dict[str, Any]:
    """Invoke a boto3 method and return its response, or a structured error dict.

    Example:
        result = safe_boto_call(ec2.describe_instances, InstanceIds=["i-abc123"])
    """
    try:
        return func(**kwargs)  # type: ignore[no-any-return]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"]["Message"]
        return {"error": True, "code": code, "message": message}
