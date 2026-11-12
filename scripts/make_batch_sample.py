"""Create a CSV in data/batch/input/ for the batch scoring job to pick up.

    python scripts/make_batch_sample.py --rows 50000
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "common"))

from fraudshield_common.data import generate_synthetic  # noqa: E402
from fraudshield_common.features import RAW_COLUMNS  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--rows", type=int, default=50_000)
args = p.parse_args()

out_dir = ROOT / "data" / "batch" / "input"
out_dir.mkdir(parents=True, exist_ok=True)
out = out_dir / f"transactions_{datetime.now():%Y%m%d_%H%M%S}.csv"
generate_synthetic(args.rows, seed=int(datetime.now().timestamp()) % 10_000)[RAW_COLUMNS].to_csv(out, index=False)
print(f"wrote {args.rows:,} rows -> {out}\nThe batch service scores it on its next run (or: make batch-now)")
