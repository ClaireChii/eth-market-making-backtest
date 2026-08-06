"""Baseline market-making strategy and desired quote types."""

from dataclasses import dataclass
import math

from src.account import Account
from src.config import StrategyConfig
from src.features import MarketFeatures
from src.market_state import MarketState


@dataclass(frozen=True, slots=True)
class DesiredQuote:
    """Describe the bid and ask that a strategy wants to maintain."""

    bid_price: float | None
    bid_quantity: float
    ask_price: float | None
    ask_quantity: float

    def __post_init__(self) -> None:
        """Keep absent quote sides paired with zero quantity."""
        self._validate_side("bid", self.bid_price, self.bid_quantity)
        self._validate_side("ask", self.ask_price, self.ask_quantity)

    @staticmethod
    def _validate_side(
        name: str,
        price: float | None,
        quantity: float,
    ) -> None:
        """Validate one optional side of a desired quote."""
        if price is None:
            if quantity != 0:
                raise ValueError(f"absent {name} must have zero quantity")
            return
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"{name} price must be finite and positive")
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError(f"{name} quantity must be finite and positive")

    @classmethod
    def empty(cls) -> "DesiredQuote":
        """Return a quote with neither a bid nor an ask."""
        return cls(None, 0.0, None, 0.0)


class BaselineStrategy:
    """Quote around mid price with fixed spread and inventory skew."""

    def __init__(self, config: StrategyConfig) -> None:
        """Store the shared strategy configuration."""
        self.config = config

    def quote(
        self,
        market_state: MarketState,
        account: Account,
        features: MarketFeatures | None = None,
    ) -> DesiredQuote:
        """Calculate a passive desired quote from market and inventory state."""
        if not market_state.has_order_book:
            return DesiredQuote.empty()         # create an empty order object.

        mid_price = market_state.mid_price
        best_bid = market_state.best_bid
        best_ask = market_state.best_ask
        if mid_price is None or best_bid is None or best_ask is None:
            return DesiredQuote.empty()

        fair_price = self._fair_price(market_state, features)

        target_inventory = self.target_inventory(market_state)
        inventory_deviation = account.inventory - target_inventory
        inventory_ratio = max(
            -1.0,
            min(
                1.0,
                inventory_deviation / self.config.max_inventory,
            ),
        )
        reservation_price = fair_price - (
            self.config.inventory_skew_ticks
            * self.config.tick_size
            * inventory_ratio
        )
        half_spread = self._half_spread(features)

        raw_bid = reservation_price - half_spread
        raw_ask = reservation_price + half_spread
        bid_price = min(
            self._round_bid(raw_bid),
            best_bid,
        )
        ask_price = max(
            self._round_ask(raw_ask),
            best_ask,
        )

        bid_quantity = min(
            self.config.order_quantity,
            max(0.0, self.config.max_inventory - account.inventory),
        )
        ask_quantity = min(
            self.config.order_quantity,
            max(0.0, self.config.max_inventory + account.inventory),
        )

        return DesiredQuote(
            bid_price=bid_price if bid_quantity > 0 else None,
            bid_quantity=bid_quantity,
            ask_price=ask_price if ask_quantity > 0 else None,
            ask_quantity=ask_quantity,
        )

    def target_inventory(self, market_state: MarketState) -> float:
        """Return the zero-inventory target."""
        return 0.0

    @staticmethod
    def _fair_price(
        market_state: MarketState,
        features: MarketFeatures | None,
    ) -> float:
        """Return midpoint as the parameter-free B0 fair price."""

        mid_price = market_state.mid_price
        if mid_price is None:
            raise RuntimeError("baseline fair price requires an order book")
        return mid_price

    def _round_bid(self, price: float) -> float:
        """Round a bid down to the nearest valid tick."""
        tick_size = self.config.tick_size
        # The epsilon prevents an exact tick from rounding down by one step.
        return math.floor(price / tick_size + 1e-12) * tick_size

    def _round_ask(self, price: float) -> float:
        """Round an ask up to the nearest valid tick."""
        tick_size = self.config.tick_size
        return math.ceil(price / tick_size - 1e-12) * tick_size

    def _half_spread(self, features: MarketFeatures | None) -> float:
        """Return the fixed B0 half-spread in price units."""
        return (
            self.config.fixed_spread_ticks
            * self.config.tick_size
            / 2
        )


class ImbalanceStrategy(BaselineStrategy):
    """Use L1 microprice as fair value before the baseline inventory skew."""

    @staticmethod
    def _fair_price(
        market_state: MarketState,
        features: MarketFeatures | None,
    ) -> float:
        """Return parameter-free microprice supplied by the feature engine."""
        if features is None:
            raise RuntimeError("imbalance strategy requires market features")
        return features.microprice


class VolatilitySpreadStrategy(BaselineStrategy):
    """Add scaled trailing price volatility to each quote side."""

    def quote(
        self,
        market_state: MarketState,
        account: Account,
        features: MarketFeatures | None = None,
    ) -> DesiredQuote:
        """Calculate B2 quotes and keep them within visible book depth."""

        quote = super().quote(market_state, account, features)
        if not market_state.has_order_book:
            return quote

        bid_price = (
            max(quote.bid_price, market_state.bid_prices[-1])
            if quote.bid_price is not None
            else None
        )
        ask_price = (
            min(quote.ask_price, market_state.ask_prices[-1])
            if quote.ask_price is not None
            else None
        )
        return DesiredQuote(
            bid_price=bid_price,
            bid_quantity=quote.bid_quantity,
            ask_price=ask_price,
            ask_quantity=quote.ask_quantity,
        )

    def _half_spread(self, features: MarketFeatures | None) -> float:
        """Add scaled one-second RMS price volatility to fixed spread."""
        if features is None:
            raise RuntimeError("volatility strategy requires market features")
        fixed_half_spread = super()._half_spread(features)
        volatility_adjustment = (
            features.price_volatility * self.config.volatility_multiplier
            if features.price_volatility is not None
            else 0.0
        )
        return fixed_half_spread + volatility_adjustment


class FundingInventoryStrategy(BaselineStrategy):
    """Move the inventory target slowly against the latest funding rate."""

    def target_inventory(self, market_state: MarketState) -> float:
        """Map the latest funding signal to a bounded contrarian target."""
        funding_rate = market_state.funding_rate
        if funding_rate is None:
            return 0.0

        funding_strength = max(
            -1.0,
            min(1.0, funding_rate / self.config.funding_rate_scale),
        )
        target_limit = (
            self.config.max_inventory
            * self.config.funding_target_fraction
        )
        return -funding_strength * target_limit
