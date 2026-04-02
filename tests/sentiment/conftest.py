"""Add the sentiment service directory to sys.path for imports."""

from __future__ import annotations

import sys
from pathlib import Path

SVC_DIR = Path(__file__).parent.parent.parent / "services" / "sentiment"
if str(SVC_DIR) not in sys.path:
    sys.path.insert(0, str(SVC_DIR))
