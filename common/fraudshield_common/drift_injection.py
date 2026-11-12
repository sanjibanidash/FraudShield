"""Deliberately distort the live stream so we can watch drift monitoring + alerts fire.

Modes (switch them live from the Flask dashboard -> Controls page):
  none              - replay data unchanged
  amount_inflation  - every amount and balance x3 (e.g. inflation / new high-value customer segment)
  type_shift        - 60% of PAYMENT/CASH_IN become TRANSFER/CASH_OUT (product change)
  balance_shift     - origin balances shrink to 20% (poorer customer base)
"""

from __future__ import annotations

import random

DRIFT_MODES = ["none", "amount_inflation", "type_shift", "balance_shift"]
_MONEY = ["amount", "oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest"]


def apply_drift(tx: dict, mode: str, rng: random.Random | None = None) -> dict:
    rng = rng or random
    if mode in (None, "", "none"):
        return tx
    tx = dict(tx)
    if mode == "amount_inflation":
        for k in _MONEY:
            tx[k] = round(float(tx[k]) * 3.0, 2)
    elif mode == "type_shift":
        if tx["type"] in ("PAYMENT", "CASH_IN") and rng.random() < 0.6:
            tx["type"] = rng.choice(["TRANSFER", "CASH_OUT"])
            if str(tx.get("nameDest", "")).startswith("M"):
                tx["nameDest"] = "C" + str(tx["nameDest"])[1:]
    elif mode == "balance_shift":
        old = float(tx["oldbalanceOrg"]) * 0.2
        tx["oldbalanceOrg"] = round(old, 2)
        tx["newbalanceOrig"] = round(max(old - float(tx["amount"]), 0.0), 2)
    else:
        raise ValueError(f"unknown drift mode {mode!r}; choose from {DRIFT_MODES}")
    return tx
