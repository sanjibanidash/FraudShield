"""A tiny file-based model registry.

models/
  model_<version>.joblib      the artifact: {"model", "features", "threshold"}
  metadata_<version>.json     metrics, training info
  current.json                {"version": "..."}  <- pointer to the champion model
  reference.csv               reference data used for drift detection
  train_snapshot.csv.gz       training data snapshot used for retraining
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .features import FEATURE_COLUMNS, build_features


def new_version() -> str:
    return datetime.now(UTC).strftime("v%Y%m%d-%H%M%S")


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def save_model(model_dir: Path, artifact: dict, metadata: dict, make_current: bool = True) -> str:
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    version = metadata.get("version") or new_version()
    metadata = {**metadata, "version": version}
    joblib.dump(artifact, model_dir / f"model_{version}.joblib")
    os.chmod(model_dir / f"model_{version}.joblib", 0o644)
    _atomic_write_text(model_dir / f"metadata_{version}.json", json.dumps(metadata, indent=2, default=str))
    if make_current:
        set_current(model_dir, version)
    return version


def set_current(model_dir: Path, version: str) -> None:
    _atomic_write_text(Path(model_dir) / "current.json", json.dumps({"version": version}))


def current_version(model_dir: Path) -> str | None:
    p = Path(model_dir) / "current.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())["version"]


def load_model(model_dir: Path, version: str | None = None) -> tuple[dict, dict]:
    model_dir = Path(model_dir)
    version = version or current_version(model_dir)
    if version is None:
        raise FileNotFoundError(f"no model registered in {model_dir} (run training first)")
    artifact = joblib.load(model_dir / f"model_{version}.joblib")
    metadata = json.loads((model_dir / f"metadata_{version}.json").read_text())
    return artifact, metadata


def list_versions(model_dir: Path) -> list[dict]:
    out = []
    for p in sorted(Path(model_dir).glob("metadata_*.json")):
        out.append(json.loads(p.read_text()))
    return out


def predict_proba(artifact: dict, raw: pd.DataFrame) -> np.ndarray:
    X = build_features(raw)[artifact.get("features", FEATURE_COLUMNS)]
    return artifact["model"].predict_proba(X)[:, 1]


def score(artifact: dict, raw: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    proba = predict_proba(artifact, raw)
    return proba, proba >= artifact["threshold"]
