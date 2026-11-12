"""Flask analyst dashboard.

Pages
  /            overview: KPIs, model version, latest drift, recent alerts (auto-refresh)
  /flagged     flagged transactions -> analyst marks "confirmed fraud" / "false alarm" (feedback loop)
  /drift       drift report history + latest Evidently HTML report
  /models      model registry: versions, metrics, promotions
  /alerts      alerts received from Alertmanager
  /controls    inject drift into the live stream, trigger retraining   (password protected)
JSON
  /api/stats, /api/flagged       for other tools
  /webhook/alertmanager          Alertmanager posts alerts here; optionally forwarded to Slack/Discord/Telegram
  /health, /metrics
"""

from __future__ import annotations

import json
import os
from functools import wraps

import requests
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, url_for
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from fraudshield_common import config
from fraudshield_common.db import connect, cursor, get_setting, set_setting
from fraudshield_common.drift_injection import DRIFT_MODES
from fraudshield_common.logging_setup import setup_logging

log = setup_logging("dashboard")
app = Flask(__name__)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")  # empty -> no auth (local dev)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PUBLIC_GRAFANA_URL = os.getenv("PUBLIC_GRAFANA_URL", "http://localhost:3000")

REVIEWS = Counter("dashboard_reviews_total", "Analyst reviews", ["label"])
ALERTS_RECEIVED = Counter("dashboard_alerts_received_total", "Alerts received from Alertmanager", ["status"])

_conn = None


def db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = connect()
    return _conn


def query(sql: str, params=None) -> list[dict]:
    global _conn
    try:
        with cursor(db()) as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    except Exception:
        _conn = None
        raise


def requires_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if DASHBOARD_PASSWORD:
            auth = request.authorization
            if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="FraudShield"'})
        return fn(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------------ data helpers
def get_stats() -> dict:
    s = query("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE is_fraud_pred) AS flagged,
               count(*) FILTER (WHERE true_label = 1) AS true_fraud,
               count(*) FILTER (WHERE is_fraud_pred AND true_label = 1) AS tp,
               count(*) FILTER (WHERE is_fraud_pred AND true_label = 0) AS fp,
               count(*) FILTER (WHERE NOT is_fraud_pred AND true_label = 1) AS fn,
               count(*) FILTER (WHERE scored_at > now() - interval '1 minute') AS last_minute,
               count(*) FILTER (WHERE is_fraud_pred AND analyst_label IS NULL) AS pending_review,
               coalesce(sum(amount) FILTER (WHERE is_fraud_pred AND true_label = 1), 0) AS fraud_amount_caught,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency
        FROM predictions WHERE scored_at > now() - interval '24 hours'""")[0]
    s["precision"] = s["tp"] / (s["tp"] + s["fp"]) if (s["tp"] + s["fp"]) else None
    s["recall"] = s["tp"] / (s["tp"] + s["fn"]) if (s["tp"] + s["fn"]) else None
    s["flag_rate"] = s["flagged"] / s["total"] if s["total"] else 0
    s["fraud_amount_caught"] = float(s["fraud_amount_caught"])
    return s


def model_info() -> dict:
    try:
        return requests.get(f"{config.API_URL}/model/info", timeout=3).json()
    except Exception:
        return {"version": "API unreachable"}


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    drift = query("SELECT * FROM drift_reports ORDER BY created_at DESC LIMIT 1")
    alerts = query("SELECT * FROM alerts ORDER BY received_at DESC LIMIT 5")
    return render_template("index.html", stats=get_stats(), model=model_info(), drift=drift[0] if drift else None,
                           alerts=alerts, drift_mode=get_setting(db(), "drift_mode", "none"),
                           grafana_url=PUBLIC_GRAFANA_URL)


@app.route("/flagged")
def flagged():
    status = request.args.get("status", "pending")
    where = {"pending": "analyst_label IS NULL", "reviewed": "analyst_label IS NOT NULL"}.get(status, "TRUE")
    rows = query(f"""SELECT tx_id, scored_at, type, amount, name_orig, name_dest, oldbalance_org, newbalance_orig,
                            fraud_probability, true_label, analyst_label, model_version
                     FROM predictions WHERE is_fraud_pred AND {where}
                     ORDER BY scored_at DESC LIMIT 200""")
    return render_template("flagged.html", rows=rows, status=status)


@app.post("/review/<tx_id>")
@requires_auth
def review(tx_id: str):
    label = request.form.get("label")
    if label not in {"fraud", "legit"}:
        abort(400)
    query("UPDATE predictions SET analyst_label = %s, reviewed_at = now() WHERE tx_id = %s", (label, tx_id))
    REVIEWS.labels(label=label).inc()
    log.info("analyst review", extra={"tx_id": tx_id, "label": label})
    return redirect(request.referrer or url_for("flagged"))


@app.route("/drift")
def drift():
    rows = query("""SELECT id, created_at, window_size, share_drifted, dataset_drift, drifted_features, details
                    FROM drift_reports ORDER BY created_at DESC LIMIT 50""")
    latest = rows[0] if rows else None
    has_html = (config.REPORTS_DIR / "drift_latest.html").exists()
    return render_template("drift.html", rows=rows, latest=latest, has_html=has_html)


@app.route("/drift/report")
def drift_report_html():
    path = config.REPORTS_DIR / "drift_latest.html"
    if not path.exists():
        abort(404)
    return send_file(path)


@app.route("/models")
def models():
    # read the registry files directly (keeps the dashboard image free of ML libraries)
    versions = [json.loads(p.read_text()) for p in config.MODEL_DIR.glob("metadata_*.json")]
    versions.sort(key=lambda m: m["version"], reverse=True)
    current_file = config.MODEL_DIR / "current.json"
    current = json.loads(current_file.read_text())["version"] if current_file.exists() else None
    events = query("SELECT * FROM model_events ORDER BY created_at DESC LIMIT 20")
    return render_template("models.html", versions=versions, current=current, events=events)


@app.route("/alerts")
def alerts():
    rows = query("SELECT * FROM alerts ORDER BY received_at DESC LIMIT 100")
    return render_template("alerts.html", rows=rows)


@app.route("/controls", methods=["GET", "POST"])
@requires_auth
def controls():
    message = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "drift":
            mode = request.form.get("mode", "none")
            if mode not in DRIFT_MODES:
                abort(400)
            set_setting(db(), "drift_mode", mode)
            message = f"Drift mode set to '{mode}'. The producer picks it up within ~5 seconds."
        elif action == "retrain":
            set_setting(db(), "retrain_requested", "1")
            message = "Retraining requested. The batch service starts it within ~15 seconds."
        log.warning("control action", extra={"action": action, "form": dict(request.form)})
    return render_template("controls.html", modes=DRIFT_MODES, message=message,
                           drift_mode=get_setting(db(), "drift_mode", "none"),
                           retrain_requested=get_setting(db(), "retrain_requested", "0"))


# ------------------------------------------------------------------ alert webhook
def format_alert(alert: dict, status: str) -> str:
    labels, ann = alert.get("labels", {}), alert.get("annotations", {})
    icon = "✅ RESOLVED" if status == "resolved" else "🚨 FIRING"
    return f"{icon} [{labels.get('severity', 'info').upper()}] {labels.get('alertname')}: {ann.get('summary', '')}"


def forward(text: str) -> None:
    """Free notification channels: Slack / Discord incoming webhooks, Telegram bot."""
    try:
        if SLACK_WEBHOOK_URL:
            requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=5)
        if DISCORD_WEBHOOK_URL:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=5)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except requests.RequestException:
        log.exception("forwarding alert failed")


@app.post("/webhook/alertmanager")
def alertmanager_webhook():
    payload = request.get_json(force=True)
    for alert in payload.get("alerts", []):
        status = alert.get("status", payload.get("status", "firing"))
        labels, ann = alert.get("labels", {}), alert.get("annotations", {})
        query("""INSERT INTO alerts (status, alertname, severity, summary, payload)
                 VALUES (%s, %s, %s, %s, %s)""",
              (status, labels.get("alertname"), labels.get("severity"), ann.get("summary"), json.dumps(alert)))
        ALERTS_RECEIVED.labels(status=status).inc()
        text = format_alert(alert, status)
        log.warning("alert received", extra={"alert": labels.get("alertname"), "status": status})
        forward(text)
    return {"ok": True}


# ------------------------------------------------------------------ json + ops
@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/flagged")
def api_flagged():
    return jsonify(query("""SELECT tx_id, scored_at, type, amount, fraud_probability, analyst_label
                            FROM predictions WHERE is_fraud_pred ORDER BY scored_at DESC LIMIT 100"""))


@app.route("/health")
def health():
    query("SELECT 1")
    return {"status": "ok"}


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.template_filter("money")
def money(v):
    return f"{float(v):,.2f}" if v is not None else "-"


@app.template_filter("pct")
def pct(v):
    return f"{float(v) * 100:.1f}%" if v is not None else "-"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
