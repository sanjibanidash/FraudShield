"""Data-drift detection with PSI (Population Stability Index) + KS test.

PSI rule of thumb:  < 0.1 no change | 0.1-0.2 moderate | > 0.2 significant drift.
We flag a feature as drifted when PSI > threshold, and the whole dataset as drifted when
the share of drifted features >= DATASET_DRIFT_SHARE (same idea as Evidently's DataDriftPreset)
OR any single feature drifted severely (PSI >= SEVERE_PSI) - e.g. a total shift in transaction types.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

EPS = 1e-6


def psi_numeric(reference: pd.Series, current: pd.Series, bins: int = 10) -> float:
    ref = reference.dropna().astype(float).to_numpy()
    cur = current.dropna().astype(float).to_numpy()
    if len(ref) == 0 or len(cur) == 0:
        return 0.0
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:  # constant column
        return 0.0 if np.allclose(cur, edges[0]) else 1.0
    edges[0], edges[-1] = -np.inf, np.inf
    ref_pct = np.histogram(ref, edges)[0] / len(ref)
    cur_pct = np.histogram(cur, edges)[0] / len(cur)
    ref_pct, cur_pct = np.clip(ref_pct, EPS, None), np.clip(cur_pct, EPS, None)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def psi_categorical(reference: pd.Series, current: pd.Series) -> float:
    cats = sorted(set(reference.dropna().unique()) | set(current.dropna().unique()))
    ref_pct = reference.value_counts(normalize=True).reindex(cats, fill_value=0).to_numpy()
    cur_pct = current.value_counts(normalize=True).reindex(cats, fill_value=0).to_numpy()
    ref_pct, cur_pct = np.clip(ref_pct, EPS, None), np.clip(cur_pct, EPS, None)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def compute_drift(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    numeric_cols: list[str],
    categorical_cols: list[str],
    psi_threshold: float = 0.2,
    dataset_drift_share: float = 0.25,
    severe_psi: float = 0.5,
) -> dict:
    features: dict[str, dict] = {}
    for col in numeric_cols:
        psi = psi_numeric(reference[col], current[col])
        ks = stats.ks_2samp(reference[col].dropna(), current[col].dropna())
        features[col] = {
            "kind": "numeric",
            "psi": round(psi, 4),
            "ks_stat": round(float(ks.statistic), 4),
            "ks_pvalue": float(ks.pvalue),
            "ref_mean": float(reference[col].mean()),
            "cur_mean": float(current[col].mean()),
            "drifted": psi > psi_threshold,
        }
    for col in categorical_cols:
        psi = psi_categorical(reference[col], current[col])
        features[col] = {
            "kind": "categorical",
            "psi": round(psi, 4),
            "ref_dist": reference[col].value_counts(normalize=True).round(4).to_dict(),
            "cur_dist": current[col].value_counts(normalize=True).round(4).to_dict(),
            "drifted": psi > psi_threshold,
        }
    n_drifted = sum(f["drifted"] for f in features.values())
    share = n_drifted / max(len(features), 1)
    max_psi = max((f["psi"] for f in features.values()), default=0.0)
    return {
        "n_features": len(features),
        "n_drifted": n_drifted,
        "share_drifted": round(share, 4),
        "dataset_drift": bool(share >= dataset_drift_share or max_psi >= severe_psi),
        "max_psi": max_psi,
        "drifted_features": [k for k, v in features.items() if v["drifted"]],
        "features": features,
        "reference_rows": len(reference),
        "current_rows": len(current),
    }
