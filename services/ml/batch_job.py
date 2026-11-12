"""Batch processing service (the "batch" half of real-time vs batch).

Runs on a schedule (BATCH_DAILY_AT="02:00" for nightly, or every BATCH_INTERVAL seconds for demos):

  1. Bulk scoring  : every CSV dropped in data/batch/input/ is scored in one vectorised pass
                     with the model loaded in-process (no API, no Kafka) -> data/batch/output/
  2. Daily report  : volumes, fraud rate, precision/recall, latency for the last 24h
                     -> `daily_reports` table + reports/daily_report_<date>.json
  3. Retraining    : if drift was detected, recall dropped, or someone clicked "Retrain now"
                     -> train a challenger on (training snapshot + recent labeled stream data),
                        compare with the champion on a recent holdout, promote only if not worse,
                        then tell the API to hot-reload.

    python batch_job.py            # run forever on schedule
    python batch_job.py --once     # run all tasks once and exit
    python batch_job.py --retrain  # force a retrain once and exit
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import requests
from prometheus_client import Counter, Gauge, start_http_server

from fraudshield_common import config
from fraudshield_common.db import TX_SELECT, connect, get_setting, read_sql, set_setting
from fraudshield_common.features import LABEL_COLUMN, RAW_COLUMNS, add_hour
from fraudshield_common.logging_setup import setup_logging
from fraudshield_common.model_store import load_model, predict_proba, save_model, score
from fraudshield_common.training import downsample_legit, evaluate, train_model

log = setup_logging("batch")

DAILY_AT = os.getenv("BATCH_DAILY_AT", "")  # e.g. "02:00" (UTC). Empty -> use BATCH_INTERVAL
INTERVAL = int(os.getenv("BATCH_INTERVAL", "900"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8004"))
MIN_RECALL = float(os.getenv("RETRAIN_MIN_RECALL", "0.6"))
MIN_NEW_LABELED = int(os.getenv("RETRAIN_MIN_ROWS", "2000"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
REFERENCE_ROWS = int(os.getenv("REFERENCE_ROWS", "5000"))

LAST_RUN = Gauge("batch_last_run_timestamp", "Unix time of last batch run")
LAST_OK = Gauge("batch_last_run_success", "1 if the last batch run succeeded")
ROWS_SCORED = Counter("batch_rows_scored_total", "Rows scored by bulk scoring")
RETRAINS = Counter("batch_retrain_total", "Retraining attempts", ["result"])


# ------------------------------------------------------------------ 1. bulk scoring
def bulk_score_files() -> int:
    config.BATCH_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    config.BATCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    done_dir = config.BATCH_INPUT_DIR.parent / "processed"
    done_dir.mkdir(exist_ok=True)
    files = sorted(config.BATCH_INPUT_DIR.glob("*.csv"))
    if not files:
        return 0
    artifact, meta = load_model(config.MODEL_DIR)
    total = 0
    for f in files:
        t0 = time.perf_counter()
        df = pd.read_csv(f)
        proba, flags = score(artifact, df)
        df["fraud_probability"], df["is_fraud_pred"], df["model_version"] = proba.round(6), flags, meta["version"]
        out = config.BATCH_OUTPUT_DIR / f"{f.stem}_scored.csv"
        df.to_csv(out, index=False)
        shutil.move(str(f), done_dir / f.name)
        total += len(df)
        log.info("bulk scored file", extra={"file": f.name, "rows": len(df), "flagged": int(flags.sum()),
                                            "seconds": round(time.perf_counter() - t0, 2), "output": str(out)})
    ROWS_SCORED.inc(total)
    return total


# ------------------------------------------------------------------ 2. daily report
def daily_report(conn) -> dict:
    df = read_sql(conn, """
        SELECT is_fraud_pred, true_label, latency_ms, analyst_label, model_version, drift_mode
        FROM predictions WHERE scored_at >= now() - interval '24 hours'""")
    report: dict = {"report_date": date.today().isoformat(), "generated_at": datetime.now(UTC).isoformat(),
                    "transactions": int(len(df))}
    if len(df):
        y = df["true_label"].dropna().astype(int)
        yhat = df.loc[y.index, "is_fraud_pred"].astype(bool)
        tp = int(((y == 1) & yhat).sum())
        fp = int(((y == 0) & yhat).sum())
        fn = int(((y == 1) & ~yhat).sum())
        report.update({
            "flagged": int(df["is_fraud_pred"].sum()),
            "flag_rate": float(df["is_fraud_pred"].mean()),
            "true_fraud": int(y.sum()),
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "api_latency_ms_p50": float(df["latency_ms"].quantile(0.5)),
            "api_latency_ms_p95": float(df["latency_ms"].quantile(0.95)),
            "analyst_reviews": df["analyst_label"].value_counts().to_dict(),
            "model_versions": df["model_version"].value_counts().to_dict(),
            "drift_modes_seen": df["drift_mode"].value_counts().to_dict(),
        })
    with conn.cursor() as cur:
        cur.execute("INSERT INTO daily_reports (report_date, details) VALUES (%s, %s)",
                    (report["report_date"], json.dumps(report, default=str)))
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / f"daily_report_{report['report_date']}.json").write_text(json.dumps(report, indent=2,
                                                                                               default=str))
    log.info("daily report", extra={"report": report})
    return report


# ------------------------------------------------------------------ 3. retraining
def retrain_reason(conn) -> str | None:
    if get_setting(conn, "retrain_requested", "0") == "1":
        return "manual"
    last = read_sql(conn, "SELECT dataset_drift, details FROM drift_reports ORDER BY created_at DESC LIMIT 1")
    if len(last):
        if bool(last.iloc[0]["dataset_drift"]):
            return "data_drift"
        details = last.iloc[0]["details"]
        details = details if isinstance(details, dict) else json.loads(details)
        recall = details.get("performance", {}).get("recall")
        if recall is not None and details["performance"].get("true_fraud", 0) >= 5 and recall < MIN_RECALL:
            return "low_recall"
    return None


def retrain(conn, reason: str) -> dict:
    log.warning("retraining started", extra={"reason": reason})
    recent = read_sql(conn, f"""SELECT {TX_SELECT} FROM predictions
                                WHERE true_label IS NOT NULL ORDER BY scored_at DESC LIMIT 100000""")
    if len(recent) < MIN_NEW_LABELED and reason != "manual":
        RETRAINS.labels(result="skipped").inc()
        log.info("not enough new labeled data", extra={"rows": len(recent)})
        return {"result": "skipped"}
    for c in ["amount", "oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest"]:
        recent[c] = recent[c].astype(float)
    recent["isFraud"] = recent["isFraud"].astype(int)
    # analyst feedback overrides the raw label when available
    recent.loc[recent["analyst_label"] == "fraud", "isFraud"] = 1
    recent.loc[recent["analyst_label"] == "legit", "isFraud"] = 0
    recent = recent.sort_values("scored_at").reset_index(drop=True)

    # newest 25% of stream data is the holdout both models are judged on
    cut = int(len(recent) * 0.75)
    recent_train, holdout = recent.iloc[:cut], recent.iloc[cut:]

    cols = RAW_COLUMNS + [LABEL_COLUMN]
    snapshot = pd.read_csv(config.MODEL_DIR / "train_snapshot.csv.gz")[cols]
    recent_train = recent_train[cols].copy()
    # stream rows get later "steps" than the snapshot so the time split validates on new data
    recent_train["step"] = recent_train["step"].astype(int) + int(snapshot["step"].max())
    combined = downsample_legit(pd.concat([snapshot, recent_train], ignore_index=True), 300_000)

    challenger, meta = train_model(combined)
    champion, champ_meta = load_model(config.MODEL_DIR)

    y = holdout["isFraud"].to_numpy()
    p_chal, p_champ = predict_proba(challenger, holdout), predict_proba(champion, holdout)
    m_chal, m_champ = evaluate(y, p_chal, challenger["threshold"]), evaluate(y, p_champ, champion["threshold"])
    key = "pr_auc" if m_chal["pr_auc"] is not None else "f1"
    promote = (m_chal[key] or 0) >= (m_champ[key] or 0)

    meta.update({"trigger": reason, "holdout": {"challenger": m_chal, "champion": m_champ},
                 "parent_version": champ_meta["version"], "data_source": "snapshot+stream"})
    version = save_model(config.MODEL_DIR, challenger, meta, make_current=promote)
    result = {"result": "promoted" if promote else "rejected", "version": version, "reason": reason,
              "metric": key, "challenger": m_chal[key], "champion": m_champ[key]}

    with conn.cursor() as cur:
        cur.execute("INSERT INTO model_events (event, version, details) VALUES (%s, %s, %s)",
                    (result["result"], version, json.dumps(result, default=str)))
    if promote:
        # the new model has learned the new distribution -> the NEWEST traffic becomes the drift reference
        newest = recent[cols].tail(REFERENCE_ROWS)
        ref_src = newest if len(newest) >= 1000 else combined
        add_hour(ref_src.sample(min(20_000, len(ref_src)), random_state=1)).to_csv(
            config.MODEL_DIR / "reference.csv", index=False)
        try:
            requests.post(f"{config.API_URL}/admin/reload", headers={"x-admin-token": ADMIN_TOKEN}, timeout=10)
        except requests.RequestException:
            log.warning("API reload call failed; API will pick up the model on its own within 30s")
    RETRAINS.labels(result=result["result"]).inc()
    set_setting(conn, "retrain_requested", "0")
    log.warning("retraining finished", extra=result)
    return result


# ------------------------------------------------------------------ scheduler
def run_all(conn, force_retrain: bool = False) -> None:
    ok = 1
    for name, fn in [("bulk_score", bulk_score_files), ("daily_report", lambda: daily_report(conn))]:
        try:
            fn()
        except Exception:
            ok = 0
            log.exception(f"{name} failed")
    try:
        reason = "manual" if force_retrain else retrain_reason(conn)
        if reason:
            retrain(conn, reason)
    except Exception:
        ok = 0
        RETRAINS.labels(result="error").inc()
        log.exception("retraining failed")
    finally:
        set_setting(conn, "retrain_requested", "0")  # never loop on a failed manual request
    LAST_RUN.set(time.time())
    LAST_OK.set(ok)


def next_run(now: datetime) -> datetime:
    if DAILY_AT:
        hh, mm = map(int, DAILY_AT.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return target if target > now else target + timedelta(days=1)
    return now + timedelta(seconds=INTERVAL)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--retrain", action="store_true")
    args = p.parse_args()
    conn = connect()
    if args.once or args.retrain:
        run_all(conn, force_retrain=args.retrain)
        return

    start_http_server(METRICS_PORT)
    due = next_run(datetime.now(UTC))
    log.info("batch scheduler started", extra={"next_run": due.isoformat()})
    while True:
        try:
            manual = get_setting(conn, "retrain_requested", "0") == "1"
            if manual or datetime.now(UTC) >= due:
                run_all(conn)
                due = next_run(datetime.now(UTC))
                log.info("next batch run scheduled", extra={"next_run": due.isoformat()})
        except Exception:
            log.exception("scheduler error")
            conn = connect()
        time.sleep(15)


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
