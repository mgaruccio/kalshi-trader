"""Backtest results extraction and reporting for WeatherMakerStrategy."""
from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.identifiers import InstrumentId


@dataclass
class BacktestResults:
    """Aggregated metrics from a completed backtest run."""

    pnl_cents: int                # realized (cash balance change)
    unrealized_pnl_cents: int     # mark-to-market value of open positions
    total_pnl_cents: int          # realized + unrealized
    fill_count: int
    order_count: int
    fill_rate: float
    max_drawdown_cents: int
    contracts_per_city: dict[str, int]
    avg_fill_price_cents: float
    open_position_count: int      # contracts still held at backtest end

    # Strategy diagnostic counters (set by A2)
    signals_received: int
    filter_passes: int
    filter_fails: int
    ladders_placed: int
    exits_attempted: int
    orders_submitted: int

    # Adjusted PnL: scale total_pnl by fill_rate / assumed_fill_rate
    adjusted_pnl_cents: float


def extract_results(engine, strategy, assumed_fill_rate: float = 0.5, starting_balance_usd: int = 10_000) -> BacktestResults:
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

    # --- orders ---
    all_orders = cache.orders()
    order_count = len(all_orders)

    # --- fill data from orders ---
    # NT LimitOrder uses status enum, not is_filled property.
    filled_orders = [
        o for o in all_orders
        if o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
    ]
    fill_count = len(filled_orders)
    fill_rate = fill_count / order_count if order_count > 0 else 0.0

    # --- PnL from account balance change ---
    accounts = cache.accounts()
    total_pnl_usd = 0.0
    if accounts:
        account = accounts[0]
        balances = account.balances()
        if balances:
            from nautilus_trader.model.currencies import USD
            bal = account.balance_total(USD)
            if bal is not None:
                ending_balance = bal.as_double()
                starting_balance = starting_balance_usd if starting_balance_usd else 10_000
                total_pnl_usd = ending_balance - starting_balance
    pnl_cents = round(total_pnl_usd * 100)

    # --- avg fill price ---
    avg_fill_price_cents = 0.0
    if fill_count > 0:
        total_price = sum(
            round(float(o.avg_px) * 100) for o in filled_orders if o.avg_px is not None
        )
        avg_fill_price_cents = total_price / fill_count

    # --- unrealized PnL: mark open positions at last observed bid ---
    unrealized_pnl_cents, open_position_count = _compute_unrealized_pnl(cache)

    # --- total PnL ---
    total_pnl_cents = pnl_cents + unrealized_pnl_cents

    # --- max drawdown ---
    max_drawdown_cents = _compute_max_drawdown_cents(filled_orders)

    # --- contracts per city ---
    contracts_per_city = _compute_contracts_per_city(filled_orders, cache)

    # --- strategy diagnostic counters ---
    signals_received = getattr(strategy, "_signals_received", 0)
    filter_passes = getattr(strategy, "_filter_passes", 0)
    filter_fails = getattr(strategy, "_filter_fails", 0)
    ladders_placed = getattr(strategy, "_ladders_placed", 0)
    exits_attempted = getattr(strategy, "_exits_attempted", 0)
    orders_submitted = getattr(strategy, "_orders_submitted", 0)

    # --- adjusted PnL: scale total PnL by fill_rate / assumed_fill_rate ---
    adjusted_pnl_cents = (
        total_pnl_cents * (fill_rate / assumed_fill_rate) if assumed_fill_rate > 0 else 0.0
    )

    return BacktestResults(
        pnl_cents=pnl_cents,
        unrealized_pnl_cents=unrealized_pnl_cents,
        total_pnl_cents=total_pnl_cents,
        fill_count=fill_count,
        order_count=order_count,
        fill_rate=fill_rate,
        max_drawdown_cents=max_drawdown_cents,
        contracts_per_city=contracts_per_city,
        avg_fill_price_cents=avg_fill_price_cents,
        open_position_count=open_position_count,
        signals_received=signals_received,
        filter_passes=filter_passes,
        filter_fails=filter_fails,
        ladders_placed=ladders_placed,
        exits_attempted=exits_attempted,
        orders_submitted=orders_submitted,
        adjusted_pnl_cents=adjusted_pnl_cents,
    )


def _compute_unrealized_pnl(cache) -> tuple[int, int]:
    """Compute mark-to-market value of open positions using last observed bid.

    For each open position, values the held contracts at the last bid price
    seen in the cache. This represents what you could sell them for right now.

    Returns (unrealized_value_cents, open_contract_count).
    """
    total_value = 0
    total_qty = 0

    for position in cache.positions():
        if position.is_closed:
            continue
        qty = int(position.quantity.as_double())
        if qty <= 0:
            continue

        # Get last quote tick for this instrument
        last_tick = cache.quote_tick(position.instrument_id)
        if last_tick is not None:
            bid_cents = round(float(last_tick.bid_price) * 100)
            total_value += qty * bid_cents
        # If no tick available, value at 0 (conservative)

        total_qty += qty

    return total_value, total_qty


def _compute_max_drawdown_cents(filled_orders) -> int:
    """Compute max drawdown in cents from filled orders.

    Drawdown is defined as the largest peak-to-trough decline in cumulative
    realized PnL. Buys are costs, sells are proceeds.
    """
    if not filled_orders:
        return 0

    # Sort by filled timestamp
    sorted_orders = sorted(filled_orders, key=lambda o: o.ts_last)

    peak = 0
    running = 0
    max_dd = 0

    for o in sorted_orders:
        if o.avg_px is None:
            continue
        price_cents = round(float(o.avg_px) * 100)
        qty = int(float(o.filled_qty))
        if o.side == OrderSide.SELL:
            running += price_cents * qty
        else:
            running -= price_cents * qty

        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return max_dd


def _compute_contracts_per_city(filled_orders, cache) -> dict[str, int]:
    """Count filled buy contracts per city.

    City extracted from ticker prefix heuristic (e.g. KXHIGHNY -> ny).
    """
    city_counts: dict[str, int] = {}
    for o in filled_orders:
        if o.side != OrderSide.BUY:
            continue
        qty = int(float(o.filled_qty))
        city = _city_from_instrument_id(str(o.instrument_id), cache)
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
        f"  Realized PnL:     {results.pnl_cents:+d}c  (${results.pnl_cents / 100:+.2f})",
        f"  Unrealized PnL:   {results.unrealized_pnl_cents:+d}c  (${results.unrealized_pnl_cents / 100:+.2f})  [{results.open_position_count} contracts @ last bid]",
        f"  Total PnL:        {results.total_pnl_cents:+d}c  (${results.total_pnl_cents / 100:+.2f})",
        f"  Adjusted PnL:     {results.adjusted_pnl_cents:+.1f}c  (total * fill_rate / assumed)",
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
