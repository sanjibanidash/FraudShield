"""Data loading: the real PaySim dataset, or a synthetic PaySim-like generator.

PaySim (Kaggle: "Synthetic Financial Datasets For Fraud Detection") has these columns:
step,type,amount,nameOrig,oldbalanceOrg,newbalanceOrig,nameDest,oldbalanceDest,newbalanceDest,isFraud,isFlaggedFraud
`step` = 1 hour of simulated time (743 steps = 31 days).

The synthetic generator reproduces the same schema and the main fraud patterns so the whole
project runs end-to-end with zero downloads (handy for CI and first runs).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .features import RAW_COLUMNS, TX_TYPES

log = logging.getLogger(__name__)

PAYSIM_COLUMNS = RAW_COLUMNS + ["isFraud", "isFlaggedFraud"]
N_STEPS = 743

_TYPE_PROBS = {"CASH_OUT": 0.35, "PAYMENT": 0.34, "CASH_IN": 0.22, "TRANSFER": 0.084, "DEBIT": 0.006}
# lognormal (mu, sigma) of the amount per transaction type
_AMOUNT_PARAMS = {
    "CASH_OUT": (11.5, 1.0),
    "PAYMENT": (8.8, 1.0),
    "CASH_IN": (11.3, 1.1),
    "TRANSFER": (12.4, 1.2),
    "DEBIT": (8.5, 1.0),
}
# activity by hour of day: quiet nights, busy days
_HOUR_WEIGHTS = np.array([0.2, 0.1, 0.05, 0.05, 0.05, 0.1, 0.3, 0.6, 0.9, 1.0, 1.0, 1.0,
                          1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.9, 0.8, 0.6, 0.5, 0.3])


def _ids(rng: np.random.Generator, prefix: str, n: int) -> np.ndarray:
    return np.char.add(prefix, rng.integers(10**8, 10**10, size=n).astype(str))


def generate_synthetic(n_rows: int = 300_000, fraud_rate: float = 0.005, seed: int = 42) -> pd.DataFrame:
    """Generate a PaySim-like dataset (vectorised, ~1s for 300k rows)."""
    rng = np.random.default_rng(seed)
    n_fraud = max(int(n_rows * fraud_rate), 10)
    n_legit = n_rows - n_fraud

    # ---------------- legitimate transactions ----------------
    step_probs = np.tile(_HOUR_WEIGHTS, N_STEPS // 24 + 1)[:N_STEPS]
    step_probs = step_probs / step_probs.sum()
    steps = rng.choice(np.arange(1, N_STEPS + 1), size=n_legit, p=step_probs)
    types = rng.choice(list(_TYPE_PROBS), size=n_legit, p=list(_TYPE_PROBS.values()))

    amount = np.empty(n_legit)
    for t, (mu, sigma) in _AMOUNT_PARAMS.items():
        m = types == t
        amount[m] = rng.lognormal(mu, sigma, m.sum())
    amount = np.round(np.minimum(amount, 10_000_000), 2)

    old_o = np.where(rng.random(n_legit) < 0.33, 0.0, np.round(rng.lognormal(10.5, 2.2, n_legit), 2))
    is_credit = types == "CASH_IN"
    new_o = np.where(is_credit, old_o + amount, np.maximum(old_o - amount, 0.0))
    # a few legit customers also empty their account (makes the problem non-trivial)
    empties = (~is_credit) & (rng.random(n_legit) < 0.03) & (old_o > 0)
    amount = np.where(empties, old_o, amount)
    new_o = np.where(empties, 0.0, new_o)

    is_payment = types == "PAYMENT"
    old_d = np.where(is_payment | (rng.random(n_legit) < 0.35), 0.0,
                     np.round(rng.lognormal(11.5, 2.3, n_legit), 2))
    new_d = np.where(is_payment, 0.0,
                     np.where(is_credit, np.maximum(old_d - amount, 0.0), old_d + amount))
    # PaySim quirk: destination balances are sometimes not updated
    stale = (~is_payment) & (rng.random(n_legit) < 0.12)
    new_d = np.where(stale, old_d, new_d)

    name_dest = np.where(is_payment, _ids(rng, "M", n_legit), _ids(rng, "C", n_legit))
    legit = pd.DataFrame({
        "step": steps, "type": types, "amount": amount,
        "nameOrig": _ids(rng, "C", n_legit), "oldbalanceOrg": old_o, "newbalanceOrig": np.round(new_o, 2),
        "nameDest": name_dest, "oldbalanceDest": old_d, "newbalanceDest": np.round(new_d, 2),
        "isFraud": 0,
    })

    # ---------------- fraudulent transactions ----------------
    f_steps = rng.integers(1, N_STEPS + 1, size=n_fraud)  # fraud happens at any hour, incl. night
    f_types = rng.choice(["TRANSFER", "CASH_OUT"], size=n_fraud)
    f_old_o = np.round(rng.lognormal(12.3, 1.6, n_fraud), 2)
    partial = rng.random(n_fraud) < 0.2
    f_amount = np.where(partial, f_old_o * rng.uniform(0.4, 0.95, n_fraud), f_old_o)
    f_amount = np.round(np.minimum(f_amount, 10_000_000), 2)
    f_new_o = np.round(np.maximum(f_old_o - f_amount, 0.0), 2)
    untouched_dest = rng.random(n_fraud) < 0.55  # mule accounts with balances never updated
    f_old_d = np.where(untouched_dest, 0.0, np.round(rng.lognormal(11.0, 2.0, n_fraud), 2))
    f_new_d = np.where(untouched_dest, 0.0, f_old_d + f_amount)
    fraud = pd.DataFrame({
        "step": f_steps, "type": f_types, "amount": f_amount,
        "nameOrig": _ids(rng, "C", n_fraud), "oldbalanceOrg": f_old_o, "newbalanceOrig": f_new_o,
        "nameDest": _ids(rng, "C", n_fraud), "oldbalanceDest": f_old_d, "newbalanceDest": np.round(f_new_d, 2),
        "isFraud": 1,
    })

    df = pd.concat([legit, fraud], ignore_index=True)
    df["isFlaggedFraud"] = ((df["type"] == "TRANSFER") & (df["amount"] > 200_000)).astype(int)
    df = df.sort_values("step", kind="stable").reset_index(drop=True)
    return df[PAYSIM_COLUMNS]


def load_dataset(path: Path, synthetic_rows: int = 300_000, seed: int = 42) -> tuple[pd.DataFrame, str]:
    """Load the real PaySim CSV if present, otherwise generate (and cache) synthetic data."""
    path = Path(path)
    if path.exists():
        log.info("loading real dataset", extra={"path": str(path)})
        dtypes = {"type": "category", "nameOrig": "string", "nameDest": "string"}
        df = pd.read_csv(path, dtype=dtypes)
        df["type"] = df["type"].astype(str)
        missing = set(PAYSIM_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"{path} is missing PaySim columns: {sorted(missing)}")
        return df[PAYSIM_COLUMNS], "paysim"

    log.warning("real dataset not found - generating synthetic PaySim-like data",
                extra={"expected_path": str(path), "rows": synthetic_rows})
    return generate_synthetic(synthetic_rows, seed=seed), "synthetic"


def time_split(df: pd.DataFrame, max_step: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """History (step <= max_step) for training, future (step > max_step) for the live stream."""
    history = df[df["step"] <= max_step].reset_index(drop=True)
    future = df[df["step"] > max_step].reset_index(drop=True)
    return history, future


def validate_types(df: pd.DataFrame) -> pd.DataFrame:
    unknown = set(df["type"].unique()) - set(TX_TYPES)
    if unknown:
        raise ValueError(f"unknown transaction types: {unknown}")
    return df
