"""Event types and timestamp batches used by the backtest."""

from dataclasses import dataclass
from enum import IntEnum


class TimestampStage(IntEnum):
    """Define the processing order for events sharing one timestamp."""

    TRADE = 1
    CANCEL_ACTIVATION = 2
    ORDER_ACTIVATION = 3

    ORDER_BOOK_UPDATE = 4

    FUNDING_UPDATE = 5

    MARK_TO_MARKET = 6
    STRATEGY_DECISION = 7


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """Represent one public market trade."""

    timestamp_ns: int
    price: float
    size: float
    is_maker_ask: bool

    @property
    def stage(self) -> TimestampStage:
        """Return the event's position in same-timestamp processing."""
        return TimestampStage.TRADE


@dataclass(frozen=True, slots=True)
class OrderBookEvent:
    """Represent one multi-level order-book snapshot."""

    timestamp_ns: int
    bid_prices: tuple[float, ...]
    ask_prices: tuple[float, ...]
    bid_quantities: tuple[float, ...]
    ask_quantities: tuple[float, ...]

    @property
    def stage(self) -> TimestampStage:
        """Return the event's position in same-timestamp processing."""
        return TimestampStage.ORDER_BOOK_UPDATE


@dataclass(frozen=True, slots=True)
class FundingUpdateEvent:
    """Represent one funding-rate signal update, not a cash settlement."""

    timestamp_ns: int
    funding_rate: float

    @property
    def stage(self) -> TimestampStage:
        """Return the event's position in same-timestamp processing."""
        return TimestampStage.FUNDING_UPDATE


MarketEvent = TradeEvent | OrderBookEvent | FundingUpdateEvent


@dataclass(frozen=True, slots=True)
class MarketEventBatch:
    """Group all public market events that share 'one timestamp'."""

    timestamp_ns: int
    trades: tuple[TradeEvent, ...]
    order_books: tuple[OrderBookEvent, ...]
    funding_updates: tuple[FundingUpdateEvent, ...]
