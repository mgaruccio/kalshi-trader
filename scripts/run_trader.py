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

    print(f"TradingNode built. Running (heartbeat → {_heartbeat_path()}). Press Ctrl+C to stop.")
    try:
        node.run()
    except KeyboardInterrupt:
        print("\nStopping TradingNode...")
        node.stop()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
