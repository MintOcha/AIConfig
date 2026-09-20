#!/usr/bin/env python3
"""Compatibility wrapper delegating to install.py --models-only."""
import subprocess
import sys
from pathlib import Path

script = Path(__file__).resolve().parent / "install.py"
cmd = [sys.executable, str(script), "--models-only"] + sys.argv[1:]
raise SystemExit(subprocess.call(cmd))
