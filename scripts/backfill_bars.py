"""D7/P3 validation-bar backfill. Dry-run unless ``--write``.

Writes only under ``data/backfill/``. See ``docs/backfill.md``.

    python scripts/backfill_bars.py --input broker.csv --symbol TX00
    python scripts/backfill_bars.py --input broker.csv --symbol TX00 --write
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evolution.backfill import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
