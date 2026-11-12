"""Postgres helpers (psycopg2 is imported lazily so pure-ML code/tests don't need it)."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from .config import DATABASE_URL

log = logging.getLogger(__name__)

# SQL aliases mapping DB snake_case columns back to the PaySim names used by the model
TX_SELECT = """
    tx_id, step, type, amount,
    name_orig AS "nameOrig", oldbalance_org AS "oldbalanceOrg", newbalance_orig AS "newbalanceOrig",
    name_dest AS "nameDest", oldbalance_dest AS "oldbalanceDest", newbalance_dest AS "newbalanceDest",
    true_label AS "isFraud", fraud_probability, is_fraud_pred, analyst_label, model_version, scored_at
"""


def connect(url: str = DATABASE_URL, retries: int = 30, delay: float = 2.0):
    import psycopg2

    last = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(url)
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as exc:  # DB still starting
            last = exc
            log.warning("database not ready, retrying", extra={"attempt": attempt})
            time.sleep(delay)
    raise RuntimeError(f"could not connect to database: {last}")


@contextmanager
def cursor(conn):
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def get_setting(conn, key: str, default: str = "") -> str:
    with cursor(conn) as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else default


def set_setting(conn, key: str, value: str) -> None:
    with cursor(conn) as cur:
        cur.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES (%s, %s, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (key, value),
        )


def read_sql(conn, sql: str, params=None):
    import pandas as pd

    with cursor(conn) as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)
