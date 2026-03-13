"""Tests for db.py — SQLite schema and access layer."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_trading.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    c = db.get_connection(db_path)
    yield c
    c.close()


def test_init_db_creates_tables(db_path):
    """init_db creates all 11 tables and enables WAL mode."""
    c = db.get_connection(db_path)
    try:
        row = c.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

        tables = {
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "evaluations",
            "desired_orders",
            "positions",
            "orders",
            "fills",
            "forecasts",
            "markets",
            "heartbeats",
            "events",
            "config",
            "danger_exited",
        }
        assert expected.issubset(tables)
    finally:
        c.close()


def test_write_and_read_evaluation(conn):
    """Write one evaluation row, read it back, verify fields."""
    rows = [
        {
            "cycle_id": "cycle-001",
            "cycle_ts": "2026-03-13T08:00:00",
            "ticker": "KXHIGHNY-26MAR13-T52",
            "city": "NYC",
            "direction": "above",
            "side": "NO",
            "threshold": 52.0,
            "settlement_date": "2026-03-13",
            "ecmwf": 49.5,
            "gfs": 50.1,
            "margin": -2.0,
            "consensus": 50.1,
            "p_win": 0.87,
            "model_scores": '{"emos": 0.85, "ngboost": 0.88}',
            "filter_result": "pass",
            "filter_reason": None,
            "features": '{"lead_hours": 24}',
        }
    ]
    db.write_evaluations(conn, rows)
    conn.commit()

    evals = db.get_latest_evaluations(conn, cycle_id="cycle-001")
    assert len(evals) == 1
    e = evals[0]
    assert e["ticker"] == "KXHIGHNY-26MAR13-T52"
    assert e["city"] == "NYC"
    assert e["p_win"] == pytest.approx(0.87)
    assert e["filter_result"] == "pass"
    assert e["model_scores"] == '{"emos": 0.85, "ngboost": 0.88}'


def test_upsert_position(conn):
    """Insert position, upsert with new qty, verify updated."""
    db.upsert_position(
        conn,
        ticker="KXHIGHNY-26MAR13-T52",
        side="NO",
        contracts=5,
        avg_price_cents=45,
        city="NYC",
        threshold=52.0,
    )
    conn.commit()

    positions = db.get_positions(conn)
    assert len(positions) == 1
    assert positions[0]["contracts"] == 5

    # Upsert with updated qty
    db.upsert_position(
        conn,
        ticker="KXHIGHNY-26MAR13-T52",
        side="NO",
        contracts=10,
        avg_price_cents=44,
        city="NYC",
        threshold=52.0,
    )
    conn.commit()

    positions = db.get_positions(conn)
    assert len(positions) == 1
    assert positions[0]["contracts"] == 10
    assert positions[0]["avg_price_cents"] == pytest.approx(44)


def test_write_heartbeat(conn):
    """Write heartbeat, read back, verify timestamp."""
    db.beat_heartbeat(conn, "evaluator", status="ok", message="all good")
    conn.commit()

    heartbeats = db.get_heartbeats(conn)
    assert len(heartbeats) == 1
    hb = heartbeats[0]
    assert hb["process_name"] == "evaluator"
    assert hb["status"] == "ok"
    assert hb["message"] == "all good"
    assert hb["last_beat"] is not None

    # Update heartbeat (upsert)
    db.beat_heartbeat(conn, "evaluator", status="warn", message="slow")
    conn.commit()

    heartbeats = db.get_heartbeats(conn)
    assert len(heartbeats) == 1
    assert heartbeats[0]["status"] == "warn"


def test_upsert_forecast(conn):
    """Insert forecast, upsert with new values, verify UNIQUE(city, date)."""
    db.upsert_forecast(
        conn,
        city="NYC",
        date="2026-03-13",
        ecmwf=50.0,
        gfs=51.0,
        icon=50.5,
        model_std=0.5,
        model_range=1.0,
    )
    conn.commit()

    # Verify initial insert
    row = conn.execute(
        "SELECT * FROM forecasts WHERE city='NYC' AND date='2026-03-13'"
    ).fetchone()
    assert row is not None
    assert row["ecmwf"] == pytest.approx(50.0)

    # Upsert with new values
    db.upsert_forecast(
        conn,
        city="NYC",
        date="2026-03-13",
        ecmwf=52.0,
        gfs=53.0,
        icon=52.5,
        model_std=0.6,
        model_range=1.0,
    )
    conn.commit()

    rows = conn.execute("SELECT * FROM forecasts WHERE city='NYC'").fetchall()
    assert len(rows) == 1
    assert rows[0]["ecmwf"] == pytest.approx(52.0)


def test_write_desired_orders_supersedes_previous(conn):
    """Write cycle 1 orders, write cycle 2, verify cycle 1 marked superseded."""
    cycle1_orders = [
        {"ticker": "KXHIGHNY-26MAR13-T52", "side": "NO", "price_cents": 45, "qty": 5, "reason": "p_win=0.87"},
        {"ticker": "KXHIGHCHI-26MAR13-T48", "side": "NO", "price_cents": 55, "qty": 3, "reason": "p_win=0.91"},
    ]
    db.write_desired_orders(conn, "cycle-001", cycle1_orders)
    conn.commit()

    pending = db.get_desired_orders(conn, status="pending")
    assert len(pending) == 2

    cycle2_orders = [
        {"ticker": "KXHIGHDCA-26MAR13-T60", "side": "NO", "price_cents": 40, "qty": 4, "reason": "p_win=0.93"},
    ]
    db.write_desired_orders(conn, "cycle-002", cycle2_orders)
    conn.commit()

    # Cycle 1 orders should be superseded
    superseded = db.get_desired_orders(conn, status="superseded")
    assert len(superseded) == 2
    assert all(o["cycle_id"] == "cycle-001" for o in superseded)

    # Only cycle 2 orders are pending
    pending = db.get_desired_orders(conn, status="pending")
    assert len(pending) == 1
    assert pending[0]["cycle_id"] == "cycle-002"


def test_write_and_read_fill(conn):
    """Write a fill, read it back."""
    db.write_fill(
        conn,
        trade_id="trade-abc-123",
        client_order_id="ord-001",
        venue_order_id="kalshi-999",
        ticker="KXHIGHNY-26MAR13-T52",
        side="NO",
        action="buy",
        price_cents=45,
        qty=5,
        fee_cents=1.5,
        pnl_cents=None,
    )
    conn.commit()

    fills = db.get_recent_fills(conn, limit=10)
    assert len(fills) == 1
    f = fills[0]
    assert f["ticker"] == "KXHIGHNY-26MAR13-T52"
    assert f["price_cents"] == 45
    assert f["qty"] == 5
    assert f["fee_cents"] == pytest.approx(1.5)
    assert f["action"] == "buy"


def test_write_danger_exited(conn):
    """Write danger exited ticker, verify in get_danger_exited set."""
    db.write_danger_exited(
        conn,
        ticker="KXHIGHNY-26MAR13-T52",
        reason="p_win dropped below danger threshold",
        rule_name="dual_model_consensus",
        p_win=0.65,
    )
    conn.commit()

    danger_set = db.get_danger_exited(conn)
    assert "KXHIGHNY-26MAR13-T52" in danger_set

    # INSERT OR IGNORE — duplicate write should not raise
    db.write_danger_exited(
        conn,
        ticker="KXHIGHNY-26MAR13-T52",
        reason="second write",
        rule_name="dual_model_consensus",
        p_win=0.60,
    )
    conn.commit()

    danger_set = db.get_danger_exited(conn)
    assert len(danger_set) == 1


def test_log_event(conn):
    """Log an event, read recent events."""
    db.log_event(conn, source="evaluator", level="info", message="cycle started")
    db.log_event(conn, source="executor", level="warn", message="order rejected")
    conn.commit()

    events = db.get_recent_events(conn, limit=100)
    assert len(events) == 2
    sources = {e["source"] for e in events}
    assert "evaluator" in sources
    assert "executor" in sources


def test_get_config_and_set_config(conn):
    """Set config key, get it back."""
    db.set_config(conn, "min_p_win", "0.90")
    db.set_config(conn, "max_position_per_ticker", "30")
    conn.commit()

    cfg = db.get_config(conn)
    assert cfg["min_p_win"] == "0.90"
    assert cfg["max_position_per_ticker"] == "30"

    # Upsert — update existing key
    db.set_config(conn, "min_p_win", "0.92")
    conn.commit()

    cfg = db.get_config(conn)
    assert cfg["min_p_win"] == "0.92"


def test_get_latest_evaluations_returns_most_recent_cycle(conn):
    """Write 2 cycles of evals, get_latest returns only the most recent."""
    cycle1_rows = [
        {
            "cycle_id": "cycle-001",
            "cycle_ts": "2026-03-13T08:00:00",
            "ticker": "KXHIGHNY-26MAR13-T52",
            "city": "NYC",
            "direction": "above",
            "side": "NO",
            "threshold": 52.0,
            "settlement_date": "2026-03-13",
            "ecmwf": 49.5,
            "gfs": 50.1,
            "margin": -2.0,
            "consensus": 50.1,
            "p_win": 0.85,
            "model_scores": "{}",
            "filter_result": "pass",
            "filter_reason": None,
            "features": "{}",
        }
    ]
    cycle2_rows = [
        {
            "cycle_id": "cycle-002",
            "cycle_ts": "2026-03-13T08:05:00",
            "ticker": "KXHIGHCHI-26MAR13-T48",
            "city": "CHI",
            "direction": "above",
            "side": "NO",
            "threshold": 48.0,
            "settlement_date": "2026-03-13",
            "ecmwf": 45.0,
            "gfs": 46.0,
            "margin": -2.0,
            "consensus": 46.0,
            "p_win": 0.91,
            "model_scores": "{}",
            "filter_result": "pass",
            "filter_reason": None,
            "features": "{}",
        },
        {
            "cycle_id": "cycle-002",
            "cycle_ts": "2026-03-13T08:05:00",
            "ticker": "KXHIGHDCA-26MAR13-T60",
            "city": "DCA",
            "direction": "above",
            "side": "NO",
            "threshold": 60.0,
            "settlement_date": "2026-03-13",
            "ecmwf": 57.0,
            "gfs": 58.0,
            "margin": -2.0,
            "consensus": 58.0,
            "p_win": 0.93,
            "model_scores": "{}",
            "filter_result": "pass",
            "filter_reason": None,
            "features": "{}",
        },
    ]

    db.write_evaluations(conn, cycle1_rows)
    db.write_evaluations(conn, cycle2_rows)
    conn.commit()

    # Without cycle_id, should return latest cycle (cycle-002)
    latest = db.get_latest_evaluations(conn)
    assert len(latest) == 2
    assert all(e["cycle_id"] == "cycle-002" for e in latest)

    # With explicit cycle_id
    cycle1 = db.get_latest_evaluations(conn, cycle_id="cycle-001")
    assert len(cycle1) == 1
    assert cycle1[0]["ticker"] == "KXHIGHNY-26MAR13-T52"


def test_write_and_read_order(conn):
    """Write an order, read it back via get_recent_orders."""
    db.write_order(
        conn,
        client_order_id="ord-001",
        venue_order_id="kalshi-555",
        ticker="KXHIGHNY-26MAR13-T52",
        side="NO",
        order_side="buy",
        price_cents=45,
        qty=5,
        status="submitted",
    )
    conn.commit()

    orders = db.get_recent_orders(conn)
    assert len(orders) == 1
    o = orders[0]
    assert o["client_order_id"] == "ord-001"
    assert o["venue_order_id"] == "kalshi-555"
    assert o["ticker"] == "KXHIGHNY-26MAR13-T52"
    assert o["side"] == "NO"
    assert o["order_side"] == "buy"
    assert o["price_cents"] == 45
    assert o["qty"] == 5
    assert o["status"] == "submitted"
    assert o["created_at"] is not None


def test_get_recent_orders_with_status_filter(conn):
    """get_recent_orders filters by status when provided."""
    db.write_order(
        conn,
        client_order_id="ord-001",
        ticker="KXHIGHNY-26MAR13-T52",
        side="NO",
        order_side="buy",
        price_cents=45,
        qty=5,
        status="submitted",
    )
    db.write_order(
        conn,
        client_order_id="ord-002",
        ticker="KXHIGHCHI-26MAR13-T48",
        side="NO",
        order_side="buy",
        price_cents=55,
        qty=3,
        status="filled",
    )
    db.write_order(
        conn,
        client_order_id="ord-003",
        ticker="KXHIGHDCA-26MAR13-T60",
        side="NO",
        order_side="buy",
        price_cents=40,
        qty=4,
        status="filled",
    )
    conn.commit()

    # No filter — all 3 returned
    all_orders = db.get_recent_orders(conn)
    assert len(all_orders) == 3

    # Filter by "submitted"
    submitted = db.get_recent_orders(conn, status="submitted")
    assert len(submitted) == 1
    assert submitted[0]["client_order_id"] == "ord-001"

    # Filter by "filled"
    filled = db.get_recent_orders(conn, status="filled")
    assert len(filled) == 2
    client_ids = {o["client_order_id"] for o in filled}
    assert client_ids == {"ord-002", "ord-003"}
