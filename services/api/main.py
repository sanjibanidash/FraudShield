"""FastAPI model-serving service (real-time inference).

Endpoints
  POST /predict          score one transaction
  POST /predict/batch    score up to 1000 transactions (used by the Kafka consumer)
  GET  /health           liveness/readiness
  GET  /model/info       current model version + metrics
  POST /admin/reload     hot-reload the champion model (called after retraining)
  GET  /metrics          Prometheus metrics
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from fraudshield_common import config
from fraudshield_common.logging_setup import setup_logging
from fraudshield_common.model_store import current_version, load_model, score

log = setup_logging("api")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
RELOAD_CHECK_SECONDS = int(os.getenv("RELOAD_CHECK_SECONDS", "30"))

# ---------------- Prometheus metrics ----------------
REQUESTS = Counter("api_requests_total", "HTTP requests", ["endpoint", "method", "status"])
LATENCY = Histogram("api_request_latency_seconds", "Request latency", ["endpoint"],
                    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
PREDICTIONS = Counter("api_predictions_total", "Predictions made", ["label"])
FRAUD_PROBA = Histogram("api_fraud_probability", "Distribution of fraud scores",
                        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.0))
MODEL_LOADED = Gauge("api_model_loaded", "1 if a model is loaded")
MODEL_INFO = Gauge("api_model_info", "Currently served model", ["version"])
MODEL_THRESHOLD = Gauge("api_model_threshold", "Decision threshold of served model")


# ---------------- Schemas ----------------
class Transaction(BaseModel):
    tx_id: str | None = Field(default=None, description="Optional id; generated if missing")
    step: int = Field(ge=0, description="Hour index of the simulation (hour of day = step % 24)")
    type: Literal["CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"]
    amount: float = Field(ge=0)
    nameOrig: str | None = None
    oldbalanceOrg: float = Field(ge=0)
    newbalanceOrig: float = Field(ge=0)
    nameDest: str | None = None
    oldbalanceDest: float = Field(ge=0)
    newbalanceDest: float = Field(ge=0)

    model_config = {"json_schema_extra": {"example": {
        "step": 410, "type": "TRANSFER", "amount": 181000.0, "nameOrig": "C1305486145",
        "oldbalanceOrg": 181000.0, "newbalanceOrig": 0.0, "nameDest": "C553264065",
        "oldbalanceDest": 0.0, "newbalanceDest": 0.0}}}


class Prediction(BaseModel):
    tx_id: str
    fraud_probability: float
    is_fraud: bool
    threshold: float
    model_version: str


class BatchRequest(BaseModel):
    transactions: list[Transaction] = Field(min_length=1, max_length=1000)


class BatchResponse(BaseModel):
    predictions: list[Prediction]
    model_version: str
    latency_ms: float


# ---------------- Model holder with hot reload ----------------
class ModelHolder:
    def __init__(self):
        self.lock = threading.Lock()
        self.artifact = None
        self.metadata: dict = {}
        self.version: str | None = None

    def load(self, force: bool = False) -> bool:
        latest = current_version(config.MODEL_DIR)
        if latest is None:
            return False
        if latest == self.version and not force:
            return False
        artifact, metadata = load_model(config.MODEL_DIR, latest)
        with self.lock:
            old = self.version
            self.artifact, self.metadata, self.version = artifact, metadata, latest
        MODEL_LOADED.set(1)
        if old and old != latest:
            MODEL_INFO.remove(old)
        MODEL_INFO.labels(version=latest).set(1)
        MODEL_THRESHOLD.set(artifact["threshold"])
        log.info("model loaded", extra={"version": latest, "previous": old})
        return True

    def predict(self, txs: list[Transaction]) -> list[Prediction]:
        with self.lock:
            artifact, version = self.artifact, self.version
        if artifact is None:
            raise HTTPException(503, "model not loaded yet")
        df = pd.DataFrame([t.model_dump() for t in txs])
        proba, flags = score(artifact, df)
        out = []
        for t, p, f in zip(txs, proba, flags, strict=True):
            PREDICTIONS.labels(label="fraud" if f else "legit").inc()
            FRAUD_PROBA.observe(float(p))
            out.append(Prediction(tx_id=t.tx_id or str(uuid.uuid4()), fraud_probability=round(float(p), 6),
                                  is_fraud=bool(f), threshold=artifact["threshold"], model_version=version))
        return out


holder = ModelHolder()


def _watch_for_new_models():
    while True:
        time.sleep(RELOAD_CHECK_SECONDS)
        try:
            holder.load()
        except Exception:  # never kill the watcher
            log.exception("model reload failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        holder.load(force=True)
    except FileNotFoundError:
        log.warning("no model yet - waiting for trainer")
    threading.Thread(target=_watch_for_new_models, daemon=True).start()
    yield


app = FastAPI(title="FraudShield Scoring API", version="1.0.0",
              description="Real-time fraud scoring for payment transactions", lifespan=lifespan)


@app.middleware("http")
async def metrics_and_logging(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - start
        # label by route template, not raw path: bots probing random URLs must not explode metric cardinality
        route = request.scope.get("route")
        endpoint = getattr(route, "path", "unmatched")
        if endpoint != "/metrics":
            LATENCY.labels(endpoint=endpoint).observe(elapsed)
            REQUESTS.labels(endpoint=endpoint, method=request.method, status=str(status)).inc()
            if status >= 400 or not endpoint.startswith("/predict"):  # don't log every prediction
                log.info("request", extra={"path": endpoint, "status": status,
                                           "latency_ms": round(elapsed * 1000, 2)})


@app.get("/health")
def health():
    if holder.artifact is None:
        raise HTTPException(503, "model not loaded")
    return {"status": "ok", "model_version": holder.version}


@app.get("/model/info")
def model_info():
    if holder.artifact is None:
        raise HTTPException(503, "model not loaded")
    return holder.metadata


@app.post("/predict", response_model=Prediction)
def predict(tx: Transaction):
    return holder.predict([tx])[0]


@app.post("/predict/batch", response_model=BatchResponse)
def predict_batch(req: BatchRequest):
    start = time.perf_counter()
    preds = holder.predict(req.transactions)
    return BatchResponse(predictions=preds, model_version=holder.version,
                         latency_ms=round((time.perf_counter() - start) * 1000, 2))


@app.post("/admin/reload")
def reload_model(x_admin_token: str | None = Header(default=None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "bad admin token")
    changed = holder.load(force=True)
    return {"reloaded": changed, "model_version": holder.version}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


logging.getLogger("uvicorn.error").setLevel("INFO")
