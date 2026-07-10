"""Verification RE-RUN of the adaptive give-back experiment.

Imports bt_exp_adaptive_giveback unchanged and re-executes it end to end
(fresh yfinance pull, fresh entry collection, all 44 policies), but writes to
bt_exp_adaptive_giveback_rerun.json so the earlier output file is never
touched. New files only; nothing in reports/ is written.

Usage:
    python bt_exp_adaptive_giveback_rerun.py
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bt_exp_adaptive_giveback as exp

exp.OUT_JSON = Path(__file__).parent / "bt_exp_adaptive_giveback_rerun.json"

if __name__ == "__main__":
    exp.main()
