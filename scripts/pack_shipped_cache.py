#!/usr/bin/env python3
"""CLI entry for packing/extracting shippable cache zips.

Usage (from repo root):
    ./.venv/bin/python scripts/pack_shipped_cache.py pack
    ./.venv/bin/python scripts/pack_shipped_cache.py extract
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cache_ship import main

if __name__ == "__main__":
    raise SystemExit(main())
