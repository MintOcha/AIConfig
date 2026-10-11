#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.0.0",
#   "Pillow>=11,<13",
# ]
# ///
"""Direct entry point for StudioMCP CLI and server."""
import os
from pathlib import Path
import sys

ROOT = Path(os.path.abspath(__file__)).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studiomcp.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
