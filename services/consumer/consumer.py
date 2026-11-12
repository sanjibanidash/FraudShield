"""Kafka consumer: the real-time scoring pipeline.

transactions topic --> micro-batch (<= BATCH_SIZE msgs or BATCH_TIMEOUT s)
                   --> FastAPI /predict/batch
                   --> Postgres `predictions` table (idempotent upsert)
                   --> fraud cases published to `fraud-alerts` topic
                   --> offsets committed only after the DB write (at-least-once delivery)
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime

import requests
from confluent_kafka import Consumer, KafkaError, Producer
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from psycopg2.extras import execute_values

from fraudshield_common import config
from fraudshield_common.db import connect
from fraudshield_common.logging_setup import setup_logging

log = setup_logging("consumer")

BATCH_SIZE = int(os.getenv("CONSUMER_BATCH_SIZE", "100"))
BATCH_TIMEOUT = float(os.getenv("CONSUMER_BATCH_TIMEOUT", "1.0"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))
GROUP_ID = os.getenv("CONSUMER_GROUP", "fraud-scorer")

CONSUMED = Counter("consumer_messages_total", "Messages consumed and scored")
FLAGGED = Counter("consumer_fraud_flagged_total", "Transactions flagged as fraud")
ACTUAL_FRAUD = Counter("consumer_actual_fraud_total", "Transactions whose true label is fraud")
ERRORS = Counter("consumer_errors_total", "Processing errors", ["stage"])
E2E = Histogram("consumer_end_to_end_latency_seconds", "Produced -> stored latency",
                buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
BATCH = Histogram("consumer_batch_size", "Micro-batch size", buckets=(1, 5, 10, 25, 50, 100, 250, 500))
LAST_SUCCESS = Gauge("consumer_last_success_timestamp", "Unix time of last successful batch")

INSERT_SQL = """
INSERT INTO predictions (tx_id, step, type, amount, name_orig, oldbalance_org, newbalance_orig,
    name_dest, oldbalance_dest, newbalance_dest, fraud_probability, is_fraud_pred, true_label,
    model_version, drift_mode, produced_at, latency_ms)
VALUES %s ON CONFLICT (tx_id) DO NOTHING
"""

MODEL_FIELDS = ["tx_id", "step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig",
                "nameDest", "oldbalanceDest", "newbalanceDest"]


def score_batch(session: requests.Session, events: list[dict]) -> dict:
    payload = {"transactions": [{k: e[k] for k in MODEL_FIELDS} for e in events]}
    for attempt in range(5):
        try:
            r = session.post(f"{config.API_URL}/predict/batch", json=payload, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            ERRORS.labels(stage="api").inc()
            wait = 2 ** attempt
            log.warning("scoring API call failed, retrying", extra={"error": str(exc), "wait_s": wait})
            time.sleep(wait)
    raise RuntimeError("scoring API unavailable")


def main() -> None:
    start_http_server(METRICS_PORT)
    consumer = Consumer({
        "bootstrap.servers": config.KAFKA_BOOTSTRAP,
        "group.id": GROUP_ID,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    alert_producer = Producer({"bootstrap.servers": config.KAFKA_BOOTSTRAP, "linger.ms": 50})
    consumer.subscribe([config.TOPIC_TRANSACTIONS])
    conn = connect()
    session = requests.Session()
    log.info("consumer started", extra={"topic": config.TOPIC_TRANSACTIONS, "group": GROUP_ID})

    while True:
        msgs = consumer.consume(num_messages=BATCH_SIZE, timeout=BATCH_TIMEOUT)
        events = []
        for m in msgs:
            if m.error():
                if m.error().code() != KafkaError._PARTITION_EOF:
                    ERRORS.labels(stage="kafka").inc()
                    log.error("kafka error", extra={"error": str(m.error())})
                continue
            try:
                events.append(json.loads(m.value()))
            except json.JSONDecodeError:
                ERRORS.labels(stage="decode").inc()
                log.error("bad message skipped")
        if not events:
            continue

        BATCH.observe(len(events))
        result = score_batch(session, events)
        preds = {p["tx_id"]: p for p in result["predictions"]}
        api_latency = result["latency_ms"]

        rows = []
        for e in events:
            p = preds[e["tx_id"]]
            rows.append((e["tx_id"], e["step"], e["type"], e["amount"], e["nameOrig"], e["oldbalanceOrg"],
                         e["newbalanceOrig"], e["nameDest"], e["oldbalanceDest"], e["newbalanceDest"],
                         p["fraud_probability"], p["is_fraud"], e.get("label"), p["model_version"],
                         e.get("drift_mode", "none"), e.get("produced_at"), api_latency))
        try:
            with conn.cursor() as cur:
                execute_values(cur, INSERT_SQL, rows)
        except Exception:
            ERRORS.labels(stage="db").inc()
            log.exception("db write failed; reconnecting (batch will be re-consumed)")
            conn = connect()
            continue  # offsets not committed -> messages are redelivered

        now = datetime.now(UTC)
        n_flagged = 0
        for e in events:
            p = preds[e["tx_id"]]
            if e.get("label") == 1:
                ACTUAL_FRAUD.inc()
            if p["is_fraud"]:
                n_flagged += 1
                alert = {**e, "fraud_probability": p["fraud_probability"], "model_version": p["model_version"]}
                alert.pop("label", None)
                alert_producer.produce(config.TOPIC_ALERTS, key=e["tx_id"], value=json.dumps(alert).encode())
            if e.get("produced_at"):
                E2E.observe((now - datetime.fromisoformat(e["produced_at"])).total_seconds())
        alert_producer.poll(0)
        consumer.commit(asynchronous=False)

        CONSUMED.inc(len(events))
        FLAGGED.inc(n_flagged)
        LAST_SUCCESS.set(time.time())
        if n_flagged:
            log.info("fraud flagged", extra={"batch": len(events), "flagged": n_flagged})


if __name__ == "__main__":
    main()
