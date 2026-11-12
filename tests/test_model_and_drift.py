import numpy as np

from fraudshield_common.data import generate_synthetic
from fraudshield_common.drift import compute_drift, psi_categorical, psi_numeric
from fraudshield_common.drift_injection import apply_drift
from fraudshield_common.features import DRIFT_CATEGORICAL, DRIFT_NUMERIC, add_hour
from fraudshield_common.model_store import current_version, list_versions, load_model, score, set_current


def test_model_quality(trained):
    _, metadata = trained
    m = metadata["metrics"]
    # far better than chance (fraud rate ~1%), but not suspiciously perfect
    assert m["pr_auc"] > 0.5
    assert m["roc_auc"] > 0.95
    assert 0 < m["threshold"] < 1


def test_model_store_roundtrip(model_dir, synthetic_df):
    version = current_version(model_dir)
    artifact, meta = load_model(model_dir)
    assert meta["version"] == version
    proba, flags = score(artifact, synthetic_df.head(500))
    assert proba.shape == (500,) and ((proba >= 0) & (proba <= 1)).all()
    assert flags.dtype == bool
    assert len(list_versions(model_dir)) == 1


def test_set_current_switches_champion(model_dir):
    set_current(model_dir, "vOTHER")
    assert current_version(model_dir) == "vOTHER"


def test_psi_identical_is_zero():
    rng = np.random.default_rng(0)
    import pandas as pd
    s = pd.Series(rng.normal(size=5000))
    assert psi_numeric(s, s) < 1e-6
    assert psi_categorical(pd.Series(list("aabbc")), pd.Series(list("aabbc"))) < 1e-6


def test_no_drift_between_two_samples_of_same_data():
    a = add_hour(generate_synthetic(20_000, seed=10))
    b = add_hour(generate_synthetic(5_000, seed=11))
    report = compute_drift(a, b, DRIFT_NUMERIC, DRIFT_CATEGORICAL)
    assert not report["dataset_drift"], report["drifted_features"]


def test_amount_inflation_is_detected():
    ref = add_hour(generate_synthetic(20_000, seed=10))
    cur = generate_synthetic(5_000, seed=11)
    cur = cur.apply(lambda r: apply_drift(r.to_dict(), "amount_inflation"), axis=1, result_type="expand")
    report = compute_drift(ref, add_hour(cur), DRIFT_NUMERIC, DRIFT_CATEGORICAL)
    assert report["dataset_drift"]
    assert "amount" in report["drifted_features"]


def test_type_shift_is_detected_on_categorical():
    ref = add_hour(generate_synthetic(20_000, seed=10))
    cur = generate_synthetic(5_000, seed=11)
    cur = cur.apply(lambda r: apply_drift(r.to_dict(), "type_shift"), axis=1, result_type="expand")
    report = compute_drift(ref, add_hour(cur), DRIFT_NUMERIC, DRIFT_CATEGORICAL)
    assert report["features"]["type"]["drifted"]
    assert report["dataset_drift"]  # one severely drifted feature is enough
