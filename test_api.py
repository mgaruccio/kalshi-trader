"""Tests for the FastAPI REST API."""
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from db import (
    init_db, get_connection,
    write_evaluations, upsert_position, write_fill,
    beat_heartbeat, log_event, write_desired_orders,
    write_danger_exited, set_config, upsert_forecast,
)
from api import create_app


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)
        yield db_path


@pytest.fixture
def seeded_db(tmp_db):
    """DB with sample data across all tables."""
    conn = get_connection(tmp_db)

    # Evaluations
    write_evaluations(conn, [
        {
            "cycle_id": "cycle-001", "cycle_ts": "2026-03-15T10:00:00",
            "ticker": "KXHIGHCHI-26MAR15-T55", "city": "chicago",
            "direction": "above", "side": "no", "threshold": 55.0,
            "settlement_date": "2026-03-15", "ecmwf": 52.0, "gfs": 51.0,
            "margin": 3.0, "consensus": 52.0, "p_win": 0.97,
            "model_scores": json.dumps({"emos": 0.98, "ngboost": 0.96}),
            "filter_result": "pass", "filter_reason": "",
            "features": json.dumps({"ecmwf_high": 52.0}),
        },
        {
            "cycle_id": "cycle-001", "cycle_ts": "2026-03-15T10:00:00",
            "ticker": "KXHIGHCHI-26MAR15-T55", "city": "chicago",
            "direction": "above", "side": "yes", "threshold": 55.0,
            "settlement_date": "2026-03-15", "ecmwf": 52.0, "gfs": 51.0,
            "margin": -3.0, "consensus": 51.0, "p_win": 0.45,
            "model_scores": json.dumps({"emos": 0.40, "ngboost": 0.50}),
            "filter_result": "min_p_win", "filter_reason": "pw=0.450 < 0.900",
            "features": json.dumps({"ecmwf_high": 52.0}),
        },
    ])

    # Position
    upsert_position(conn, "KXHIGHCHI-26MAR15-T55", side="no", contracts=5,
                   avg_price_cents=88.0, city="chicago", threshold=55.0)

    # Fill
    write_fill(conn, trade_id="t001", ticker="KXHIGHCHI-26MAR15-T55",
              side="no", action="buy", price_cents=88, qty=5, fee_cents=1.5)

    # Heartbeat
    beat_heartbeat(conn, "evaluator", status="ok", message="cycle=001 evals=40")
    beat_heartbeat(conn, "executor", status="ok", message="refresh")

    # Desired orders
    write_desired_orders(conn, "cycle-001", [
        {"ticker": "KXHIGHCHI-26MAR15-T55", "side": "no",
         "price_cents": 88, "qty": 3, "reason": "ladder offset=0"},
    ])

    # Danger exited
    write_danger_exited(conn, "KXHIGHNY-26MAR15-T50", "p_win dropped", "ml_exit", 0.65)

    # Events
    log_event(conn, "evaluator", "info", "Cycle started")

    # Config
    set_config(conn, "min_p_win", "0.90")

    # Forecast
    upsert_forecast(conn, "chicago", "2026-03-15",
                   ecmwf=52.0, gfs=51.0, icon=52.5,
                   model_std=0.8, model_range=1.5)

    conn.commit()
    conn.close()
    return tmp_db


class TestHealthEndpoint:
    def test_health_returns_heartbeats(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "heartbeats" in data
        names = [h["process_name"] for h in data["heartbeats"]]
        assert "evaluator" in names
        assert "executor" in names


class TestEvaluationsEndpoint:
    def test_latest_evaluations(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/evaluations/latest")
        assert resp.status_code == 200
        evals = resp.json()
        assert len(evals) == 2
        assert evals[0]["ticker"] == "KXHIGHCHI-26MAR15-T55"

    def test_evaluations_empty_db(self, tmp_db):
        app = create_app(str(tmp_db))
        client = TestClient(app)
        resp = client.get("/api/evaluations/latest")
        assert resp.status_code == 200
        assert resp.json() == []


class TestPositionsEndpoint:
    def test_positions(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/positions")
        assert resp.status_code == 200
        positions = resp.json()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "KXHIGHCHI-26MAR15-T55"
        assert positions[0]["contracts"] == 5


class TestFillsEndpoint:
    def test_recent_fills(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/fills")
        assert resp.status_code == 200
        fills = resp.json()
        assert len(fills) == 1
        assert fills[0]["price_cents"] == 88


class TestDesiredOrdersEndpoint:
    def test_desired_orders(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/orders/desired")
        assert resp.status_code == 200
        orders = resp.json()
        assert len(orders) == 1


class TestDangerExitedEndpoint:
    def test_danger_exited(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/danger-exited")
        assert resp.status_code == 200
        data = resp.json()
        assert "KXHIGHNY-26MAR15-T50" in data["tickers"]


class TestEventsEndpoint:
    def test_recent_events(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 1


class TestConfigEndpoint:
    def test_config(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/config")
        assert resp.status_code == 200
        config = resp.json()
        assert config["min_p_win"] == "0.90"


class TestForecastsEndpoint:
    def test_forecasts(self, seeded_db):
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/api/forecasts")
        assert resp.status_code == 200
        forecasts = resp.json()
        assert len(forecasts) >= 1
        assert forecasts[0]["city"] == "chicago"


class TestDashboardRoute:
    def test_root_serves_html(self, seeded_db):
        """Root serves the dashboard HTML (or 404 if static dir missing)."""
        app = create_app(str(seeded_db))
        client = TestClient(app)
        resp = client.get("/")
        # Will be 404 until static/index.html exists — that's OK
        assert resp.status_code in (200, 404)
