"""
Auto-versioning for AgentCore deployments.

Version scheme:  v{N}-{git_sha[:7]}
  - N auto-increments from the highest existing vN-* tag in the ECR repo
  - git SHA provides commit traceability

Usage:
    from lib.version import next_version, preflight_checks
    tag = next_version("bedrock_agentcore-mcp_server")  # -> "v4-a3b4c5d"
    preflight_checks()  # warns on dirty tree, verifies Docker
"""

from __future__ import annotations

import re
import subprocess
import sys

import boto3

from .config import REGION


# ── Git helpers ──────────────────────────────────────────────────────────

def git_sha() -> str:
    """Return the short (7-char) SHA of HEAD."""
    result = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def git_is_clean() -> bool:
    """Return True if the working tree has no uncommitted changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def git_branch() -> str:
    """Return the current branch name."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


# ── ECR version query ────────────────────────────────────────────────────

def _highest_version(ecr_repo: str) -> int:
    """Query ECR for the highest v{N} tag number. Returns 0 if none found."""
    ecr = boto3.client("ecr", region_name=REGION)
    try:
        paginator = ecr.get_paginator("describe_images")
        version_pattern = re.compile(r"^v(\d+)")
        highest = 0

        for page in paginator.paginate(
            repositoryName=ecr_repo,
            filter={"tagStatus": "TAGGED"},
        ):
            for image in page.get("imageDetails", []):
                for tag in image.get("imageTags", []):
                    m = version_pattern.match(tag)
                    if m:
                        highest = max(highest, int(m.group(1)))
        return highest
    except ecr.exceptions.RepositoryNotFoundException:
        return 0
    except Exception:
        return 0


def next_version(ecr_repo: str) -> str:
    """Compute the next version tag: v{N+1}-{git_sha}."""
    current = _highest_version(ecr_repo)
    sha = git_sha()
    return f"v{current + 1}-{sha}"


def current_version(ecr_repo: str) -> str | None:
    """Return the highest version tag string, or None if no versions exist."""
    ecr = boto3.client("ecr", region_name=REGION)
    try:
        paginator = ecr.get_paginator("describe_images")
        version_pattern = re.compile(r"^v(\d+)")
        best_n = 0
        best_tag = None

        for page in paginator.paginate(
            repositoryName=ecr_repo,
            filter={"tagStatus": "TAGGED"},
        ):
            for image in page.get("imageDetails", []):
                for tag in image.get("imageTags", []):
                    m = version_pattern.match(tag)
                    if m and int(m.group(1)) > best_n:
                        best_n = int(m.group(1))
                        best_tag = tag
        return best_tag
    except Exception:
        return None


# ── Pre-flight checks ───────────────────────────────────────────────────

def preflight_checks() -> None:
    """Run pre-deploy checks. Warns on issues, exits on blockers."""
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: Docker is not running. Start Docker Desktop first.")
        sys.exit(1)

    if not git_is_clean():
        print(
            "WARNING: Uncommitted changes detected. "
            "The deploy will use the current HEAD SHA, "
            "but your local changes won't match the tagged version."
        )
        print(f"  HEAD:   {git_sha()}")
        print(f"  Branch: {git_branch()}")
        print()
