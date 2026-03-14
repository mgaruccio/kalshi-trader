"""Backtest results extraction and reporting for WeatherMakerStrategy."""
from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId


@dataclass
class BacktestResults:
    """Aggregated metrics from a completed backtest run."""

    pnl_cents: int
    fill_count: int
    order_count: int
    fill_rate: float
    max_drawdown_cents: int
    contracts_per_city: dict[str, int]
    avg_fill_price_cents: float

    # Strategy diagnostic counters (set by A2)
    signals_received: int
    filter_passes: int
    filter_fails: int
    ladders_placed: int
    exits_attempted: int
    orders_submitted: int

    # Adjusted PnL: scale by fill_rate / assumed_fill_rate
    adjusted_pnl_cents: float


def extract_results(engine, strategy, assumed_fill_rate: float = 0.5) -> BacktestResults:
    """Extract metrics from a completed BacktestEngine run and strategy state.

    Args:
        engine: BacktestEngine after engine.run() has been called.
        strategy: WeatherMakerStrategy instance (must have diagnostic counters
                  from A2: _signals_received, _filter_passes, _filter_fails,
                  _ladders_placed, _exits_attempted, _orders_submitted).
        assumed_fill_rate: Expected fill rate used to scale adjusted PnL.

    Returns:
        BacktestResults with all fields populated.
    """
    cache = engine.cache

    # --- fills and orders ---
    fills = cache.fills()
    orders = cache.orders()

    fill_count = len(fills)
    order_count = len(orders)
    fill_rate = fill_count / order_count if order_count > 0 else 0.0

    # --- PnL from positions ---
    # Sum realized PnL from all positions
    total_pnl_usd = 0.0
    for position in cache.positions():
        rpnl = position.realized_pnl
        if rpnl is not None:
            total_pnl_usd += rpnl.as_double()
    pnl_cents = round(total_pnl_usd * 100)

    # --- avg fill price ---
    avg_fill_price_cents = 0.0
    if fill_count > 0:
        total_price = sum(
            round(float(f.last_px) * 100) for f in fills
        )
        avg_fill_price_cents = total_price / fill_count

    # --- max drawdown ---
    # NautilusTrader exposes account statistics; use realized PnL series
    max_drawdown_cents = _compute_max_drawdown_cents(fills)

    # --- contracts per city ---
    contracts_per_city = _compute_contracts_per_city(fills, cache)

    # --- strategy diagnostic counters ---
    signals_received = getattr(strategy, "_signals_received", 0)
    filter_passes = getattr(strategy, "_filter_passes", 0)
    filter_fails = getattr(strategy, "_filter_fails", 0)
    ladders_placed = getattr(strategy, "_ladders_placed", 0)
    exits_attempted = getattr(strategy, "_exits_attempted", 0)
    orders_submitted = getattr(strategy, "_orders_submitted", 0)

    # --- adjusted PnL ---
    adjusted_pnl_cents = (
        pnl_cents * (fill_rate / assumed_fill_rate) if assumed_fill_rate > 0 else 0.0
    )

    return BacktestResults(
        pnl_cents=pnl_cents,
        fill_count=fill_count,
        order_count=order_count,
        fill_rate=fill_rate,
        max_drawdown_cents=max_drawdown_cents,
        contracts_per_city=contracts_per_city,
        avg_fill_price_cents=avg_fill_price_cents,
        signals_received=signals_received,
        filter_passes=filter_passes,
        filter_fails=filter_fails,
        ladders_placed=ladders_placed,
        exits_attempted=exits_attempted,
        orders_submitted=orders_submitted,
        adjusted_pnl_cents=adjusted_pnl_cents,
    )


def _compute_max_drawdown_cents(fills) -> int:
    """Compute max drawdown in cents from the fill sequence.

    Drawdown is defined as the largest peak-to-trough decline in cumulative
    realized PnL over the fill sequence. A fill's contribution to PnL is
    positive for sells (exits) and negative for buys (entries at cost).
    """
    if not fills:
        return 0

    peak = 0
    running = 0
    max_dd = 0

    for f in fills:
        price_cents = round(float(f.last_px) * 100)
        qty = int(f.last_qty)
        if f.order_side == OrderSide.SELL:
            running += price_cents * qty   # proceeds
        else:
            running -= price_cents * qty   # cost

        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return max_dd


def _compute_contracts_per_city(fills, cache) -> dict[str, int]:
    """Count filled buy contracts per city using instrument metadata.

    City is extracted from the instrument's raw_symbol metadata if available,
    falling back to the ticker prefix heuristic (e.g. KXHIGHNY -> ny).
    """
    city_counts: dict[str, int] = {}
    for f in fills:
        if f.order_side != OrderSide.BUY:
            continue
        qty = int(f.last_qty)
        city = _city_from_instrument_id(str(f.instrument_id), cache)
        city_counts[city] = city_counts.get(city, 0) + qty

    return city_counts


def _city_from_instrument_id(instrument_id_str: str, cache) -> str:
    """Derive city name from instrument ID.

    Tries instrument metadata first; falls back to ticker-prefix heuristic.
    Ticker format: KXHIGH<CITY_CODE>-<DATE>-<THRESHOLD>-<SIDE>
    e.g. KXHIGHNY-26MAR15-T54-YES -> city code "NY"
    """
    try:
        inst_id = InstrumentId.from_str(instrument_id_str)
        instrument = cache.instrument(inst_id)
        if instrument is not None:
            info = getattr(instrument, "info", None)
            if info and "city" in info:
                return str(info["city"])
    except Exception:
        pass

    # Heuristic: extract from symbol prefix (KXHIGH<CODE>)
    symbol = instrument_id_str.split(".")[0] if "." in instrument_id_str else instrument_id_str
    ticker = symbol.split("-")[0]  # e.g. "KXHIGHNY"
    if ticker.startswith("KXHIGH") and len(ticker) > 6:
        return ticker[6:].lower()  # e.g. "ny"
    return "unknown"


def format_report(results: BacktestResults) -> str:
    """Format a human-readable backtest summary report.

    Args:
        results: BacktestResults from extract_results().

    Returns:
        Multi-line string suitable for printing or logging.
    """
    lines = [
        "=" * 60,
        "BACKTEST RESULTS",
        "=" * 60,
        "",
        "--- PnL ---",
        f"  PnL:              {results.pnl_cents:+d}c  (${results.pnl_cents / 100:+.2f})",
        f"  Adjusted PnL:     {results.adjusted_pnl_cents:+.1f}c  (fill_rate / assumed)",
        "",
        "--- Fill Statistics ---",
        f"  Orders placed (NT): {results.order_count}",
        f"  Fills:            {results.fill_count}",
        f"  Fill rate:        {results.fill_rate:.1%}",
        f"  Avg fill price:   {results.avg_fill_price_cents:.1f}c",
        "",
        "--- Risk ---",
        f"  Max drawdown:     {results.max_drawdown_cents}c  (${results.max_drawdown_cents / 100:.2f})",
        "",
        "--- Strategy Diagnostics ---",
        f"  Signals received: {results.signals_received}",
        f"  Filter passes:    {results.filter_passes}",
        f"  Filter fails:     {results.filter_fails}",
        f"  Ladders placed:   {results.ladders_placed}",
        f"  Exits attempted:  {results.exits_attempted}",
        f"  Orders submitted: {results.orders_submitted}",
        "",
        "--- Contracts Per City ---",
    ]

    if results.contracts_per_city:
        for city, count in sorted(results.contracts_per_city.items()):
            lines.append(f"  {city:20s}: {count}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)
