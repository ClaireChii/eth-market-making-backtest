"""Market features derived causally from the current public state."""

from collections import deque
from dataclasses import dataclass
import math

from src.market_state import MarketState


@dataclass(frozen=True, slots=True)
class FeatureEngineConfig:
    """Configure fixed-time sampling for the trailing volatility feature."""

    volatility_sample_interval_ns: int = 1_000_000_000
    volatility_window: int = 60       # 60 log returns

    def __post_init__(self) -> None:
        """Validate positive sampling and lookback settings."""
        if self.volatility_sample_interval_ns <= 0:
            raise ValueError("volatility_sample_interval_ns must be positive")
        if self.volatility_window <= 0:
            raise ValueError("volatility_window must be positive")


@dataclass(frozen=True, slots=True)
class MarketFeatures:
    """Store the top-of-book features available to one strategy decision."""

    l1_imbalance: float
    microprice: float
    price_volatility: float | None = None


class FeatureEngine:
    """Calculate transparent market features without future information."""

    def __init__(self, config: FeatureEngineConfig | None = None) -> None:
        """Initialize an empty fixed-time volatility history."""
        self.config = config or FeatureEngineConfig()
        self._next_grid_time_ns: int | None = None
        self._last_processed_book_time_ns: int | None = None
        self._last_observed_mid: float | None = None
        self._last_sampled_mid: float | None = None
        self._log_returns: deque[float] = deque()
        self._squared_return_sum = 0.0

    def calculate(self, market_state: MarketState) -> MarketFeatures | None:
        """Return imbalance, microprice, and warmed-up price volatility."""
        if not market_state.has_order_book:
            return None

        best_bid = market_state.best_bid
        best_ask = market_state.best_ask
        bid_quantity = market_state.bid_quantities[0]
        ask_quantity = market_state.ask_quantities[0]
        if best_bid is None or best_ask is None:
            return None

        mid_price = (best_bid + best_ask) / 2
        self._advance_volatility_grid(market_state, mid_price)
        price_volatility = self._price_volatility(mid_price)

        total_quantity = bid_quantity + ask_quantity

        if total_quantity <= 0:
            return MarketFeatures(
                l1_imbalance=0.0,
                microprice=mid_price,
                price_volatility=price_volatility,
            )

        imbalance = (bid_quantity - ask_quantity) / total_quantity
        microprice = (
            best_ask * bid_quantity
            + best_bid * ask_quantity
        ) / total_quantity

        return MarketFeatures(
            l1_imbalance=imbalance,
            microprice=microprice,
            price_volatility=price_volatility,
        )

    def _advance_volatility_grid(
        self,
        market_state: MarketState,
        current_mid: float,
    ) -> None:
        """Sample causal midpoint on a fixed grid with last observation carry."""
        current_time_ns = market_state.current_timestamp_ns
        latest_book_time_ns = market_state.last_book_update_ns

        if current_time_ns is None or latest_book_time_ns is None:
            return

        if self._next_grid_time_ns is None:
            self._initialize_volatility_grid(current_time_ns, current_mid)
            self._last_processed_book_time_ns = latest_book_time_ns
            return

        new_book = (
            latest_book_time_ns != self._last_processed_book_time_ns
        )
        interval = self.config.volatility_sample_interval_ns
        while self._next_grid_time_ns < current_time_ns:
            # Fill every missing one-second sample before this event.
            self._append_mid_sample(self._required_last_observed_mid())
            self._next_grid_time_ns += interval

        if self._next_grid_time_ns == current_time_ns:
            sample_mid = (
                current_mid
                if new_book
                else self._required_last_observed_mid()
            )
            self._append_mid_sample(sample_mid)
            self._next_grid_time_ns += interval

        if new_book:
            self._last_observed_mid = current_mid
            self._last_processed_book_time_ns = latest_book_time_ns

    def _initialize_volatility_grid(
        self,
        current_time_ns: int,
        current_mid: float,
    ) -> None:
        """Set the first observed mid and the next whole-grid timestamp."""
        interval = self.config.volatility_sample_interval_ns
        self._last_observed_mid = current_mid
        remainder = current_time_ns % interval
        if remainder == 0:
            self._append_mid_sample(current_mid)
            self._next_grid_time_ns = current_time_ns + interval
        else:
            self._next_grid_time_ns = (
                current_time_ns + interval - remainder
            )

    def _append_mid_sample(self, mid_price: float) -> None:
        """Append one grid midpoint and update rolling squared log returns."""
        if self._last_sampled_mid is not None:
            log_return = math.log(mid_price / self._last_sampled_mid)
            if len(self._log_returns) == self.config.volatility_window:
                oldest_return = self._log_returns.popleft()
                self._squared_return_sum -= oldest_return ** 2
            self._log_returns.append(log_return)
            self._squared_return_sum += log_return ** 2
        self._last_sampled_mid = mid_price

    def _price_volatility(self, current_mid: float) -> float | None:
        """Return one-grid-interval RMS volatility in price units."""
        if len(self._log_returns) < self.config.volatility_window:
            return None
        return current_mid * math.sqrt(
            max(self._squared_return_sum, 0.0)
            / self.config.volatility_window
        )

    def _required_last_observed_mid(self) -> float:
        """Return the midpoint required after volatility initialization."""
        if self._last_observed_mid is None:
            raise RuntimeError("volatility grid requires an observed midpoint")
        return self._last_observed_mid
