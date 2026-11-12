"""Kafka producer: replays the 'future' part of PaySim as a live stream of payment events.

- Sends to topic `transactions`, keyed by the origin account (same customer -> same partition, keeps order).
- Rate controlled by PRODUCER_TPS.
- Drift injection mode is read from the `settings` table every few seconds, so you can flip it
  live from the dashboard (Controls page) and watch the drift monitor + alerts react.
- The true label (`isFraud`) travels with the event to simulate ground truth arriving later
  (in real life: chargebacks / customer complaints). The model never sees it.
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from datetime import UTC, datetime

import pandas as pd
from confluent_kafka import Producer
from prometheus_client import Counter, Gauge, start_http_server

from fraudshield_common import config
from fraudshield_common.db import connect, get_setting
from fraudshield_common.drift_injection import DRIFT_MODES, apply_drift
from fraudshield_common.logging_setup import setup_logging

log = setup_logging("producer")

TPS = float(os.getenv("PRODUCER_TPS", "20"))
LOOP = os.getenv("PRODUCER_LOOP", "true").lower() == "true"
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))
SETTINGS_REFRESH = 5.0

SENT = Counter("producer_messages_total", "Messages produced", ["drift_mode"])
ERRORS = Counter("producer_errors_total", "Delivery errors")
DRIFT_GAUGE = Gauge("producer_drift_mode", "Active drift mode (1 = active)", ["mode"])


def delivery_report(err, _msg):
    if err is not None:
        ERRORS.inc()
        log.error("delivery failed", extra={"error": str(err)})


def set_drift_gauge(mode: str) -> None:
    for m in DRIFT_MODES:
        DRIFT_GAUGE.labels(mode=m).set(1 if m == mode else 0)


def main() -> None:
    start_http_server(METRICS_PORT)
    producer = Producer({
        "bootstrap.servers": config.KAFKA_BOOTSTRAP,
        "linger.ms": 20,
        "acks": "all",
        "enable.idempotence": True,
        "client.id": "fraudshield-producer",
    })
    conn = connect()
    drift_mode = os.getenv("DRIFT_MODE", "none")
    set_drift_gauge(drift_mode)
    last_settings_check = 0.0
    rng = random.Random(7)
    interval = 1.0 / TPS if TPS > 0 else 0

    while True:
        log.info("starting replay", extra={"file": str(config.STREAM_DATA_PATH), "tps": TPS})
        for chunk in pd.read_csv(config.STREAM_DATA_PATH, chunksize=5_000):
            for row in chunk.to_dict(orient="records"):
                now = time.time()
                if now - last_settings_check > SETTINGS_REFRESH:
                    last_settings_check = now
                    try:
                        new_mode = get_setting(conn, "drift_mode", "none")
                    except Exception:
                        log.exception("could not read settings; reconnecting")
                        conn = connect()
                        new_mode = drift_mode
                    if new_mode != drift_mode:
                        log.warning("drift mode changed", extra={"from": drift_mode, "to": new_mode})
                        drift_mode = new_mode
                        set_drift_gauge(drift_mode)

                tx = apply_drift(row, drift_mode, rng)
                event = {
                    "tx_id": str(uuid.uuid4()),
                    "step": int(tx["step"]),
                    "type": tx["type"],
                    "amount": float(tx["amount"]),
                    "nameOrig": tx["nameOrig"],
                    "oldbalanceOrg": float(tx["oldbalanceOrg"]),
                    "newbalanceOrig": float(tx["newbalanceOrig"]),
                    "nameDest": tx["nameDest"],
                    "oldbalanceDest": float(tx["oldbalanceDest"]),
                    "newbalanceDest": float(tx["newbalanceDest"]),
                    "label": int(tx["isFraud"]),  # delayed ground truth (not a model input)
                    "drift_mode": drift_mode,
                    "produced_at": datetime.now(UTC).isoformat(),
                }
                producer.produce(config.TOPIC_TRANSACTIONS, key=event["nameOrig"],
                                 value=json.dumps(event).encode(), on_delivery=delivery_report)
                producer.poll(0)
                SENT.labels(drift_mode=drift_mode).inc()
                if interval:
                    time.sleep(max(0.0, interval - (time.time() - now)))
        producer.flush()
        if not LOOP:
            log.info("replay finished")
            return
        log.info("replay finished - looping")


if __name__ == "__main__":
    main()
