import pytest

pytest.importorskip("flask")
pytest.importorskip("prometheus_client")


def test_format_alert():
    import app as dashboard

    msg = dashboard.format_alert(
        {"labels": {"alertname": "DataDriftDetected", "severity": "warning"},
         "annotations": {"summary": "drift!"}}, "firing")
    assert "FIRING" in msg and "DataDriftDetected" in msg and "WARNING" in msg


def test_template_filters():
    import app as dashboard

    assert dashboard.pct(0.1234) == "12.3%"
    assert dashboard.money(1234.5) == "1,234.50"
    assert dashboard.pct(None) == "-"


def test_controls_require_password(monkeypatch):
    import app as dashboard

    monkeypatch.setattr(dashboard, "DASHBOARD_PASSWORD", "secret")
    r = dashboard.app.test_client().get("/controls")
    assert r.status_code == 401
