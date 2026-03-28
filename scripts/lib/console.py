"""
Console output helpers — colors and logging.
"""

from __future__ import annotations

import subprocess
import sys


class Colors:
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


def log(msg: str, color: str = "") -> None:
    """Print a message with optional ANSI color."""
    print(f"{color}{msg}{Colors.RESET}" if color else msg)


def run(cmd: list[str], check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess command and log it.

    Args:
        cmd: Command and arguments.
        check: If True, exit on non-zero return code (deploy_agent behaviour).
               If False, return the result for caller to inspect.
        **kwargs: Passed through to subprocess.run.
    """
    log(f"  $ {' '.join(cmd)}", Colors.CYAN)
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        log(f"  ERROR: exit code {result.returncode}", Colors.RED)
        if check:
            sys.exit(result.returncode)
    return result
