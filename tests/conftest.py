import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "common"))
os.environ.setdefault("LOG_FORMAT", "text")

from fraudshield_common.data import generate_synthetic  # noqa: E402
from fraudshield_common.training import train_model  # noqa: E402


@pytest.fixture(scope="session")
def synthetic_df():
    return generate_synthetic(60_000, fraud_rate=0.01, seed=1)


@pytest.fixture(scope="session")
def trained(synthetic_df):
    artifact, metadata = train_model(synthetic_df, seed=1)
    return artifact, metadata


@pytest.fixture()
def model_dir(tmp_path, trained):
    from fraudshield_common.model_store import save_model

    artifact, metadata = trained
    save_model(tmp_path, artifact, {**metadata, "trigger": "test", "data_source": "synthetic"})
    return tmp_path
