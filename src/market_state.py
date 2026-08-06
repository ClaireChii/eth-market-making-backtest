"""Current public market state used by the backtest."""

from dataclasses import dataclass

from src.events import FundingUpdateEvent, OrderBookEvent


@dataclass(slots=True)
class MarketState:
    """Store the latest order book and funding signal at simulation time."""

    current_timestamp_ns: int | None = None
    last_book_update_ns: int | None = None
    bid_prices: tuple[float, ...] = ()
    ask_prices: tuple[float, ...] = ()
    bid_quantities: tuple[float, ...] = ()
    ask_quantities: tuple[float, ...] = ()
    last_funding_update_ns: int | None = None
    funding_rate: float | None = None

    def advance_to(self, timestamp_ns: int) -> None:
        """Advance simulation time without changing the latest market data."""
        if (
            self.current_timestamp_ns is not None
            and timestamp_ns < self.current_timestamp_ns
        ):
            raise ValueError(
                "MarketState time cannot move backwards: "
                f"{timestamp_ns} < {self.current_timestamp_ns}"
            )
        self.current_timestamp_ns = timestamp_ns

    def apply_order_book(self, event: OrderBookEvent) -> None:
        """Replace the current order book with the latest snapshot."""
        self.advance_to(event.timestamp_ns)
        self.last_book_update_ns = event.timestamp_ns
        self.bid_prices = event.bid_prices
        self.ask_prices = event.ask_prices
        self.bid_quantities = event.bid_quantities
        self.ask_quantities = event.ask_quantities

    def apply_funding_update(self, event: FundingUpdateEvent) -> None:
        """Update the funding signal without creating a cash settlement."""
        self.advance_to(event.timestamp_ns)
        self.last_funding_update_ns = event.timestamp_ns
        self.funding_rate = event.funding_rate

    @property
    def has_order_book(self) -> bool:
        """Return whether at least one bid and ask are available."""
        return bool(self.bid_prices and self.ask_prices)

    @property
    def best_bid(self) -> float | None:
        """Return the highest current bid, or None before the first book."""
        return self.bid_prices[0] if self.bid_prices else None

    @property
    def best_ask(self) -> float | None:
        """Return the lowest current ask, or None before the first book."""
        return self.ask_prices[0] if self.ask_prices else None

    @property
    def mid_price(self) -> float | None:
        """Return the current top-of-book midpoint."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        """Return the current top-of-book spread."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid
