"""Order state and delayed order actions used by the backtest."""

from dataclasses import dataclass, field
from enum import Enum
import math

from src.events import TimestampStage


class OrderSide(str, Enum):
    """Identify whether an order buys or sells the instrument."""

    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """Describe the current lifecycle state of an order."""

    PENDING = "pending"
    ACTIVE = "active"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Order:
    """Represent one simulated limit order and its lifecycle."""

    order_id: str
    side: OrderSide
    price: float
    total_quantity: float
    submitted_at_ns: int
    remaining_quantity: float = field(init=False)
    activated_at_ns: int | None = None
    cancel_requested_at_ns: int | None = None
    cancelled_at_ns: int | None = None
    queue_ahead: float | None = None
    status: OrderStatus = field(init=False, default=OrderStatus.PENDING)

    def __post_init__(self) -> None:
        """Validate immutable submission fields and initialize remaining size."""
        if not self.order_id:
            raise ValueError("order_id must not be empty")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be finite and positive")
        if not math.isfinite(self.total_quantity) or self.total_quantity <= 0:
            raise ValueError("total_quantity must be finite and positive")
        self.remaining_quantity = self.total_quantity

    @property
    def filled_quantity(self) -> float:
        """Return the cumulative quantity filled on this order."""
        return self.total_quantity - self.remaining_quantity

    @property
    def is_live(self) -> bool:
        """Return whether the order can currently receive fills."""
        return self.status is OrderStatus.ACTIVE

    @property
    def is_partially_filled(self) -> bool:
        """Return whether the active order has received a partial fill."""
        return self.is_live and self.filled_quantity > 0

    @property
    def is_cancel_pending(self) -> bool:
        """Return whether cancellation was requested but has not taken effect."""
        return (
            self.cancel_requested_at_ns is not None
            and self.status in {OrderStatus.PENDING, OrderStatus.ACTIVE}
        )

    def activate(self, timestamp_ns: int, queue_ahead: float) -> None:
        """Make a pending order live and assign its initial visible queue."""
        if self.status is not OrderStatus.PENDING:
            raise ValueError(f"Cannot activate {self.status.value} order")
        if timestamp_ns < self.submitted_at_ns:
            raise ValueError("activation cannot precede submission")
        if not math.isfinite(queue_ahead) or queue_ahead < 0:
            raise ValueError("queue_ahead must be finite and non-negative")

        self.activated_at_ns = timestamp_ns
        self.queue_ahead = queue_ahead
        self.status = OrderStatus.ACTIVE

    def resize_pending(self, total_quantity: float) -> None:
        """Reduce a pending order before activation to satisfy a risk limit."""
        if self.status is not OrderStatus.PENDING:
            raise ValueError("only pending orders can be resized")
        if not math.isfinite(total_quantity) or total_quantity <= 0:
            raise ValueError("resized quantity must be finite and positive")
        if total_quantity > self.total_quantity:
            raise ValueError("pending order quantity cannot be increased")

        self.total_quantity = total_quantity
        self.remaining_quantity = total_quantity

    def request_cancel(self, timestamp_ns: int) -> None:
        """Record a cancellation request without immediately removing the order."""
        if self.status not in {OrderStatus.PENDING, OrderStatus.ACTIVE}:
            raise ValueError(f"Cannot cancel {self.status.value} order")
        if self.cancel_requested_at_ns is not None:
            raise ValueError("cancellation has already been requested")
        if timestamp_ns < self.submitted_at_ns:
            raise ValueError("cancellation request cannot precede submission")

        self.cancel_requested_at_ns = timestamp_ns

    def cancel(self, timestamp_ns: int) -> None:
        """Apply a requested cancellation when its latency has elapsed."""
        if self.status not in {OrderStatus.PENDING, OrderStatus.ACTIVE}:
            raise ValueError(f"Cannot cancel {self.status.value} order")
        if self.cancel_requested_at_ns is None:
            raise ValueError("cancellation must be requested first")
        if timestamp_ns < self.cancel_requested_at_ns:
            raise ValueError("cancellation cannot precede its request")

        self.cancelled_at_ns = timestamp_ns
        self.status = OrderStatus.CANCELLED

    def apply_fill(self, quantity: float) -> None:
        """Reduce remaining size and mark a fully filled order as terminal."""
        if not self.is_live:
            raise ValueError("only active orders can receive fills")
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("fill quantity must be finite and positive")
        if quantity > self.remaining_quantity:
            raise ValueError("fill quantity exceeds remaining order quantity")

        self.remaining_quantity -= quantity
        if math.isclose(self.remaining_quantity, 0.0, abs_tol=1e-12):
            self.remaining_quantity = 0.0
            self.status = OrderStatus.FILLED


@dataclass(frozen=True, slots=True)
class PlaceOrderAction:
    """Schedule a submitted order to become active after placement latency."""

    order_id: str
    effective_at_ns: int

    @property
    def timestamp_ns(self) -> int:
        """Expose the effective time for event scheduling."""
        return self.effective_at_ns

    @property
    def stage(self) -> TimestampStage:
        """Place activations after same-timestamp cancellations."""
        return TimestampStage.ORDER_ACTIVATION


@dataclass(frozen=True, slots=True)
class CancelOrderAction:
    """Schedule a requested cancellation to take effect after latency."""

    order_id: str
    effective_at_ns: int

    @property
    def timestamp_ns(self) -> int:
        """Expose the effective time for event scheduling."""
        return self.effective_at_ns

    @property
    def stage(self) -> TimestampStage:
        """Place cancellations before same-timestamp order activations."""
        return TimestampStage.CANCEL_ACTIVATION


OrderAction = PlaceOrderAction | CancelOrderAction
