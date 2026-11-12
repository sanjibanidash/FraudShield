"""Initial (offline / batch) training.

    python training/train.py               # train (uses data/raw/paysim.csv or synthetic data)
    python training/train.py --if-missing  # only train if no model is registered yet (used by docker compose)

Outputs (in MODEL_DIR): versioned model + metadata, current.json, reference.csv, train_snapshot.csv.gz
Also writes the "future" part of the data to STREAM_DATA_PATH for the Kafka producer to replay.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from fraudshield_common import config  # noqa: E402
from fraudshield_common.data import load_dataset, time_split  # noqa: E402
from fraudshield_common.features import add_hour  # noqa: E402
from fraudshield_common.logging_setup import setup_logging  # noqa: E402
from fraudshield_common.model_store import current_version, save_model  # noqa: E402
from fraudshield_common.training import downsample_legit, train_model  # noqa: E402

log = setup_logging("trainer")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=config.RAW_DATA_PATH)
    p.add_argument("--model-dir", type=Path, default=config.MODEL_DIR)
    p.add_argument("--stream-out", type=Path, default=config.STREAM_DATA_PATH)
    p.add_argument("--max-step", type=int, default=config.TRAIN_MAX_STEP)
    p.add_argument("--max-legit", type=int, default=config.MAX_LEGIT_TRAIN_ROWS)
    p.add_argument("--synthetic-rows", type=int, default=config.SYNTHETIC_ROWS)
    p.add_argument("--if-missing", action="store_true", help="skip if a model already exists")
    args = p.parse_args()

    if args.if_missing and current_version(args.model_dir) and args.stream_out.exists():
        log.info("model already registered - skipping training", extra={"version": current_version(args.model_dir)})
        return 0

    df, source = load_dataset(args.data, synthetic_rows=args.synthetic_rows, seed=config.RANDOM_SEED)
    history, future = time_split(df, args.max_step)
    log.info("dataset split", extra={"source": source, "history_rows": len(history), "stream_rows": len(future),
                                     "history_fraud": int(history["isFraud"].sum())})

    history = downsample_legit(history, args.max_legit, seed=config.RANDOM_SEED)
    artifact, metadata = train_model(history, seed=config.RANDOM_SEED)
    metadata.update({"data_source": source, "trigger": "initial"})

    args.model_dir.mkdir(parents=True, exist_ok=True)
    # Reference data for drift monitoring = what the model saw in training (legit + fraud mix)
    reference = add_hour(history.sample(min(20_000, len(history)), random_state=1))
    reference.to_csv(args.model_dir / "reference.csv", index=False)
    # Snapshot for retraining later (kept small so the free VM is happy)
    snapshot = downsample_legit(history, 150_000, seed=config.RANDOM_SEED)
    snapshot.to_csv(args.model_dir / "train_snapshot.csv.gz", index=False)

    version = save_model(args.model_dir, artifact, metadata)

    args.stream_out.parent.mkdir(parents=True, exist_ok=True)
    future.to_csv(args.stream_out, index=False)

    log.info("training complete", extra={"version": version, "stream_file": str(args.stream_out)})
    print(json.dumps({"version": version, **metadata["metrics"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
