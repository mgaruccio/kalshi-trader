"""FastAPI REST API for the trading dashboard.

Thin read-only wrappers around db.py functions. No business logic.
"""
import argparse
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import (
    get_connection,
    get_latest_evaluations,
    get_positions,
    get_desired_orders,
    get_recent_fills,
    get_recent_orders,
    get_heartbeats,
    get_danger_exited,
    get_config,
    get_recent_events,
    get_forecasts,
)

log = logging.getLogger(__name__)


def _query(db_path: str, func, *args, **kwargs):
    """Open connection, call func, close. Simple and safe."""
    conn = get_connection(Path(db_path))
    try:
        return func(conn, *args, **kwargs)
    finally:
        conn.close()


def create_app(db_path: str = "data/trading.db") -> FastAPI:
    app = FastAPI(title="Kalshi Weather Dashboard")

    @app.get("/api/health")
    def health():
        heartbeats = _query(db_path, get_heartbeats)
        return {"heartbeats": heartbeats}

    @app.get("/api/evaluations/latest")
    def evaluations_latest():
        return _query(db_path, get_latest_evaluations)

    @app.get("/api/positions")
    def positions():
        return _query(db_path, get_positions)

    @app.get("/api/orders/desired")
    def desired_orders(status: str = "pending"):
        return _query(db_path, get_desired_orders, status=status)

    @app.get("/api/fills")
    def fills(limit: int = 50):
        return _query(db_path, get_recent_fills, limit=limit)

    @app.get("/api/orders")
    def orders(status: str | None = None, limit: int = 50):
        return _query(db_path, get_recent_orders, status=status, limit=limit)

    @app.get("/api/danger-exited")
    def danger_exited():
        tickers = _query(db_path, get_danger_exited)
        return {"tickers": list(tickers)}

    @app.get("/api/events")
    def events(limit: int = 100):
        return _query(db_path, get_recent_events, limit=limit)

    @app.get("/api/config")
    def config():
        return _query(db_path, get_config)

    @app.get("/api/forecasts")
    def forecasts():
        return _query(db_path, get_forecasts)

    # Static files + dashboard
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    def dashboard():
        index = Path(__file__).parent / "static" / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse(
            status_code=404,
            content={"message": "Dashboard not yet deployed. API available at /api/"},
        )

    return app


# Module-level app for uvicorn — respects TRADING_DB env var
def _get_app() -> FastAPI:
    db_path = os.environ.get("TRADING_DB", "data/trading.db")
    return create_app(db_path)


app = _get_app()


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description="Trading Dashboard API")
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port)
