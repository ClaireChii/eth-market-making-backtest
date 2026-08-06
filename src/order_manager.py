"""Reconcile desired quotes with delayed simulated orders."""

from dataclasses import dataclass
from heapq import heappop, heappush
import math

from src.account import Account
from src.market_state import MarketState
from src.orders import (
    CancelOrderAction,
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    PlaceOrderAction,
)
from src.strategy import DesiredQuote


@dataclass(frozen=True, slots=True)
class OrderManagerConfig:
    """Configure order latency and the hard inventory boundary."""

    placement_latency_ns: int
    cancellation_latency_ns: int
    max_inventory: float

    def __post_init__(self) -> None:
        """Validate latency and inventory settings."""
        if self.placement_latency_ns < 0:
            raise ValueError("placement_latency_ns must be non-negative")
        if self.cancellation_latency_ns < 0:
            raise ValueError("cancellation_latency_ns must be non-negative")
        if not math.isfinite(self.max_inventory) or self.max_inventory <= 0:
            raise ValueError("max_inventory must be finite and positive")


class OrderManager:
    """Own orders and schedule changes required by desired quotes."""

    def __init__(self, config: OrderManagerConfig) -> None:
        """Initialize an empty order store and action queue."""
        self.config = config
        self.orders: dict[str, Order] = {}
        self._active_order_ids: set[str] = set()
        self._current_order_ids: dict[OrderSide, str | None] = {
            OrderSide.BUY: None,
            OrderSide.SELL: None,
        }
        self._action_heap: list[tuple[int, int, int, OrderAction]] = []
        self._next_order_number = 1
        self._next_action_sequence = 1

    # 1. Expose scheduled actions and currently maintained orders.

    @property
    def next_action_timestamp_ns(self) -> int | None:
        """Return the next scheduled action time, if any."""
        return self._action_heap[0][0] if self._action_heap else None

    @property
    def active_orders(self) -> tuple[Order, ...]:
        """Return all orders currently eligible for fills."""
        stale_ids = {
            order_id
            for order_id in self._active_order_ids
            if not self.orders[order_id].is_live
        }
        self._active_order_ids.difference_update(stale_ids)
        return tuple(
            self.orders[order_id]
            for order_id in self._active_order_ids
        )

    def current_order(self, side: OrderSide) -> Order | None:
        """Return the latest maintained order for one side."""
        order_id = self._current_order_ids[side]
        return self.orders.get(order_id) if order_id is not None else None

    # 2. Reconcile the strategy's desired bid and ask with existing orders.

    def reconcile(
        self,
        desired_quote: DesiredQuote,
        timestamp_ns: int,
    ) -> tuple[OrderAction, ...]:
        """Schedule the order changes required by a new desired quote."""
        scheduled: list[OrderAction] = []
        scheduled.extend(
            self._reconcile_side(
                side=OrderSide.BUY,
                price=desired_quote.bid_price,
                quantity=desired_quote.bid_quantity,
                timestamp_ns=timestamp_ns,
            )
        )
        scheduled.extend(
            self._reconcile_side(
                side=OrderSide.SELL,
                price=desired_quote.ask_price,
                quantity=desired_quote.ask_quantity,
                timestamp_ns=timestamp_ns,
            )
        )
        return tuple(scheduled)

    def _reconcile_side(
        self,
        side: OrderSide,
        price: float | None,
        quantity: float,
        timestamp_ns: int,
    ) -> list[OrderAction]:
        """Reconcile one desired quote side with its maintained order."""
        scheduled: list[OrderAction] = []
        current = self.current_order(side)

        # A. The strategy will not quote on this side.
        if price is None:
            if current is not None and current.status in {
                OrderStatus.PENDING,
                OrderStatus.ACTIVE,
            }:
                action = self._request_cancel(current, timestamp_ns)
                if action is not None:
                    scheduled.append(action)
            self._current_order_ids[side] = None
            return scheduled

        # B. Keep an existing order that already matches the desired quote.
        if (
            current is not None
            and not current.is_cancel_pending
            and current.status in {OrderStatus.PENDING, OrderStatus.ACTIVE}
            and math.isclose(current.price, price, abs_tol=1e-12)
            and math.isclose(current.total_quantity, quantity, abs_tol=1e-12)
        ):
            return scheduled

        # C. Replace an existing order that no longer matches the desired quote.
        if current is not None and current.status in {
            OrderStatus.PENDING,
            OrderStatus.ACTIVE,
        }:
            action = self._request_cancel(current, timestamp_ns)
            if action is not None:
                scheduled.append(action)

        order = Order(
            order_id=self._new_order_id(),
            side=side,
            price=price,
            total_quantity=quantity,
            submitted_at_ns=timestamp_ns,
        )
        self.orders[order.order_id] = order
        self._current_order_ids[side] = order.order_id

        place_action = PlaceOrderAction(
            order_id=order.order_id,
            effective_at_ns=(
                timestamp_ns + self.config.placement_latency_ns
            ),
        )
        self._schedule(place_action)
        scheduled.append(place_action)
        return scheduled

    # 3. Create and schedule latency-delayed order actions.
    def _request_cancel(
        self,
        order: Order,
        timestamp_ns: int,
    ) -> CancelOrderAction | None:
        """Request one cancellation unless it is already pending."""
        if order.is_cancel_pending:
            return None
        order.request_cancel(timestamp_ns)
        action = CancelOrderAction(
            order_id=order.order_id,
            effective_at_ns=(
                timestamp_ns + self.config.cancellation_latency_ns
            ),
        )
        self._schedule(action)
        return action

    def _new_order_id(self) -> str:
        """Return the next deterministic order identifier."""
        order_id = f"order-{self._next_order_number:08d}"
        self._next_order_number += 1
        return order_id

    def _schedule(self, action: OrderAction) -> None:
        """Push an action timestamp, stage, and insertion order."""
        heappush(
            self._action_heap,
            (
                action.timestamp_ns,
                int(action.stage),
                self._next_action_sequence,
                action,
            ),
        )
        self._next_action_sequence += 1

    # 4. Apply cancellations and placements.

    def process_actions_at(
        self,
        timestamp_ns: int,
        market_state: MarketState,
        account: Account,
    ) -> tuple[OrderAction, ...]:
        """Apply every action scheduled for one exact simulation timestamp."""
        next_timestamp = self.next_action_timestamp_ns
        if next_timestamp is not None and next_timestamp < timestamp_ns:
            raise ValueError(
                "scheduled order action was skipped: "
                f"{next_timestamp} < {timestamp_ns}"
            )

        processed: list[OrderAction] = []

        while (
            self._action_heap
            and self._action_heap[0][0] == timestamp_ns
        ):
            _, _, _, action = heappop(self._action_heap)
            order = self.orders[action.order_id]

            if isinstance(action, CancelOrderAction):
                self._apply_cancel(order, timestamp_ns)
            elif isinstance(action, PlaceOrderAction):
                self._apply_placement(order, timestamp_ns, market_state, account)
            else:
                raise TypeError(f"Unsupported order action: {type(action)!r}")

            processed.append(action)
        return tuple(processed)

    def _apply_cancel(self, order: Order, timestamp_ns: int) -> None:
        """Cancel a non-terminal order and clear its maintained-side pointer."""
        if order.status not in {OrderStatus.PENDING, OrderStatus.ACTIVE}:
            return
        order.cancel(timestamp_ns)
        self._active_order_ids.discard(order.order_id)
        if self._current_order_ids[order.side] == order.order_id:
            self._current_order_ids[order.side] = None

    def _apply_placement(
        self,
        order: Order,
        timestamp_ns: int,
        market_state: MarketState,
        account: Account,
    ) -> None:
        """Activate a pending order after applying inventory limit."""
        if order.status is not OrderStatus.PENDING:
            return

        if self._would_cross(order, market_state):
            self._cancel_before_activation(order, timestamp_ns)
            return

        available_capacity = self._available_capacity(order.side, account)
        if available_capacity <= 1e-12:
            self._cancel_before_activation(order, timestamp_ns)
            return
        if order.total_quantity > available_capacity:
            order.resize_pending(available_capacity)

        order.activate(
            timestamp_ns=timestamp_ns,
            queue_ahead=self._visible_queue_ahead(order, market_state),
        )
        self._active_order_ids.add(order.order_id)

    # 5. Enforce post-only behavior and the hard inventory limit.

    def _cancel_before_activation(
        self,
        order: Order,
        timestamp_ns: int,
    ) -> None:
        """Cancel an order before activation and clear its side pointer."""
        if order.cancel_requested_at_ns is None:
            order.request_cancel(timestamp_ns)
        order.cancel(timestamp_ns)
        if self._current_order_ids[order.side] == order.order_id:
            self._current_order_ids[order.side] = None

    @staticmethod
    def _would_cross(order: Order, market_state: MarketState) -> bool:
        """Return whether an order is marketable at activation."""
        best_bid = market_state.best_bid
        best_ask = market_state.best_ask
        if best_bid is None or best_ask is None:
            return True

        if order.side is OrderSide.BUY:
            return order.price >= best_ask or math.isclose(
                order.price,
                best_ask,
                abs_tol=1e-12,
            )
        return order.price <= best_bid or math.isclose(
            order.price,
            best_bid,
            abs_tol=1e-12,
        )

    def _available_capacity(
        self,
        side: OrderSide,
        account: Account,
    ) -> float:
        """Return remaining capacity after current live exposure."""
        live_exposure = sum(
            self.orders[order_id].remaining_quantity
            for order_id in self._active_order_ids
            if self.orders[order_id].is_live
            and self.orders[order_id].side is side
        )
        if side is OrderSide.BUY:
            return max(
                0.0,
                self.config.max_inventory
                - account.inventory
                - live_exposure,
            )
        return max(
            0.0,
            self.config.max_inventory
            + account.inventory
            - live_exposure,
        )

    # 6. Initialize visible queue position when an order becomes active.

    @staticmethod
    def _visible_queue_ahead(
        order: Order,
        market_state: MarketState,
    ) -> float:
        """Return displayed quantity at the order's exact price level."""
        if order.side is OrderSide.BUY:
            prices = market_state.bid_prices
            quantities = market_state.bid_quantities
        else:
            prices = market_state.ask_prices
            quantities = market_state.ask_quantities

        for price, quantity in zip(prices, quantities, strict=True):
            if math.isclose(price, order.price, abs_tol=1e-12):
                return quantity
        return 0.0
