"""Feature engineering shared by training, the real-time API and the batch job.

Keeping this in ONE place avoids training/serving skew: the API and the batch job
compute exactly the same features the model was trained on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TX_TYPES = ["CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"]

RAW_COLUMNS = [
    "step",
    "type",
    "amount",
    "nameOrig",
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
]
LABEL_COLUMN = "isFraud"

NUMERIC_RAW = ["amount", "oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest"]

FEATURE_COLUMNS = [
    "amount",
    "log_amount",
    "oldbalanceOrg",
    "newbalanceOrig",
    "oldbalanceDest",
    "newbalanceDest",
    "errorBalanceOrig",
    "errorBalanceDest",
    "amount_to_balance",
    "hour",
    "orig_emptied",
    "dest_zero_before",
    "dest_is_merchant",
] + [f"type_{t}" for t in TX_TYPES]

# Columns monitored for data drift. Hour-of-day is deliberately NOT monitored: the stream is replayed in
# time order, so any short window covers only a few hours and would always look "drifted" (false alarms).
DRIFT_NUMERIC = ["amount", "oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest"]
DRIFT_CATEGORICAL = ["type"]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Turn raw PaySim-style transactions into the model feature matrix."""
    out = pd.DataFrame(index=df.index)
    amount = df["amount"].astype(float)
    old_o = df["oldbalanceOrg"].astype(float)
    new_o = df["newbalanceOrig"].astype(float)
    old_d = df["oldbalanceDest"].astype(float)
    new_d = df["newbalanceDest"].astype(float)

    out["amount"] = amount
    out["log_amount"] = np.log1p(amount.clip(lower=0))
    out["oldbalanceOrg"] = old_o
    out["newbalanceOrig"] = new_o
    out["oldbalanceDest"] = old_d
    out["newbalanceDest"] = new_d
    # In a consistent ledger these are ~0. Fraud often leaves the books inconsistent.
    out["errorBalanceOrig"] = new_o + amount - old_o
    out["errorBalanceDest"] = old_d + amount - new_d
    out["amount_to_balance"] = amount / (old_o + 1.0)
    out["hour"] = (df["step"].astype(int) % 24) if "step" in df else 0
    out["orig_emptied"] = ((old_o > 0) & (new_o == 0)).astype(int)
    out["dest_zero_before"] = ((old_d == 0) & (new_d == 0)).astype(int)
    if "nameDest" in df:
        out["dest_is_merchant"] = df["nameDest"].fillna("").astype(str).str.startswith("M").astype(int)
    else:
        out["dest_is_merchant"] = 0
    tx_type = df["type"].astype(str).str.upper()
    for t in TX_TYPES:
        out[f"type_{t}"] = (tx_type == t).astype(int)
    return out[FEATURE_COLUMNS]


def add_hour(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["hour"] = df["step"].astype(int) % 24
    return df
