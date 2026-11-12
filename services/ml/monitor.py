"""Drift + model-performance monitor.

Every MONITOR_INTERVAL seconds:
  1. take the last MONITOR_WINDOW scored transactions from Postgres
  2. compare their feature distributions with the training reference (PSI + KS)
  3. compute live precision / recall using the (delayed) true labels
  4. store a row in `drift_reports`, expose Prometheus gauges (Grafana + alert rules use them)
  5. write an Evidently HTML drift report if Evidently is installed (served by the dashboard)
"""

from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
from prometheus_client import Gauge, start_http_server

from fraudshield_common import config
from fraudshield_common.db import TX_SELECT, connect, read_sql
from fraudshield_common.drift import compute_drift
from fraudshield_common.features import DRIFT_CATEGORICAL, DRIFT_NUMERIC, add_hour
from fraudshield_common.logging_setup import setup_logging

log = setup_logging("monitor")

INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
WINDOW = int(os.getenv("MONITOR_WINDOW", "3000"))
MIN_ROWS = int(os.getenv("MONITOR_MIN_ROWS", "500"))
MIN_LABELED_FRAUD = int(os.getenv("MONITOR_MIN_LABELED_FRAUD", "5"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8003"))

DRIFT_SHARE = Gauge("fraudshield_drift_share", "Share of monitored features that drifted")
DATASET_DRIFT = Gauge("fraudshield_dataset_drift", "1 if dataset drift detected")
FEATURE_PSI = Gauge("fraudshield_feature_psi", "PSI per feature", ["feature"])
PRECISION = Gauge("fraudshield_model_precision", "Precision on recent labeled window")
RECALL = Gauge("fraudshield_model_recall", "Recall on recent labeled window")
PRED_RATE = Gauge("fraudshield_predicted_fraud_rate", "Share of recent tx flagged")
TRUE_RATE = Gauge("fraudshield_actual_fraud_rate", "Share of recent tx that are truly fraud")
LAST_RUN = Gauge("fraudshield_monitor_last_run_timestamp", "Unix time of last monitor run")


def evidently_report(reference: pd.DataFrame, current: pd.DataFrame) -> None:
    """Optional pretty HTML report. Supports Evidently >=0.7 and the older 0.4.x API."""
    cols = DRIFT_NUMERIC + DRIFT_CATEGORICAL
    out = config.REPORTS_DIR / "drift_latest.html"
    try:
        try:  # Evidently >= 0.7
            from evidently import Report
            from evidently.presets import DataDriftPreset

            snapshot = Report([DataDriftPreset()]).run(current_data=current[cols], reference_data=reference[cols])
            snapshot.save_html(str(out))
        except ImportError:  # Evidently 0.4.x
            from evidently.metric_preset import DataDriftPreset
            from evidently.report import Report

            report = Report(metrics=[DataDriftPreset()])
            report.run(reference_data=reference[cols], current_data=current[cols])
            report.save_html(str(out))
        log.info("evidently report written", extra={"path": str(out)})
    except ImportError:
        log.debug("evidently not installed - skipping HTML report")
    except Exception:
        log.exception("evidently report failed (PSI monitoring still works)")


def run_once(conn, reference: pd.DataFrame) -> dict | None:
    current = read_sql(conn, f"SELECT {TX_SELECT} FROM predictions ORDER BY scored_at DESC LIMIT %s", (WINDOW,))
    if len(current) < MIN_ROWS:
        log.info("not enough data yet", extra={"rows": len(current), "needed": MIN_ROWS})
        return None
    for c in ["amount", "oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest", "fraud_probability"]:
        current[c] = current[c].astype(float)
    current = add_hour(current)

    drift = compute_drift(reference, current, DRIFT_NUMERIC, DRIFT_CATEGORICAL,
                          config.PSI_THRESHOLD, config.DATASET_DRIFT_SHARE, config.SEVERE_PSI)

    labeled = current.dropna(subset=["isFraud"])
    y, yhat = labeled["isFraud"].astype(int).to_numpy(), labeled["is_fraud_pred"].astype(bool).to_numpy()
    tp, fp, fn = int(((y == 1) & yhat).sum()), int(((y == 0) & yhat).sum()), int(((y == 1) & ~yhat).sum())
    perf = {
        "labeled_rows": int(len(labeled)), "true_fraud": int(y.sum()), "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "recall": tp / (tp + fn) if (tp + fn) else None,
        "predicted_fraud_rate": float(current["is_fraud_pred"].astype(bool).mean()),
        "actual_fraud_rate": float(y.mean()) if len(y) else None,
    }

    # ---- Prometheus ----
    DRIFT_SHARE.set(drift["share_drifted"])
    DATASET_DRIFT.set(1 if drift["dataset_drift"] else 0)
    for feat, info in drift["features"].items():
        FEATURE_PSI.labels(feature=feat).set(info["psi"])
    PRED_RATE.set(perf["predicted_fraud_rate"])
    if perf["actual_fraud_rate"] is not None:
        TRUE_RATE.set(perf["actual_fraud_rate"])
    # only publish precision/recall when there are enough fraud cases to be meaningful
    if perf["true_fraud"] >= MIN_LABELED_FRAUD:
        RECALL.set(perf["recall"] if perf["recall"] is not None else np.nan)
        PRECISION.set(perf["precision"] if perf["precision"] is not None else np.nan)
    LAST_RUN.set(time.time())

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO drift_reports (window_size, share_drifted, dataset_drift, drifted_features, details)
               VALUES (%s, %s, %s, %s, %s)""",
            (len(current), drift["share_drifted"], drift["dataset_drift"], drift["drifted_features"],
             json.dumps({"drift": drift, "performance": perf}, default=str)),
        )
    level = log.warning if drift["dataset_drift"] else log.info
    level("drift check", extra={"share_drifted": drift["share_drifted"], "drifted": drift["drifted_features"],
                                "dataset_drift": drift["dataset_drift"], "recall": perf["recall"],
                                "precision": perf["precision"]})
    evidently_report(reference, current)
    return drift


def load_reference() -> pd.DataFrame:
    ref = pd.read_csv(config.MODEL_DIR / "reference.csv")
    return add_hour(ref) if "hour" not in ref else ref


def main() -> None:
    start_http_server(METRICS_PORT)
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    reference, ref_mtime = None, 0.0
    while True:
        try:
            mtime = (config.MODEL_DIR / "reference.csv").stat().st_mtime
            if mtime != ref_mtime:  # reference is replaced after a retrain is promoted
                reference, ref_mtime = load_reference(), mtime
                log.info("reference data loaded", extra={"rows": len(reference)})
            run_once(conn, reference)
        except Exception:
            log.exception("monitor run failed")
            try:
                conn = connect()
            except Exception:
                pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
