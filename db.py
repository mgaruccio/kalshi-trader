"""SQLite schema and access layer for the DB-backed trading architecture.

WAL mode for concurrent reads. All writes go through named functions.
Two processes share this DB: the evaluator (ML inference) and the NT executor
(order management). A FastAPI dashboard reads it for observability.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    cycle_ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    city TEXT,
    direction TEXT,
    side TEXT,
    threshold REAL,
    settlement_date TEXT,
    ecmwf REAL,
    gfs REAL,
    margin REAL,
    consensus REAL,
    p_win REAL,
    model_scores TEXT,
    filter_result TEXT,
    filter_reason TEXT,
    features TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS desired_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    reason TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    contracts INTEGER NOT NULL DEFAULT 0,
    avg_price_cents REAL,
    city TEXT,
    threshold REAL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT,
    venue_order_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    order_side TEXT NOT NULL,
    price_cents INTEGER,
    qty INTEGER,
    status TEXT DEFAULT 'submitted',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    client_order_id TEXT,
    venue_order_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT,
    price_cents INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    fee_cents REAL DEFAULT 0,
    pnl_cents REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS forecasts (
    city TEXT NOT NULL,
    date TEXT NOT NULL,
    ecmwf REAL,
    gfs REAL,
    icon REAL,
    model_std REAL,
    model_range REAL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (city, date)
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    city TEXT,
    direction TEXT,
    threshold REAL,
    settlement_date TEXT,
    yes_bid INTEGER,
    yes_ask INTEGER,
    no_bid INTEGER,
    no_ask INTEGER,
    status TEXT DEFAULT 'open',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS heartbeats (
    process_name TEXT PRIMARY KEY,
    status TEXT DEFAULT 'ok',
    message TEXT DEFAULT '',
    last_beat TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    level TEXT DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS danger_exited (
    ticker TEXT PRIMARY KEY,
    reason TEXT,
    rule_name TEXT,
    p_win REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def init_db(path: Path) -> None:
    """Create all tables and enable WAL mode. Safe to call on existing DB."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def get_connection(path: Path) -> sqlite3.Connection:
    """Open a connection with WAL mode and Row factory enabled."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Write functions
# ---------------------------------------------------------------------------

def write_evaluations(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Batch insert evaluation rows."""
    conn.executemany(
        """
        INSERT INTO evaluations
            (cycle_id, cycle_ts, ticker, city, direction, side, threshold,
             settlement_date, ecmwf, gfs, margin, consensus, p_win,
             model_scores, filter_result, filter_reason, features)
        VALUES
            (:cycle_id, :cycle_ts, :ticker, :city, :direction, :side,
             :threshold, :settlement_date, :ecmwf, :gfs, :margin,
             :consensus, :p_win, :model_scores, :filter_result,
             :filter_reason, :features)
        """,
        rows,
    )


def write_desired_orders(
    conn: sqlite3.Connection, cycle_id: str, orders: list[dict]
) -> None:
    """Mark all previous 'pending' desired_orders as 'superseded', then insert new ones."""
    conn.execute(
        "UPDATE desired_orders SET status='superseded' WHERE status='pending'"
    )
    for order in orders:
        conn.execute(
            """
            INSERT INTO desired_orders (cycle_id, ticker, side, price_cents, qty, reason)
            VALUES (:cycle_id, :ticker, :side, :price_cents, :qty, :reason)
            """,
            {
                "cycle_id": cycle_id,
                "ticker": order["ticker"],
                "side": order["side"],
                "price_cents": order["price_cents"],
                "qty": order["qty"],
                "reason": order.get("reason"),
            },
        )


def upsert_position(conn: sqlite3.Connection, ticker: str, **fields) -> None:
    """Insert or replace a position row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO positions
            (ticker, side, contracts, avg_price_cents, city, threshold, updated_at)
        VALUES
            (:ticker, :side, :contracts, :avg_price_cents, :city, :threshold, :updated_at)
        """,
        {
            "ticker": ticker,
            "side": fields.get("side"),
            "contracts": fields.get("contracts", 0),
            "avg_price_cents": fields.get("avg_price_cents"),
            "city": fields.get("city"),
            "threshold": fields.get("threshold"),
            "updated_at": now,
        },
    )


def upsert_forecast(conn: sqlite3.Connection, city: str, date: str, **fields) -> None:
    """Insert or replace a forecast row. UNIQUE constraint on (city, date)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO forecasts
            (city, date, ecmwf, gfs, icon, model_std, model_range, updated_at)
        VALUES
            (:city, :date, :ecmwf, :gfs, :icon, :model_std, :model_range, :updated_at)
        """,
        {
            "city": city,
            "date": date,
            "ecmwf": fields.get("ecmwf"),
            "gfs": fields.get("gfs"),
            "icon": fields.get("icon"),
            "model_std": fields.get("model_std"),
            "model_range": fields.get("model_range"),
            "updated_at": now,
        },
    )


def upsert_market(conn: sqlite3.Connection, ticker: str, **fields) -> None:
    """Insert or replace a market row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO markets
            (ticker, city, direction, threshold, settlement_date,
             yes_bid, yes_ask, no_bid, no_ask, status, updated_at)
        VALUES
            (:ticker, :city, :direction, :threshold, :settlement_date,
             :yes_bid, :yes_ask, :no_bid, :no_ask, :status, :updated_at)
        """,
        {
            "ticker": ticker,
            "city": fields.get("city"),
            "direction": fields.get("direction"),
            "threshold": fields.get("threshold"),
            "settlement_date": fields.get("settlement_date"),
            "yes_bid": fields.get("yes_bid"),
            "yes_ask": fields.get("yes_ask"),
            "no_bid": fields.get("no_bid"),
            "no_ask": fields.get("no_ask"),
            "status": fields.get("status", "open"),
            "updated_at": now,
        },
    )


def write_order(conn: sqlite3.Connection, **fields) -> None:
    """Insert an order row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO orders
            (client_order_id, venue_order_id, ticker, side, order_side,
             price_cents, qty, status, created_at, updated_at)
        VALUES
            (:client_order_id, :venue_order_id, :ticker, :side, :order_side,
             :price_cents, :qty, :status, :created_at, :updated_at)
        """,
        {
            "client_order_id": fields.get("client_order_id"),
            "venue_order_id": fields.get("venue_order_id"),
            "ticker": fields["ticker"],
            "side": fields["side"],
            "order_side": fields["order_side"],
            "price_cents": fields.get("price_cents"),
            "qty": fields.get("qty"),
            "status": fields.get("status", "submitted"),
            "created_at": now,
            "updated_at": now,
        },
    )


def write_fill(conn: sqlite3.Connection, **fields) -> None:
    """Insert a fill row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO fills
            (trade_id, client_order_id, venue_order_id, ticker, side,
             action, price_cents, qty, fee_cents, pnl_cents, created_at)
        VALUES
            (:trade_id, :client_order_id, :venue_order_id, :ticker, :side,
             :action, :price_cents, :qty, :fee_cents, :pnl_cents, :created_at)
        """,
        {
            "trade_id": fields.get("trade_id"),
            "client_order_id": fields.get("client_order_id"),
            "venue_order_id": fields.get("venue_order_id"),
            "ticker": fields["ticker"],
            "side": fields["side"],
            "action": fields.get("action"),
            "price_cents": fields["price_cents"],
            "qty": fields["qty"],
            "fee_cents": fields.get("fee_cents", 0),
            "pnl_cents": fields.get("pnl_cents"),
            "created_at": now,
        },
    )


def write_danger_exited(
    conn: sqlite3.Connection,
    ticker: str,
    reason: str,
    rule_name: str,
    p_win: float | None,
) -> None:
    """Record a danger-exited ticker. INSERT OR IGNORE — first write wins."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO danger_exited (ticker, reason, rule_name, p_win, created_at)
        VALUES (:ticker, :reason, :rule_name, :p_win, :created_at)
        """,
        {
            "ticker": ticker,
            "reason": reason,
            "rule_name": rule_name,
            "p_win": p_win,
            "created_at": now,
        },
    )


def beat_heartbeat(
    conn: sqlite3.Connection,
    process_name: str,
    status: str = "ok",
    message: str = "",
) -> None:
    """Upsert a heartbeat row for a named process."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO heartbeats (process_name, status, message, last_beat)
        VALUES (:process_name, :status, :message, :last_beat)
        """,
        {
            "process_name": process_name,
            "status": status,
            "message": message,
            "last_beat": now,
        },
    )


def log_event(
    conn: sqlite3.Connection, source: str, level: str, message: str
) -> None:
    """Append an event row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO events (source, level, message, created_at)
        VALUES (:source, :level, :message, :created_at)
        """,
        {"source": source, "level": level, "message": message, "created_at": now},
    )


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert or replace a config key-value pair."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO config (key, value, updated_at)
        VALUES (:key, :value, :updated_at)
        """,
        {"key": key, "value": value, "updated_at": now},
    )


# ---------------------------------------------------------------------------
# Read functions
# ---------------------------------------------------------------------------

def get_latest_evaluations(
    conn: sqlite3.Connection, cycle_id: str | None = None
) -> list[dict]:
    """Return evaluations for a cycle.

    If cycle_id is None, finds the most recent cycle_id by created_at and
    returns all rows for that cycle.
    """
    if cycle_id is None:
        row = conn.execute(
            "SELECT cycle_id FROM evaluations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return []
        cycle_id = row["cycle_id"]

    rows = conn.execute(
        "SELECT * FROM evaluations WHERE cycle_id = ? ORDER BY created_at",
        (cycle_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_positions(conn: sqlite3.Connection) -> list[dict]:
    """Return all position rows."""
    rows = conn.execute(
        "SELECT * FROM positions ORDER BY ticker"
    ).fetchall()
    return [dict(r) for r in rows]


def get_desired_orders(
    conn: sqlite3.Connection, status: str | None = None
) -> list[dict]:
    """Return desired_orders, optionally filtered by status."""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM desired_orders WHERE status = ? ORDER BY created_at",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM desired_orders ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_fills(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Return the most recent fill rows."""
    rows = conn.execute(
        "SELECT * FROM fills ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_orders(
    conn: sqlite3.Connection, status: str | None = None, limit: int = 50
) -> list[dict]:
    """Return recent order rows, optionally filtered by status."""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_heartbeats(conn: sqlite3.Connection) -> list[dict]:
    """Return all heartbeat rows."""
    rows = conn.execute(
        "SELECT * FROM heartbeats ORDER BY process_name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_danger_exited(conn: sqlite3.Connection) -> set[str]:
    """Return the set of tickers that have been danger-exited."""
    rows = conn.execute("SELECT ticker FROM danger_exited").fetchall()
    return {r["ticker"] for r in rows}


def get_config(conn: sqlite3.Connection) -> dict[str, str]:
    """Return all config key-value pairs as a dict."""
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_recent_events(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Return the most recent event rows."""
    rows = conn.execute(
        "SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_forecasts(conn: sqlite3.Connection) -> list[dict]:
    """Return all forecast rows ordered by date desc, city."""
    rows = conn.execute(
        "SELECT * FROM forecasts ORDER BY date DESC, city"
    ).fetchall()
    return [dict(r) for r in rows]
