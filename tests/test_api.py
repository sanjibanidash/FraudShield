import pytest

pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")
from fastapi.testclient import TestClient  # noqa: E402

FRAUDY = {"step": 410, "type": "TRANSFER", "amount": 181000.0, "nameOrig": "C1", "oldbalanceOrg": 181000.0,
          "newbalanceOrig": 0.0, "nameDest": "C2", "oldbalanceDest": 0.0, "newbalanceDest": 0.0}
NORMAL = {"step": 410, "type": "PAYMENT", "amount": 1200.0, "nameOrig": "C3", "oldbalanceOrg": 50000.0,
          "newbalanceOrig": 48800.0, "nameDest": "M4", "oldbalanceDest": 0.0, "newbalanceDest": 0.0}


@pytest.fixture()
def client(model_dir, monkeypatch):
    from fraudshield_common import config

    monkeypatch.setattr(config, "MODEL_DIR", model_dir)
    import main  # imported once (re-importing would re-register Prometheus metrics)

    with TestClient(main.app) as c:  # lifespan loads the model from the patched MODEL_DIR
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_predict_single(client):
    r = client.post("/predict", json=FRAUDY)
    assert r.status_code == 200
    body = r.json()
    assert 0 <= body["fraud_probability"] <= 1
    assert body["model_version"].startswith("v")


def test_fraud_pattern_scores_higher_than_normal_payment(client):
    p_fraud = client.post("/predict", json=FRAUDY).json()["fraud_probability"]
    p_normal = client.post("/predict", json=NORMAL).json()["fraud_probability"]
    assert p_fraud > p_normal


def test_batch_keeps_ids(client):
    r = client.post("/predict/batch", json={"transactions": [{**FRAUDY, "tx_id": "a"}, {**NORMAL, "tx_id": "b"}]})
    assert r.status_code == 200
    assert [p["tx_id"] for p in r.json()["predictions"]] == ["a", "b"]


def test_validation_rejects_bad_input(client):
    assert client.post("/predict", json={**NORMAL, "type": "BITCOIN"}).status_code == 422
    assert client.post("/predict", json={**NORMAL, "amount": -5}).status_code == 422
    assert client.post("/predict/batch", json={"transactions": []}).status_code == 422


def test_metrics_exposed(client):
    client.post("/predict", json=NORMAL)
    text = client.get("/metrics").text
    assert "api_predictions_total" in text and "api_request_latency_seconds" in text


def test_admin_reload(client):
    r = client.post("/admin/reload", headers={"x-admin-token": "anything"})
    assert r.status_code in (200, 401)
