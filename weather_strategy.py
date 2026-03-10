"""Weather trading strategy for NautilusTrader.

Reacts to:
- ModelSignal: evaluate potential entries (buy NO when p_win > threshold)
- DangerAlert: evaluate exits (sell when corroborated danger)
- QuoteTick: track prices for profit targets and entry pricing
"""
import logging

from nautilus_trader.model.data import QuoteTick, DataType
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol
from nautilus_trader.model.enums import OrderSide, TimeInForce, OrderStatus
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from adapter import KALSHI_VENUE
from data_types import ModelSignal, DangerAlert

CLIMATE_CLIENT = ClientId("CLIMATE")

log = logging.getLogger(__name__)


class WeatherStrategyConfig(StrategyConfig, frozen=True):
    min_p_win: float = 0.95        # minimum ensemble p_win to enter
    max_cost_cents: int = 92        # maximum price to pay (cents)
    sell_target_cents: int = 97     # take-profit target (cents)
    danger_exit_enabled: bool = True  # auto-sell on CRITICAL DangerAlert
    trade_size: int = 1             # contracts per trade
    max_position_per_ticker: int = 20


class WeatherStrategy(Strategy):
    """Event-driven weather trading strategy.

    Entry: On ModelSignal with p_win >= min_p_win, if we can buy at <= max_cost.
    Exit: On DangerAlert CRITICAL with corroboration, or on profit target hit.
    """

    def __init__(self, config: WeatherStrategyConfig):
        super().__init__(config)
        self._cfg = config
        self._latest_quotes: dict[str, QuoteTick] = {}  # instrument_id_str -> tick
        self._positions_info: dict[str, dict] = {}  # ticker -> {side, threshold, city, contracts}
        self._danger_exited: set[str] = set()  # tickers we've danger-exited (no retry)
        self._feature_actor = None  # set after construction
        self._signals_received: int = 0
        self._alerts_received: int = 0

    def set_feature_actor(self, actor):
        """Wire the FeatureActor reference for position updates."""
        self._feature_actor = actor

    def on_start(self):
        """Subscribe to ModelSignal, DangerAlert, and quote ticks for all instruments."""
        self.subscribe_data(DataType(ModelSignal), client_id=CLIMATE_CLIENT)
        self.subscribe_data(DataType(DangerAlert), client_id=CLIMATE_CLIENT)

        # Subscribe to all cached instruments for quote ticks
        instruments = self.cache.instruments()
        for inst in instruments:
            self.subscribe_quote_ticks(inst.id)

        self.log.info(f"WeatherStrategy started, subscribed to {len(instruments)} instruments")

    def on_data(self, data):
        """Route incoming data to appropriate handler."""
        if isinstance(data, ModelSignal):
            self._signals_received += 1
            self._evaluate_entry(data)
        elif isinstance(data, DangerAlert):
            self._alerts_received += 1
            self._evaluate_exit(data)

    def on_quote_tick(self, tick: QuoteTick):
        """Track latest quotes for entry pricing and profit targets."""
        self._latest_quotes[tick.instrument_id.value] = tick
        # Check profit targets on held positions
        self._check_profit_targets(tick)

    def _quote_key(self, ticker: str, side: str) -> str:
        """Build the canonical quote cache key matching on_quote_tick storage."""
        return f"{ticker}-{side.upper()}.KALSHI"

    def _evaluate_entry(self, signal: ModelSignal):
        """Evaluate a ModelSignal for potential entry."""
        if signal.ticker in self._danger_exited:
            self.log.info(f"Skip {signal.ticker}: danger-exited")
            return

        if signal.p_win < self._cfg.min_p_win:
            self.log.info(f"Skip {signal.ticker}: p_win={signal.p_win:.3f} < {self._cfg.min_p_win}")
            return

        # Check if we already have max position
        existing = self._positions_info.get(signal.ticker, {})
        current_contracts = existing.get("contracts", 0)
        if current_contracts >= self._cfg.max_position_per_ticker:
            self.log.info(f"Skip {signal.ticker}: position full ({current_contracts})")
            return

        # Check if we have a quote for pricing
        quote_key = self._quote_key(signal.ticker, signal.side)
        quote = self._latest_quotes.get(quote_key)
        if quote is None:
            self.log.warning(f"No quote for {quote_key}, skipping entry")
            return

        # Check price
        ask_cents = int(quote.ask_price.as_double() * 100)
        if ask_cents > self._cfg.max_cost_cents:
            self.log.info(f"Skip {signal.ticker}: ask={ask_cents}c > {self._cfg.max_cost_cents}c")
            return

        # Place entry order
        instrument_id = InstrumentId(
            Symbol(f"{signal.ticker}-{signal.side.upper()}"),
            KALSHI_VENUE,
        )
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.warning(f"Instrument {instrument_id} not in cache")
            return

        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(self._cfg.trade_size),
            price=quote.ask_price,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info(
            f"Entry: {signal.ticker} {signal.side} p_win={signal.p_win:.3f} "
            f"ask={ask_cents}c"
        )

    def _evaluate_exit(self, alert: DangerAlert):
        """Evaluate a DangerAlert for potential exit."""
        if not self._cfg.danger_exit_enabled:
            return

        if alert.alert_level != "CRITICAL":
            self.log.info(f"DangerAlert {alert.alert_level} for {alert.ticker}: {alert.reason}")
            return

        if alert.ticker in self._danger_exited:
            self.log.debug(f"Already danger-exited {alert.ticker}, ignoring alert")
            return

        pos_info = self._positions_info.get(alert.ticker)
        if pos_info is None:
            self.log.debug(f"No position for {alert.ticker}, ignoring CRITICAL alert")
            return

        self.log.warning(
            f"DANGER EXIT: {alert.ticker} rule={alert.rule_name} "
            f"reason={alert.reason}"
        )

        # Mark as danger-exited BEFORE attempting sell (prevents retry loops)
        self._danger_exited.add(alert.ticker)

        # Submit sell order
        side = pos_info.get("side", "no").upper()
        instrument_id = InstrumentId(
            Symbol(f"{alert.ticker}-{side}"),
            KALSHI_VENUE,
        )
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.error(f"EXIT FAILED: instrument {instrument_id} not in cache — position stays open")
            return

        contracts = pos_info.get("contracts", 1)
        # Sell at best bid (market sell via aggressive limit)
        quote_key = self._quote_key(alert.ticker, side)
        quote = self._latest_quotes.get(quote_key)
        if quote is not None:
            price = quote.bid_price
        else:
            # Fallback: sell at 1c (will match any buyer)
            price = instrument.make_price(0.01)

        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(contracts),
            price=price,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

    def _check_profit_targets(self, tick: QuoteTick):
        """Check if any held position has hit the sell target."""
        # Find the ticker from the instrument ID
        inst_str = tick.instrument_id.symbol.value  # e.g. "KXHIGHCHI-T55-NO"
        # Extract ticker (everything before -YES or -NO)
        if inst_str.endswith("-YES"):
            ticker = inst_str[:-4]
            side = "YES"
        elif inst_str.endswith("-NO"):
            ticker = inst_str[:-3]
            side = "NO"
        else:
            return

        pos_info = self._positions_info.get(ticker)
        if pos_info is None:
            return

        bid_cents = int(tick.bid_price.as_double() * 100)
        if bid_cents >= self._cfg.sell_target_cents:
            instrument = self.cache.instrument(tick.instrument_id)
            if instrument is None:
                return

            contracts = pos_info.get("contracts", 1)
            order = self.order_factory.limit(
                instrument_id=tick.instrument_id,
                order_side=OrderSide.SELL,
                quantity=instrument.make_qty(contracts),
                price=tick.bid_price,
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order)
            self.log.info(f"Profit target hit: {ticker} bid={bid_cents}c >= {self._cfg.sell_target_cents}c")

    def on_order_filled(self, event: OrderFilled):
        """Track position changes on fills."""
        self.log.info(f"Order filled: {event}")
        
        # Extract ticker and side from instrument ID
        inst_str = event.instrument_id.symbol.value # e.g. "KXHIGHCHI-26MAR01-T72-NO"
        if inst_str.endswith("-YES"):
            ticker = inst_str[:-4]
            side = "yes"
        elif inst_str.endswith("-NO"):
            ticker = inst_str[:-3]
            side = "no"
        else:
            self.log.warning(f"Could not parse ticker from {inst_str}")
            return

        # Update positions_info
        if ticker not in self._positions_info:
            # We need the metadata (threshold, city) which we can get from parse_ticker if available
            # or from the ModelSignal we reacted to. For now, let's parse ticker.
            from kalshi_weather_ml.markets import parse_ticker
            parsed = parse_ticker(ticker)
            city = ""
            threshold = 0.0
            if parsed:
                threshold = parsed["threshold"]
                # Map series to city
                from kalshi_weather_ml.markets import SERIES_CONFIG
                series_to_city = {s: c for s, c in SERIES_CONFIG}
                city = series_to_city.get(parsed["series"], "")
            
            self._positions_info[ticker] = {
                "side": side,
                "threshold": threshold,
                "city": city,
                "contracts": 0,
            }

        pos = self._positions_info[ticker]
        qty = int(event.last_qty.as_double())
        
        if event.order_side == OrderSide.BUY:
            pos["contracts"] += qty
        else:
            pos["contracts"] -= qty

        # Clean up if position closed
        if pos["contracts"] <= 0:
            self._positions_info.pop(ticker)
        
        # Sync with FeatureActor
        self._sync_positions_to_actor()

    def on_stop(self):
        """Log shutdown -- engine handles order cleanup on stop."""
        self.log.info("WeatherStrategy stopping")

    def _sync_positions_to_actor(self):
        """Push current position info to FeatureActor for exit rule evaluation."""
        if self._feature_actor is not None:
            self._feature_actor.update_positions(self._positions_info)

    @property
    def signals_received(self) -> int:
        return self._signals_received

    @property
    def alerts_received(self) -> int:
        return self._alerts_received
