# Backward-compat shim — real implementation lives in lib/version.py
# Other scripts in scripts/ may still rom _version import ...
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.version import *  # noqa: F401,F403
