"""
Docker build and push helpers.
"""

from __future__ import annotations

from pathlib import Path

from .config import PLATFORM, ecr_uri
from .console import log, run, Colors


def build_image(ecr_repo: str, tag: str, build_dir: Path, no_cache: bool = False) -> bool:
    """Build a Docker image for the given ECR repo and tag.

    Returns True on success.
    """
    uri = ecr_uri(ecr_repo)
    full_tag = f"{uri}:{tag}"

    log(f"\n  Building image...", Colors.BOLD)
    cmd = [
        "docker", "build",
        "--platform", PLATFORM,
        "-t", full_tag,
        "-t", f"{uri}:latest",
    ]
    if no_cache:
        cmd.append("--no-cache")
        log("  (--no-cache: forcing full rebuild)")
    cmd.append(str(build_dir))

    result = run(cmd)
    if result.returncode != 0:
        log(f"  Build FAILED", Colors.RED)
        return False
    log(f"  Built: {full_tag}", Colors.GREEN)
    return True


def push_image(ecr_repo: str, tag: str) -> bool:
    """Push tagged + latest images to ECR.

    Returns True on success.
    """
    uri = ecr_uri(ecr_repo)
    full_tag = f"{uri}:{tag}"

    log(f"\n  Pushing image...", Colors.BOLD)
    r1 = run(["docker", "push", full_tag])
    r2 = run(["docker", "push", f"{uri}:latest"])
    if r1.returncode != 0 or r2.returncode != 0:
        log(f"  Push FAILED", Colors.RED)
        return False
    log(f"  Pushed: {full_tag}", Colors.GREEN)
    return True
