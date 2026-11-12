"""Model training + evaluation, reused by the initial trainer and the batch retraining job."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)

from .features import FEATURE_COLUMNS, LABEL_COLUMN, build_features

log = logging.getLogger(__name__)


def make_model(seed: int = 42) -> HistGradientBoostingClassifier:
    # Gradient boosted trees (LightGBM-style histogram algorithm, built into scikit-learn).
    # class_weight="balanced" handles the heavy class imbalance (fraud << 1%).
    return HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.08,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        class_weight="balanced",
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=20,
        random_state=seed,
    )


def best_threshold(y_true: np.ndarray, proba: np.ndarray, min_precision: float = 0.5) -> float:
    """Pick the threshold with max F1 among thresholds keeping precision >= min_precision."""
    precision, recall, thresholds = precision_recall_curve(y_true, proba)
    precision, recall = precision[:-1], recall[:-1]
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-9, None)
    ok = precision >= min_precision
    if ok.any():
        f1 = np.where(ok, f1, -1)
    return float(thresholds[int(np.argmax(f1))]) if len(thresholds) else 0.5


def evaluate(y_true: np.ndarray, proba: np.ndarray, threshold: float) -> dict:
    y_pred = (proba >= threshold).astype(int)
    has_both = len(np.unique(y_true)) > 1
    return {
        "pr_auc": float(average_precision_score(y_true, proba)) if has_both else None,
        "roc_auc": float(roc_auc_score(y_true, proba)) if has_both else None,
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "threshold": float(threshold),
        "n": int(len(y_true)),
        "n_fraud": int(np.sum(y_true)),
    }


def downsample_legit(df: pd.DataFrame, max_legit: int, seed: int = 42) -> pd.DataFrame:
    fraud = df[df[LABEL_COLUMN] == 1]
    legit = df[df[LABEL_COLUMN] == 0]
    if len(legit) > max_legit:
        legit = legit.sample(max_legit, random_state=seed)
    return pd.concat([fraud, legit]).sort_values("step", kind="stable").reset_index(drop=True)


def train_model(history: pd.DataFrame, seed: int = 42, val_fraction: float = 0.15) -> tuple[dict, dict]:
    """Time-based split: train on older steps, tune threshold + evaluate on the most recent steps."""
    history = history.sort_values("step", kind="stable").reset_index(drop=True)
    cut_step = history["step"].quantile(1 - val_fraction)
    train = history[history["step"] <= cut_step]
    val = history[history["step"] > cut_step]
    if val[LABEL_COLUMN].sum() < 5:  # tiny datasets: fall back to a random split
        val = history.sample(frac=val_fraction, random_state=seed)
        train = history.drop(val.index)

    model = make_model(seed)
    model.fit(build_features(train), train[LABEL_COLUMN].astype(int))

    val_proba = model.predict_proba(build_features(val))[:, 1]
    threshold = best_threshold(val[LABEL_COLUMN].to_numpy(), val_proba)
    metrics = evaluate(val[LABEL_COLUMN].to_numpy(), val_proba, threshold)

    artifact = {"model": model, "features": FEATURE_COLUMNS, "threshold": threshold}
    metadata = {
        "trained_at": datetime.now(UTC).isoformat(),
        "algorithm": "HistGradientBoostingClassifier",
        "n_train": int(len(train)),
        "n_val": int(len(val)),
        "train_fraud_rate": float(train[LABEL_COLUMN].mean()),
        "metrics": metrics,
    }
    log.info("model trained", extra={"metrics": metrics})
    return artifact, metadata
