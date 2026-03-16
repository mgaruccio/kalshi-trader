"""Live/demo trader script for WeatherMakerStrategy.

Bootstraps a TradingNode with the Kalshi adapter, SignalActor, and
WeatherMakerStrategy. Defaults to the demo exchange. Production requires
the --i-accept-risk flag.

Usage:
    # Demo (safe default)
    uv run python scripts/run_trader.py

    # Production (requires explicit opt-in)
    uv run python scripts/run_trader.py --environment production --i-accept-risk

    # With config overrides
    uv run python scripts/run_trader.py \\
        --environment demo \\
        --signal-url ws://localhost:8000 \\
        --confidence-threshold 0.97 \\
        --ladder-depth 2

Credentials loaded from .env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import urllib.parse

from dotenv import load_dotenv

load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run WeatherMakerStrategy live/demo trader.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--environment",
        choices=["demo", "production"],
        default="demo",
        help="Trading environment (demo or production)",
    )
    parser.add_argument(
        "--signal-url",
        default="http://localhost:8000",
        help="Base URL of the signal server, no trailing path (e.g. http://localhost:8000)",
    )
    parser.add_argument(
        "--i-accept-risk",
        action="store_true",
        default=False,
        help="Required when --environment=production. Acknowledges real-money risk.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Sandbox mode: real market data, simulated execution via SandboxExecutionClient.",
    )
    parser.add_argument(
        "--starting-balance",
        type=int,
        default=20,
        help="Simulated starting capital in USD (only used with --dry-run)",
    )

    # Strategy config overrides (same set as run_backtest.py)
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=None,
        help="Override WeatherMakerConfig.confidence_threshold",
    )
    parser.add_argument(
        "--ladder-depth",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.ladder_depth",
    )
    parser.add_argument(
        "--level-quantity",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.level_quantity",
    )
    parser.add_argument(
        "--exit-price-cents",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.exit_price_cents",
    )
    parser.add_argument(
        "--reprice-threshold",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.reprice_threshold",
    )

    return parser.parse_args()


def _require_production_flag(args: argparse.Namespace) -> None:
    """Abort if production environment is requested without explicit risk acknowledgement.

    Dry-run mode skips this check since no orders are submitted.
    """
    if args.environment == "production" and not args.i_accept_risk and not args.dry_run:
        print(
            "ERROR: --environment=production requires --i-accept-risk flag.\n"
            "       This flag acknowledges that you are trading with real money.\n"
            "       Add --i-accept-risk to proceed.",
            file=sys.stderr,
        )
        sys.exit(1)


def _load_credentials() -> tuple[str, str]:
    """Load Kalshi credentials from environment. Abort if missing."""
    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    missing = []
    if not api_key_id:
        missing.append("KALSHI_API_KEY_ID")
    if not private_key_path:
        missing.append("KALSHI_PRIVATE_KEY_PATH")

    if missing:
        print(
            f"ERROR: Missing required environment variables: {', '.join(missing)}\n"
            "       Set them in your .env file or shell environment.",
            file=sys.stderr,
        )
        sys.exit(1)

    return api_key_id, private_key_path


def _build_strategy_config(args: argparse.Namespace):
    from kalshi.strategy import WeatherMakerConfig

    overrides: dict = {}
    if args.confidence_threshold is not None:
        overrides["confidence_threshold"] = args.confidence_threshold
    if args.ladder_depth is not None:
        overrides["ladder_depth"] = args.ladder_depth
    if args.level_quantity is not None:
        overrides["level_quantity"] = args.level_quantity
    if args.exit_price_cents is not None:
        overrides["exit_price_cents"] = args.exit_price_cents
    if args.reprice_threshold is not None:
        overrides["reprice_threshold"] = args.reprice_threshold

    return WeatherMakerConfig(**overrides)


def _signal_ws_url(http_url: str) -> str:
    """Derive WebSocket URL from HTTP base URL.

    Strips any existing path component so a URL like http://host:8000/extra
    doesn't produce http://host:8000/extra/v1/stream.
    """
    parsed = urllib.parse.urlparse(http_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    base = urllib.parse.urlunparse((ws_scheme, parsed.netloc, "", "", "", ""))
    return base + "/v1/stream"


def _heartbeat_path() -> str:
    return "/tmp/kalshi-trader-heartbeat"


def _portfolio_snapshot_path() -> str:
    return "/tmp/kalshi-trader-portfolio.json"


def _write_portfolio_snapshot(strategy) -> None:
    """Write current portfolio state to JSON for external monitoring."""
    from kalshi.common.constants import KALSHI_VENUE
    from nautilus_trader.model.objects import Currency

    usd = Currency.from_str("USD")

    try:
        account = strategy.portfolio.account(KALSHI_VENUE)
        if account is None:
            return

        usd_balance = account.balances().get(usd)
        if usd_balance is None:
            return

        # Open positions
        positions = []
        for pos in strategy.cache.positions(venue=KALSHI_VENUE):
            if pos.is_closed:
                continue
            qty = int(pos.quantity.as_double())
            if qty <= 0:
                continue

            # Mark-to-market at last bid
            last_tick = strategy.cache.quote_tick(pos.instrument_id)
            bid_price = round(float(last_tick.bid_price) * 100) if last_tick else None
            ask_price = round(float(last_tick.ask_price) * 100) if last_tick else None
            mtm_value = qty * bid_price if bid_price is not None else None

            positions.append({
                "instrument": str(pos.instrument_id),
                "side": str(pos.side),
                "qty": qty,
                "avg_entry_cents": round(pos.avg_px_open * 100),
                "cost_cents": round(pos.avg_px_open * 100) * qty,
                "bid_cents": bid_price,
                "ask_cents": ask_price,
                "mtm_value_cents": mtm_value,
                "unrealized_pnl_cents": (
                    mtm_value - round(pos.avg_px_open * 100) * qty
                    if mtm_value is not None
                    else None
                ),
                "realized_pnl_usd": float(pos.realized_pnl.as_double()),
            })

        total_cost = sum(p["cost_cents"] for p in positions)
        total_mtm = sum(p["mtm_value_cents"] for p in positions if p["mtm_value_cents"] is not None)
        total_unrealized = sum(
            p["unrealized_pnl_cents"] for p in positions if p["unrealized_pnl_cents"] is not None
        )

        snapshot = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "account": {
                "total_usd": float(usd_balance.total.as_double()),
                "locked_usd": float(usd_balance.locked.as_double()),
                "free_usd": float(usd_balance.free.as_double()),
            },
            "positions": sorted(positions, key=lambda p: -p["qty"]),
            "summary": {
                "open_position_count": len(positions),
                "total_contracts": sum(p["qty"] for p in positions),
                "total_cost_cents": total_cost,
                "total_mtm_cents": total_mtm,
                "total_unrealized_pnl_cents": total_unrealized,
                "portfolio_value_cents": round(usd_balance.total.as_double() * 100) + total_mtm,
            },
        }

        tmp = _portfolio_snapshot_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, _portfolio_snapshot_path())

    except Exception as e:
        # Never crash the strategy for a monitoring write
        strategy.log.warning(f"Portfolio snapshot failed: {e}")


def main() -> None:
    args = _parse_args()

    _require_production_flag(args)
    api_key_id, private_key_path = _load_credentials()

    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.live.config import TradingNodeConfig

    from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
    from kalshi.factories import (
        KalshiLiveDataClientFactory,
        KalshiLiveExecClientFactory,
    )
    from kalshi.sandbox import KalshiSandboxExecClientFactory
    from kalshi.signal_actor import SignalActor, SignalActorConfig
    from kalshi.strategy import WeatherMakerStrategy

    # --- Configs ---
    # Load only KXHIGH city series — loading all 90k instruments takes >120s.
    # Each city has its own series ticker (KXHIGHNY, KXHIGHCHI, etc).
    kxhigh_series = [
        "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN",
        "KXHIGHPHIL", "KXHIGHTATL", "KXHIGHTBOS", "KXHIGHTDAL", "KXHIGHTDC",
        "KXHIGHTHOU", "KXHIGHTLV", "KXHIGHTMIN", "KXHIGHTOKC", "KXHIGHTSATX",
        "KXHIGHTSEA", "KXHIGHTSFO",
    ]
    data_cfg = KalshiDataClientConfig(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        environment=args.environment,
        series_tickers=kxhigh_series,
    )

    signal_actor_cfg = SignalActorConfig(
        signal_http_url=args.signal_url,
        signal_ws_url=_signal_ws_url(args.signal_url),
    )

    strategy_cfg = _build_strategy_config(args)

    if args.dry_run:
        print(
            f"{'=' * 60}\n"
            f"  SANDBOX MODE — real data, simulated execution\n"
            f"  Starting balance: ${args.starting_balance}\n"
            f"{'=' * 60}"
        )

    print(
        f"Starting WeatherMakerStrategy trader\n"
        f"  environment:          {args.environment}\n"
        f"  dry_run:              {args.dry_run}\n"
        f"  signal_url:           {args.signal_url}\n"
        f"  confidence_threshold: {strategy_cfg.confidence_threshold}\n"
        f"  ladder_depth:         {strategy_cfg.ladder_depth}\n"
        f"  level_quantity:       {strategy_cfg.level_quantity}\n"
    )

    # --- TradingNode ---
    if args.dry_run:
        from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig

        from nautilus_trader.live.config import LiveExecEngineConfig

        sandbox_exec_cfg = SandboxExecutionClientConfig(
            venue="KALSHI",
            oms_type="HEDGING",
            account_type="CASH",
            base_currency="USD",
            starting_balances=[f"{args.starting_balance} USD"],
            use_position_ids=True,
        )
        node_config = TradingNodeConfig(
            trader_id="KALSHI-TRADER-001",
            exec_engine=LiveExecEngineConfig(reconciliation=False),
            data_clients={
                "KALSHI": data_cfg,
            },
            exec_clients={
                "KALSHI": sandbox_exec_cfg,
            },
        )
    else:
        exec_cfg = KalshiExecClientConfig(
            api_key_id=api_key_id,
            private_key_path=private_key_path,
            environment=args.environment,
        )
        node_config = TradingNodeConfig(
            trader_id="KALSHI-TRADER-001",
            data_clients={
                "KALSHI": data_cfg,
            },
            exec_clients={
                "KALSHI": exec_cfg,
            },
        )

    node = TradingNode(config=node_config)

    node.add_data_client_factory("KALSHI", KalshiLiveDataClientFactory)
    if args.dry_run:
        node.add_exec_client_factory("KALSHI", KalshiSandboxExecClientFactory)
    else:
        node.add_exec_client_factory("KALSHI", KalshiLiveExecClientFactory)
    node.build()

    # --- Add components ---
    signal_actor = SignalActor(config=signal_actor_cfg)
    node.trader.add_actor(signal_actor)

    strategy = WeatherMakerStrategy(config=strategy_cfg)
    node.trader.add_strategy(strategy)

    # --- Heartbeat timer (write file every 30s for systemd watchdog / monitoring) ---
    def _write_heartbeat(event=None) -> None:
        path = _heartbeat_path()
        try:
            pathlib.Path(path).touch()
        except OSError:
            pass

    strategy.clock.set_timer(
        "heartbeat",
        interval=datetime.timedelta(seconds=30),
        callback=_write_heartbeat,
    )

    def _snapshot_portfolio(event=None) -> None:
        _write_portfolio_snapshot(strategy)

    strategy.clock.set_timer(
        "portfolio_snapshot",
        interval=datetime.timedelta(seconds=60),
        callback=_snapshot_portfolio,
    )

    print(
        f"TradingNode built. Running.\n"
        f"  heartbeat  → {_heartbeat_path()}\n"
        f"  portfolio  → {_portfolio_snapshot_path()}\n"
        f"Press Ctrl+C to stop."
    )
    try:
        node.run()
    except KeyboardInterrupt:
        print("\nStopping TradingNode...")
        node.stop()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
