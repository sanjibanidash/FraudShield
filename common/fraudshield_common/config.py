"""Central configuration. Every value can be overridden with an environment variable."""

from __future__ import annotations

import os
from pathlib import Path


def env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "y", "on"}


# --- Paths -----------------------------------------------------------------
PROJECT_ROOT = Path(env_str("PROJECT_ROOT", str(Path(__file__).resolve().parents[2])))
DATA_DIR = Path(env_str("DATA_DIR", str(PROJECT_ROOT / "data")))
MODEL_DIR = Path(env_str("MODEL_DIR", str(PROJECT_ROOT / "models")))
REPORTS_DIR = Path(env_str("REPORTS_DIR", str(PROJECT_ROOT / "reports")))

# Real PaySim file (download from Kaggle and place it here). If missing -> synthetic data.
RAW_DATA_PATH = Path(env_str("RAW_DATA_PATH", str(DATA_DIR / "raw" / "paysim.csv")))
STREAM_DATA_PATH = Path(env_str("STREAM_DATA_PATH", str(DATA_DIR / "stream" / "stream.csv")))
BATCH_INPUT_DIR = Path(env_str("BATCH_INPUT_DIR", str(DATA_DIR / "batch" / "input")))
BATCH_OUTPUT_DIR = Path(env_str("BATCH_OUTPUT_DIR", str(DATA_DIR / "batch" / "output")))

# --- Training --------------------------------------------------------------
TRAIN_MAX_STEP = env_int("TRAIN_MAX_STEP", 400)  # steps (hours) <= this are history; rest is "future" stream
MAX_LEGIT_TRAIN_ROWS = env_int("MAX_LEGIT_TRAIN_ROWS", 600_000)
SYNTHETIC_ROWS = env_int("SYNTHETIC_ROWS", 300_000)
RANDOM_SEED = env_int("RANDOM_SEED", 42)

# --- Infra -----------------------------------------------------------------
KAFKA_BOOTSTRAP = env_str("KAFKA_BOOTSTRAP", "localhost:29092")
TOPIC_TRANSACTIONS = env_str("TOPIC_TRANSACTIONS", "transactions")
TOPIC_ALERTS = env_str("TOPIC_ALERTS", "fraud-alerts")
DATABASE_URL = env_str("DATABASE_URL", "postgresql://fraud:fraud@localhost:5432/fraudshield")
API_URL = env_str("API_URL", "http://localhost:8000")

# --- Drift -----------------------------------------------------------------
PSI_THRESHOLD = env_float("PSI_THRESHOLD", 0.2)
DATASET_DRIFT_SHARE = env_float("DATASET_DRIFT_SHARE", 0.25)
SEVERE_PSI = env_float("SEVERE_PSI", 0.5)
