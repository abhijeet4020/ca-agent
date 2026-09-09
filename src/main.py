"""Purpose: top-level entry point required by ARCHITECTURE.md, kept as a thin shim so the
launch path and the console script share one implementation. All behaviour lives in
ca_agent.cli; this file exists only so `python src/main.py` works without an install.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from ca_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
