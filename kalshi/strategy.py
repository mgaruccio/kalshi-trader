"""WeatherMakerStrategy — forecast-filtered passive market maker for KXHIGH contracts."""
from __future__ import annotations

import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import DataType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol
from nautilus_trader.model.objects import Currency
from nautilus_trader.trading.strategy import Strategy

from kalshi.common.constants import KALSHI_VENUE
from kalshi.providers import parse_instrument_id
from kalshi.signals import ForecastDrift, SignalScore

_ET = ZoneInfo("America/New_York")
_USD = Currency.from_str("USD")


class WeatherMakerConfig(StrategyConfig, frozen=True):
    """Configuration for the WeatherMakerStrategy."""

    # Filter
    confidence_threshold: float = 0.95
    min_models: int = 2
    max_model_spread: float = 0.15      # max spread between per-model probabilities

    # Quote ladder
    ladder_depth: int = 3
    ladder_spacing: int = 1             # cents between levels
    level_quantity: int = 10            # contracts per level
    reprice_threshold: int = 2          # cents — min bid movement to trigger reprice
    max_entry_cents: int = 96           # hard cap on anchor price

    # Risk
    market_cap_pct: float = 0.20        # max fraction of account per contract
    city_cap_pct: float = 0.33          # max fraction of account per city

    # Exit
    exit_price_cents: int = 97          # close position when bid reaches this

    # Time-of-day gating (ET)
    entry_phase_start_et: str = "10:30"
    entry_phase_end_et: str = "15:00"
    tomorrow_min_age_minutes: int = 30  # delay for tomorrow contract entry

    # Circuit breaker
    max_drawdown_pct: float = 0.15
    small_account_drawdown_pct: float = 0.25   # relaxed limit when balance < threshold
    small_account_threshold_usd: int = 100     # accounts below this use relaxed limit
    halt_file_path: str = "/tmp/kalshi-halt"

    # Dry-run mode — log orders instead of submitting
    dry_run: bool = False
    dry_run_balance_usd: int = 20              # simulated starting capital


def should_quote(
    config: WeatherMakerConfig,
    score: SignalScore,
    drift_cities: set[str],
) -> tuple[str, bool]:
    """Decide if a contract should be quoted and on which side.

    Returns (side, passes) where side is "yes" or "no" and passes is True if
    all filter conditions are met.
    """
    # Determine winning side
    if score.no_p_win >= score.yes_p_win:
        side = "no"
        p_win = score.no_p_win
        anchor_cents = score.yes_bid  # NO buyer bids yes_bid price in practice
    else:
        side = "yes"
        p_win = score.yes_p_win
        anchor_cents = score.yes_bid

    # Must be tradeable market — signal server returns "active", Kalshi API uses "open"
    if score.status and score.status not in ("open", "active"):
        return side, False

    # Check model count
    if score.n_models < config.min_models:
        return side, False

    # Check drift — pause only
    if score.city in drift_cities:
        return side, False

    # Check confidence threshold
    if p_win < config.confidence_threshold:
        return side, False

    # Check model agreement (spread among non-zero model probabilities)
    model_scores = [v for v in (score.emos_no, score.ngboost_no, score.drn_no) if v > 0.0]
    if len(model_scores) >= 2:
        spread = max(model_scores) - min(model_scores)
        if spread > config.max_model_spread:
            return side, False

    # Check hard cap on entry price
    if anchor_cents > config.max_entry_cents:
        return side, False

    return side, True


def check_risk_caps(
    market_exposure_cents: int,
    city_exposure_cents: int,
    quantity: int,
    price_cents: int,
    account_balance_cents: int,
    market_cap_pct: float,
    city_cap_pct: float,
) -> int:
    """Return max allowed quantity given risk caps.

    Caller derives market_exposure_cents and city_exposure_cents from
    cache.positions() and cache.orders_open() — no ExposureTracker needed.

    Returns a value between 0 and quantity (inclusive).
    """
    if account_balance_cents <= 0 or price_cents <= 0:
        return 0

    market_cap = int(account_balance_cents * market_cap_pct)
    city_cap = int(account_balance_cents * city_cap_pct)

    market_remaining = max(0, market_cap - market_exposure_cents)
    city_remaining = max(0, city_cap - city_exposure_cents)

    max_by_market = market_remaining // price_cents
    max_by_city = city_remaining // price_cents

    return min(quantity, max_by_market, max_by_city)


def compute_ladder(
    anchor_bid_cents: int,
    depth: int,
    spacing: int,
    qty: int,
) -> list[tuple[int, int]]:
    """Compute a descending quote ladder from the anchor bid.

    Returns up to `depth` (price_cents, quantity) tuples, each spaced
    `spacing` cents apart, starting at the anchor. Levels below 1c excluded.
    """
    levels: list[tuple[int, int]] = []
    for i in range(depth):
        price = anchor_bid_cents - (i * spacing)
        if price < 1:
            break
        levels.append((price, qty))
    return levels


class WeatherMakerStrategy(Strategy):
    """Forecast-filtered passive market maker for KXHIGH contracts."""

    def __init__(self, config: WeatherMakerConfig) -> None:
        super().__init__(config)
        self._init_state(config)

    def _init_state(self, config: WeatherMakerConfig) -> None:
        """Initialize strategy state. Called from __init__ and test helpers."""
        self._config = config
        self._scores: dict[str, SignalScore] = {}
        self._drift_cities: set[str] = set()
        self._quoted_tickers: set[str] = set()
        self._resting_orders: dict[str, list[ClientOrderId]] = {}
        self._last_anchor: dict[str, int] = {}
        self._halted: bool = False
        self._initial_balance_cents: int = 0
        # ticker -> ns timestamp when it first passed filter (for tomorrow gating)
        self._first_quoted_ns: dict[str, int] = {}
        # tickers with an IOC exit order in-flight (prevents duplicate exits)
        self._exiting_tickers: set[str] = set()
        # Dry-run simulated balance tracker
        self._dry_run_balance_cents: int = 0
        # Diagnostic counters
        self._signals_received: int = 0
        self._filter_passes: int = 0
        self._filter_fails: int = 0
        self._ladders_placed: int = 0
        self._exits_attempted: int = 0
        self._orders_submitted: int = 0

    def on_start(self) -> None:
        self.subscribe_data(DataType(SignalScore))
        self.subscribe_data(DataType(ForecastDrift))
        # Record initial balance for drawdown circuit breaker
        if self._config.dry_run:
            self._initial_balance_cents = self._config.dry_run_balance_usd * 100
            self._dry_run_balance_cents = self._initial_balance_cents
            self.log.info(
                f"DRY-RUN MODE — no orders will be submitted, "
                f"simulated balance {self._initial_balance_cents}c"
            )
        else:
            account = self.portfolio.account(KALSHI_VENUE)
            if account is not None:
                bal = account.balances().get(_USD)
                if bal is not None:
                    self._initial_balance_cents = int(bal.total.as_double() * 100)
            self.log.info(
                f"WeatherMakerStrategy started — subscribed to signals, "
                f"initial balance {self._initial_balance_cents}c"
            )

    def on_stop(self) -> None:
        open_orders = self.cache.orders_open(strategy_id=self.id)
        if open_orders:
            self.cancel_orders(open_orders)
        self.log.info("WeatherMakerStrategy stopped — all orders canceled")

    def on_data(self, data: Data) -> None:
        if isinstance(data, SignalScore):
            self._handle_signal_score(data)
        elif isinstance(data, ForecastDrift):
            self._handle_forecast_drift(data)

    def on_order_filled(self, event) -> None:
        self.log.info(
            f"FILL: {event.instrument_id} {event.order_side} "
            f"{event.last_qty}@{event.last_px}"
        )
        # Clear exit guard so next tick can re-attempt if position remains open
        try:
            base_ticker, _ = parse_instrument_id(event.instrument_id)
            self._exiting_tickers.discard(base_ticker)
        except ValueError:
            pass

    def on_order_canceled(self, event) -> None:
        pass  # _resting_orders replaced entirely on reprice

    def on_order_rejected(self, event) -> None:
        self.log.error(f"ORDER REJECTED: {event.instrument_id} — {event.reason}")
        # Clear exit guard so next tick can retry
        try:
            base_ticker, _ = parse_instrument_id(event.instrument_id)
            self._exiting_tickers.discard(base_ticker)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _handle_signal_score(self, score: SignalScore) -> None:
        """Process a new score — update state and re-evaluate filter.

        Per Enhancement #18: drift is NOT cleared by a new score. Drift
        persists until session end or explicit 'drift_cleared' signal.
        """
        self._signals_received += 1
        self._scores[score.ticker] = score
        self._evaluate_contract(score.ticker)

    def _handle_forecast_drift(self, drift: ForecastDrift) -> None:
        """Pause quoting for the drifted city."""
        self._drift_cities.add(drift.city)
        self.log.warning(f"Forecast drift for {drift.city}: {drift.message}")

        # Re-evaluate all contracts for this city (may cancel resting orders)
        for ticker, score in list(self._scores.items()):
            if score.city == drift.city:
                self._evaluate_contract(ticker)

    # ------------------------------------------------------------------
    # Filter evaluation
    # ------------------------------------------------------------------

    def _evaluate_contract(self, ticker: str) -> None:
        """Re-evaluate whether to quote a contract based on current state."""
        if self._halted:
            return

        score = self._scores.get(ticker)
        if score is None:
            return

        side, passes = should_quote(self._config, score, self._drift_cities)

        if passes and ticker not in self._quoted_tickers:
            self._filter_passes += 1
            self._quoted_tickers.add(ticker)
            # Record first-seen time for tomorrow contract gating
            if ticker not in self._first_quoted_ns:
                self._first_quoted_ns[ticker] = self.clock.timestamp_ns()
            self.log.info(
                f"Filter PASS: {ticker} ({side} side, "
                f"p_win={score.no_p_win if side == 'no' else score.yes_p_win:.3f})"
            )
            # Subscribe to quote ticks for both sides of the contract.
            # cache.add_instrument() MUST precede subscribe_quote_ticks() —
            # StreamingFeatherWriter silently drops ticks for unknown instruments.
            for side_suffix in ("YES", "NO"):
                instrument_id = InstrumentId(
                    Symbol(f"{ticker}-{side_suffix}"),
                    KALSHI_VENUE,
                )
                instrument = self.cache.instrument(instrument_id)
                if instrument is not None:
                    self.cache.add_instrument(instrument)
                    self.subscribe_quote_ticks(instrument_id)

        elif not passes and ticker in self._quoted_tickers:
            self._filter_fails += 1
            self._quoted_tickers.discard(ticker)
            self._cancel_all_for_ticker(ticker)
            self.log.info(f"Filter EXIT: {ticker}")

        elif not passes and ticker not in self._quoted_tickers:
            self._filter_fails += 1

    # ------------------------------------------------------------------
    # Quote management
    # ------------------------------------------------------------------

    def on_quote_tick(self, tick) -> None:
        """Handle incoming quote tick — check halt then reprice."""
        # Cheap halt check on every tick (halt file only, no position iteration)
        if not self._halted and os.path.exists(self._config.halt_file_path):
            self._trigger_halt("halt file present")
        if self._halted:
            return

        try:
            base_ticker, tick_side = parse_instrument_id(tick.instrument_id)
        except ValueError:
            return

        if base_ticker not in self._quoted_tickers:
            return

        score = self._scores.get(base_ticker)
        if score is None:
            return

        side, passes = should_quote(self._config, score, self._drift_cities)
        if not passes or tick_side != side:
            return

        bid_cents = round(float(tick.bid_price) * 100)
        if bid_cents <= 0:
            return

        # Check exit condition
        if bid_cents >= self._config.exit_price_cents:
            self._exit_position(base_ticker, tick.instrument_id, bid_cents)
            return

        # Dead-zone check
        last_anchor = self._last_anchor.get(base_ticker, 0)
        if last_anchor > 0 and abs(bid_cents - last_anchor) < self._config.reprice_threshold:
            return

        self._reprice_ladder(base_ticker, tick.instrument_id, side, bid_cents, score)

    def _reprice_ladder(
        self,
        ticker: str,
        instrument_id: InstrumentId,
        side: str,
        bid_cents: int,
        score: SignalScore,
    ) -> None:
        """Cancel existing orders and place new ladder at current bid."""
        self._check_circuit_breaker()
        if self._halted:
            return
        self._cancel_all_for_ticker(ticker)

        # Resolve balance — dry-run uses simulated tracker, live uses portfolio
        if self._config.dry_run:
            balance_cents = self._dry_run_balance_cents
        else:
            account = self.portfolio.account(instrument_id.venue)
            if account is None:
                self.log.warning(f"No account for {instrument_id.venue}")
                return
            usd_balance = account.balances().get(_USD)
            if usd_balance is None:
                return
            balance_cents = int(usd_balance.total.as_double() * 100)

        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.warning(f"Instrument not in cache: {instrument_id}")
            return

        # Derive exposure from NT cache (single source of truth)
        market_exposure = self._market_exposure_cents(ticker)
        city_exposure = self._city_exposure_cents(score.city)

        levels = compute_ladder(
            anchor_bid_cents=bid_cents,
            depth=self._config.ladder_depth,
            spacing=self._config.ladder_spacing,
            qty=self._config.level_quantity,
        )

        new_order_ids = []
        for price_cents, qty in levels:
            allowed_qty = check_risk_caps(
                market_exposure_cents=market_exposure,
                city_exposure_cents=city_exposure,
                quantity=qty,
                price_cents=price_cents,
                account_balance_cents=balance_cents,
                market_cap_pct=self._config.market_cap_pct,
                city_cap_pct=self._config.city_cap_pct,
            )
            if allowed_qty <= 0:
                break

            if self._config.dry_run:
                cost_cents = allowed_qty * price_cents
                self.log.info(
                    f"DRY-RUN BUY: {instrument_id} {allowed_qty}@{price_cents}c "
                    f"(cost={cost_cents}c, balance={balance_cents}c→{balance_cents - cost_cents}c)"
                )
                self._dry_run_balance_cents -= cost_cents
                balance_cents = self._dry_run_balance_cents
                self._orders_submitted += 1
            else:
                order = self.order_factory.limit(
                    instrument_id=instrument_id,
                    order_side=OrderSide.BUY,
                    quantity=instrument.make_qty(allowed_qty),
                    price=instrument.make_price(price_cents / 100.0),
                    time_in_force=TimeInForce.GTC,
                )
                self.submit_order(order)
                self._orders_submitted += 1
                new_order_ids.append(order.client_order_id)

            # Update local tally for subsequent ladder levels
            market_exposure += allowed_qty * price_cents
            city_exposure += allowed_qty * price_cents

        self._resting_orders[ticker] = new_order_ids
        self._last_anchor[ticker] = bid_cents
        if self._config.dry_run:
            self._ladders_placed += 1
        elif new_order_ids:
            self._ladders_placed += 1

    def _exit_position(self, ticker: str, instrument_id: InstrumentId, bid_cents: int) -> None:
        """Exit ALL open long positions when price hits exit threshold.

        In HEDGING mode each fill creates a separate position, so we must
        close each one individually by passing position_id to submit_order.
        Without position_id, the sell creates a new SHORT instead of closing
        the existing LONG.

        Guard against duplicate exits: if exits are already in-flight for
        this ticker, skip. Cleared in on_order_filled / on_order_rejected.

        In dry-run mode: log the exit and credit simulated proceeds.
        """
        if ticker in self._exiting_tickers:
            return

        self._cancel_all_for_ticker(ticker)

        if self._config.dry_run:
            # In dry-run mode, we don't have real positions. Credit proceeds
            # based on the bid price for any simulated cost already deducted.
            self._exits_attempted += 1
            self.log.info(
                f"DRY-RUN EXIT: {ticker} at {bid_cents}c "
                f"(balance={self._dry_run_balance_cents}c)"
            )
            return

        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return

        total_qty = 0
        positions = self.cache.positions(venue=instrument_id.venue)
        for pos in positions:
            if pos.instrument_id == instrument_id and not pos.is_closed:
                qty = int(pos.quantity.as_double())
                if qty > 0 and pos.is_long:
                    order = self.order_factory.market(
                        instrument_id=instrument_id,
                        order_side=OrderSide.SELL,
                        quantity=instrument.make_qty(qty),
                        time_in_force=TimeInForce.IOC,
                    )
                    self.submit_order(order, position_id=pos.id)
                    total_qty += qty

        if total_qty > 0:
            self._exiting_tickers.add(ticker)
            self._exits_attempted += 1
            self.log.info(f"EXIT: {ticker} at {bid_cents}c, closing {total_qty} contracts")

    def _cancel_all_for_ticker(self, ticker: str) -> None:
        """Cancel all resting orders for a specific ticker."""
        order_ids = self._resting_orders.pop(ticker, [])
        for oid in order_ids:
            order = self.cache.order(oid)
            if order and order.is_open:
                self.cancel_order(order)

    # ------------------------------------------------------------------
    # Exposure helpers (cache-derived, no ExposureTracker)
    # ------------------------------------------------------------------

    def _market_exposure_cents(self, ticker: str) -> int:
        """Sum open order + position exposure for a specific market ticker."""
        total = 0
        for order in self.cache.orders_open(strategy_id=self.id):
            try:
                base_ticker, _ = parse_instrument_id(order.instrument_id)
            except ValueError:
                continue
            if base_ticker == ticker:
                price_cents = round(float(order.price) * 100) if order.price else 0
                qty = int(order.quantity.as_double())
                total += price_cents * qty
        return total

    def _city_exposure_cents(self, city: str) -> int:
        """Sum open order exposure for all tickers in a city."""
        total = 0
        for t, score in self._scores.items():
            if score.city == city:
                total += self._market_exposure_cents(t)
        return total

    # ------------------------------------------------------------------
    # Time-of-day and tomorrow contract helpers
    # ------------------------------------------------------------------

    def _in_entry_phase(self) -> bool:
        """Return True if current ET time is within the entry phase window."""
        now_ns = self.clock.timestamp_ns()
        now_et = datetime.fromtimestamp(now_ns / 1e9, tz=_ET).time()
        h_start, m_start = (int(x) for x in self._config.entry_phase_start_et.split(":"))
        h_end, m_end = (int(x) for x in self._config.entry_phase_end_et.split(":"))
        start = time(h_start, m_start)
        end = time(h_end, m_end)
        return start <= now_et < end

    def _is_tomorrow_contract(self, ticker: str) -> bool:
        """Return True if this ticker settles tomorrow (not today)."""
        # Ticker format: KXHIGHNY-26MAR15-T54 — date part is index 1
        parts = ticker.split("-")
        if len(parts) < 2:
            return False
        date_str = parts[1]  # e.g. "26MAR15"
        try:
            # Parse YYMONDD: e.g. "26MAR15" -> 2026-03-15
            settlement = datetime.strptime(date_str, "%y%b%d").date()
            now_ns = self.clock.timestamp_ns()
            today = datetime.fromtimestamp(now_ns / 1e9, tz=_ET).date()
            return settlement > today
        except ValueError:
            return False

    def _tomorrow_delay_elapsed(self, ticker: str) -> bool:
        """Return True if enough time has passed since first quote for a tomorrow contract."""
        first_ns = self._first_quoted_ns.get(ticker, 0)
        if first_ns == 0:
            return False
        elapsed_ns = self.clock.timestamp_ns() - first_ns
        delay_ns = self._config.tomorrow_min_age_minutes * 60 * 1_000_000_000
        return elapsed_ns >= delay_ns

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _check_circuit_breaker(self) -> None:
        """Halt if halt file exists or drawdown limit breached.

        Drawdown is measured from portfolio value (cash + mark-to-market
        positions at last bid), not just cash balance. This prevents
        capital deployment from being mistaken for losses.
        """
        if self._halted:
            return

        # Halt file check
        if os.path.exists(self._config.halt_file_path):
            self._trigger_halt("halt file present")
            return

        # Drawdown check — portfolio value vs initial balance
        if self._initial_balance_cents > 0:
            portfolio_cents = (
                self._dry_run_balance_cents
                if self._config.dry_run
                else self._portfolio_value_cents()
            )
            if portfolio_cents is not None:
                drawdown = (self._initial_balance_cents - portfolio_cents) / self._initial_balance_cents
                # Small accounts get a relaxed drawdown limit
                threshold = self._config.small_account_threshold_usd * 100
                limit = (
                    self._config.small_account_drawdown_pct
                    if self._initial_balance_cents < threshold
                    else self._config.max_drawdown_pct
                )
                if drawdown >= limit:
                    self._trigger_halt(
                        f"drawdown {drawdown:.1%} >= limit {limit:.1%}"
                    )

    def _portfolio_value_cents(self) -> int | None:
        """Return cash + mark-to-market value of open positions in cents."""
        account = self.portfolio.account(KALSHI_VENUE)
        if account is None:
            return None
        usd_balance = account.balances().get(_USD)
        if usd_balance is None:
            return None
        cash_cents = int(usd_balance.total.as_double() * 100)

        # Mark open positions at last bid
        mtm_cents = 0
        for position in self.cache.positions(venue=KALSHI_VENUE):
            if position.is_closed:
                continue
            qty = int(position.quantity.as_double())
            if qty <= 0:
                continue
            last_tick = self.cache.quote_tick(position.instrument_id)
            if last_tick is not None:
                bid_cents = round(float(last_tick.bid_price) * 100)
                mtm_cents += qty * bid_cents

        return cash_cents + mtm_cents

    def _trigger_halt(self, reason: str) -> None:
        """Cancel all orders and set halted flag. Do NOT close positions."""
        self._halted = True
        self.log.error(f"CIRCUIT BREAKER TRIGGERED: {reason} — halting all quoting")
        open_orders = self.cache.orders_open(strategy_id=self.id)
        if open_orders:
            self.cancel_orders(open_orders)
