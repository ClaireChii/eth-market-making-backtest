"""Conservative visible-queue fill simulation for resting orders."""

from collections.abc import Iterable
from dataclasses import dataclass
import math

from src.events import TradeEvent
from src.fills import Fill
from src.orders import Order, OrderSide


@dataclass(frozen=True, slots=True)
class FillModelConfig:
    """Configure maker costs in basis points of executed notional."""

    maker_fee_bps: float = 0.0

    def __post_init__(self) -> None:
        """Allow finite positive fees, zero fees, or negative rebates."""
        if not math.isfinite(self.maker_fee_bps):
            raise ValueError("maker_fee_bps must be finite")


class FillModel:
    """Convert public trades into fills for eligible active orders."""

    PRICE_TOLERANCE = 1e-9

    def __init__(self, config: FillModelConfig) -> None:
        """Store the fee convention used for generated fills."""
        self.config = config

    def process_trade(
        self,
        trade: TradeEvent,
        orders: Iterable[Order],
    ) -> tuple[Fill, ...]:
        """Apply one trade to eligible orders and return resulting fills."""
        if not math.isfinite(trade.price) or trade.price <= 0:
            raise ValueError("trade price must be finite and positive")
        if not math.isfinite(trade.size) or trade.size <= 0:
            raise ValueError("trade size must be finite and positive")

        eligible_orders = sorted(
            (
                order
                for order in orders
                if self._is_eligible(order, trade)
            ),
            key=self._execution_priority,
        )
        remaining_trade_size = trade.size
        fills: list[Fill] = []

        for order in eligible_orders:
            if self._trade_passed_order(trade, order):
                fill_quantity = order.remaining_quantity
            else:
                if remaining_trade_size <= 0:
                    break
                remaining_trade_size = self._consume_queue(
                    order,
                    remaining_trade_size,
                )
                fill_quantity = min(
                    order.remaining_quantity,
                    remaining_trade_size,
                )
                remaining_trade_size -= fill_quantity

            if fill_quantity <= 0:
                continue

            order.apply_fill(fill_quantity)
            fills.append(
                Fill(
                    timestamp_ns=trade.timestamp_ns,
                    order_id=order.order_id,
                    side=order.side,
                    price=order.price,
                    quantity=fill_quantity,
                    fee_cashflow=self._fee_cashflow(
                        order.price,
                        fill_quantity,
                    ),
                )
            )

        return tuple(fills)

    @staticmethod
    def _is_eligible(order: Order, trade: TradeEvent) -> bool:
        """Return whether trade direction and price can reach the order."""
        if not order.is_live:
            return False
        if trade.is_maker_ask:
            return (
                order.side is OrderSide.SELL
                and (
                    trade.price > order.price
                    or math.isclose(
                        trade.price,
                        order.price,
                        rel_tol=0.0,
                        abs_tol=FillModel.PRICE_TOLERANCE,
                    )
                )
            )
        return (
            order.side is OrderSide.BUY
            and (
                trade.price < order.price
                or math.isclose(
                    trade.price,
                    order.price,
                    rel_tol=0.0,
                    abs_tol=FillModel.PRICE_TOLERANCE,
                )
            )
        )

    @staticmethod
    def _execution_priority(order: Order) -> tuple[float, int, str]:
        """Sort better prices first, then activation time and order ID."""
        price_priority = (
            order.price
            if order.side is OrderSide.SELL
            else -order.price
        )
        return (
            price_priority,
            order.activated_at_ns or 0,
            order.order_id,
        )

    @staticmethod
    def _trade_passed_order(trade: TradeEvent, order: Order) -> bool:
        """Return whether a worse trade price proves the order was consumed."""
        prices_equal = math.isclose(
            trade.price,
            order.price,
            rel_tol=0.0,
            abs_tol=FillModel.PRICE_TOLERANCE,
        )
        if order.side is OrderSide.SELL:
            return trade.price > order.price and not prices_equal
        return trade.price < order.price and not prices_equal

    @staticmethod
    def _consume_queue(order: Order, trade_size: float) -> float:
        """Consume visible queue ahead and return trade size left for our order."""
        if order.queue_ahead is None:
            raise RuntimeError("active order requires queue_ahead")
        queue_consumed = min(order.queue_ahead, trade_size)
        order.queue_ahead -= queue_consumed
        return trade_size - queue_consumed

    def _fee_cashflow(self, price: float, quantity: float) -> float:
        """Convert signed maker fee basis points into account cashflow."""
        return -(
            price
            * quantity
            * self.config.maker_fee_bps
            / 10_000
        )
