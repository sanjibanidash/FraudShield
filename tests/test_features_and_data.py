import pandas as pd
import pytest

from fraudshield_common.data import PAYSIM_COLUMNS, generate_synthetic, time_split
from fraudshield_common.drift_injection import DRIFT_MODES, apply_drift
from fraudshield_common.features import FEATURE_COLUMNS, build_features


def test_synthetic_schema_matches_paysim(synthetic_df):
    assert list(synthetic_df.columns) == PAYSIM_COLUMNS
    assert synthetic_df["step"].between(1, 743).all()
    assert (synthetic_df["amount"] >= 0).all()


def test_fraud_only_in_transfer_and_cash_out(synthetic_df):
    fraud_types = set(synthetic_df.loc[synthetic_df["isFraud"] == 1, "type"])
    assert fraud_types <= {"TRANSFER", "CASH_OUT"}
    assert 0.005 < synthetic_df["isFraud"].mean() < 0.02


def test_synthetic_is_deterministic():
    a, b = generate_synthetic(1000, seed=3), generate_synthetic(1000, seed=3)
    pd.testing.assert_frame_equal(a, b)


def test_time_split_has_no_leakage(synthetic_df):
    hist, fut = time_split(synthetic_df, 400)
    assert hist["step"].max() <= 400 < fut["step"].min()
    assert len(hist) + len(fut) == len(synthetic_df)


def test_build_features():
    tx = pd.DataFrame([{"step": 25, "type": "TRANSFER", "amount": 100.0, "oldbalanceOrg": 100.0,
                        "newbalanceOrig": 0.0, "nameDest": "C1", "oldbalanceDest": 0.0, "newbalanceDest": 0.0}])
    f = build_features(tx)
    assert list(f.columns) == FEATURE_COLUMNS
    row = f.iloc[0]
    assert row["hour"] == 1
    assert row["errorBalanceOrig"] == 0          # 0 + 100 - 100
    assert row["errorBalanceDest"] == 100        # 0 + 100 - 0  (money vanished -> suspicious)
    assert row["orig_emptied"] == 1 and row["dest_zero_before"] == 1
    assert row["type_TRANSFER"] == 1 and row["type_PAYMENT"] == 0


@pytest.mark.parametrize("mode", DRIFT_MODES)
def test_drift_injection_keeps_schema(mode):
    tx = {"step": 410, "type": "PAYMENT", "amount": 10.0, "nameOrig": "C1", "oldbalanceOrg": 50.0,
          "newbalanceOrig": 40.0, "nameDest": "M9", "oldbalanceDest": 0.0, "newbalanceDest": 0.0, "isFraud": 0}
    out = apply_drift(tx, mode)
    assert set(out) == set(tx)
    assert tx["amount"] == 10.0  # original not mutated
    if mode == "amount_inflation":
        assert out["amount"] == 30.0


def test_unknown_drift_mode_raises():
    with pytest.raises(ValueError):
        apply_drift({"type": "PAYMENT"}, "bogus")
