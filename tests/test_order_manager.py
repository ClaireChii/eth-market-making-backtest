"""Tests for desired-quote reconciliation and delayed order actions."""

import unittest

from src.account import Account
from src.events import OrderBookEvent
from src.fills import Fill
from src.market_state import MarketState
from src.order_manager import OrderManager, OrderManagerConfig
from src.orders import OrderSide, OrderStatus
from src.strategy import DesiredQuote


def manager() -> OrderManager:
    """Build an order manager with 100 ns synthetic latency."""
    return OrderManager(
        OrderManagerConfig(
            placement_latency_ns=100,
            cancellation_latency_ns=100,
            max_inventory=1.0,
        )
    )


def market_state() -> MarketState:
    """Build a small visible book used for queue initialization."""
    state = MarketState()
    state.apply_order_book(
        OrderBookEvent(
            timestamp_ns=100,
            bid_prices=(99.9, 99.8),
            ask_prices=(100.1, 100.2),
            bid_quantities=(2.0, 3.0),
            ask_quantities=(4.0, 5.0),
        )
    )
    return state


def moved_market_state() -> MarketState:
    """Build a moved book that makes the original bid marketable."""
    state = MarketState()
    state.apply_order_book(
        OrderBookEvent(
            timestamp_ns=200,
            bid_prices=(99.7,),
            ask_prices=(99.8,),
            bid_quantities=(2.0,),
            ask_quantities=(4.0,),
        )
    )
    return state


def desired_quote(
    bid_price: float | None = 99.9,
    bid_quantity: float = 0.2,
    ask_price: float | None = 100.1,
    ask_quantity: float = 0.2,
) -> DesiredQuote:
    """Build a desired quote while keeping absent sides valid."""
    return DesiredQuote(
        bid_price=bid_price,
        bid_quantity=bid_quantity if bid_price is not None else 0.0,
        ask_price=ask_price,
        ask_quantity=ask_quantity if ask_price is not None else 0.0,
    )


class OrderManagerTest(unittest.TestCase):
    """Verify placement, retention, replacement, queue, and risk behavior."""

    def test_initial_quote_schedules_and_activates_both_sides(self) -> None:
        """Create two pending orders and activate them after latency."""
        order_manager = manager()
        actions = order_manager.reconcile(desired_quote(), timestamp_ns=100)

        self.assertEqual(len(actions), 2)
        self.assertEqual(order_manager.next_action_timestamp_ns, 200)
        self.assertEqual(
            order_manager.current_order(OrderSide.BUY).status,
            OrderStatus.PENDING,
        )

        order_manager.process_actions_at(200, market_state(), Account())

        bid = order_manager.current_order(OrderSide.BUY)
        ask = order_manager.current_order(OrderSide.SELL)
        self.assertEqual(bid.status, OrderStatus.ACTIVE)
        self.assertEqual(ask.status, OrderStatus.ACTIVE)
        self.assertEqual(bid.queue_ahead, 2.0)
        self.assertEqual(ask.queue_ahead, 4.0)

    def test_unchanged_quote_preserves_order_and_queue(self) -> None:
        """Do nothing when price and quantity match the maintained order."""
        order_manager = manager()
        order_manager.reconcile(desired_quote(), 100)
        order_manager.process_actions_at(200, market_state(), Account())
        bid = order_manager.current_order(OrderSide.BUY)

        actions = order_manager.reconcile(desired_quote(), 250)

        self.assertEqual(actions, ())
        self.assertIs(order_manager.current_order(OrderSide.BUY), bid)
        self.assertEqual(bid.queue_ahead, 2.0)

    def test_replacement_cancels_old_before_activating_new(self) -> None:
        """Replace a quote at one effective time without active overlap."""
        order_manager = manager()
        order_manager.reconcile(desired_quote(), 100)
        order_manager.process_actions_at(200, market_state(), Account())
        old_bid = order_manager.current_order(OrderSide.BUY)

        actions = order_manager.reconcile(
            desired_quote(bid_price=99.8),
            timestamp_ns=250,
        )
        new_bid = order_manager.current_order(OrderSide.BUY)

        self.assertEqual(len(actions), 2)
        self.assertTrue(old_bid.is_cancel_pending)
        self.assertEqual(new_bid.status, OrderStatus.PENDING)

        order_manager.process_actions_at(350, market_state(), Account())

        self.assertEqual(old_bid.status, OrderStatus.CANCELLED)
        self.assertEqual(new_bid.status, OrderStatus.ACTIVE)
        self.assertEqual(new_bid.queue_ahead, 3.0)
        active_bids = [
            order
            for order in order_manager.active_orders
            if order.side is OrderSide.BUY
        ]
        self.assertEqual(active_bids, [new_bid])

    def test_empty_side_schedules_cancellation_only(self) -> None:
        """Cancel an existing side when the desired quote removes it."""
        order_manager = manager()
        order_manager.reconcile(desired_quote(), 100)
        order_manager.process_actions_at(200, market_state(), Account())
        old_bid = order_manager.current_order(OrderSide.BUY)

        actions = order_manager.reconcile(
            desired_quote(bid_price=None),
            timestamp_ns=250,
        )

        self.assertEqual(len(actions), 1)
        self.assertIsNone(order_manager.current_order(OrderSide.BUY))
        order_manager.process_actions_at(350, market_state(), Account())
        self.assertEqual(old_bid.status, OrderStatus.CANCELLED)

    def test_activation_resizes_to_remaining_inventory_capacity(self) -> None:
        """Enforce the hard limit using inventory available at activation."""
        order_manager = manager()
        account = Account()
        account.apply_fill(
            Fill(100, "inventory", OrderSide.BUY, 100.0, 0.9)
        )
        order_manager.reconcile(
            desired_quote(ask_price=None),
            timestamp_ns=100,
        )

        order_manager.process_actions_at(200, market_state(), account)

        bid = order_manager.current_order(OrderSide.BUY)
        self.assertEqual(bid.status, OrderStatus.ACTIVE)
        self.assertAlmostEqual(bid.total_quantity, 0.1)

    def test_marketable_post_only_order_is_rejected_at_activation(self) -> None:
        """Do not activate a delayed bid that now crosses the current ask."""
        order_manager = manager()
        order_manager.reconcile(
            desired_quote(ask_price=None),
            timestamp_ns=100,
        )
        bid = order_manager.current_order(OrderSide.BUY)

        order_manager.process_actions_at(
            200,
            moved_market_state(),
            Account(),
        )

        self.assertEqual(bid.status, OrderStatus.CANCELLED)
        self.assertIsNone(bid.activated_at_ns)
        self.assertEqual(order_manager.active_orders, ())

    def test_risk_rejection_handles_existing_cancel_request(self) -> None:
        """Reject a pending order even when its delayed cancel is outstanding."""
        order_manager = manager()
        account = Account()
        account.apply_fill(
            Fill(100, "inventory", OrderSide.BUY, 100.0, 1.0)
        )
        order_manager.reconcile(
            desired_quote(ask_price=None),
            timestamp_ns=100,
        )
        old_bid = order_manager.current_order(OrderSide.BUY)
        order_manager.reconcile(
            desired_quote(bid_price=99.8, ask_price=None),
            timestamp_ns=150,
        )

        order_manager.process_actions_at(200, market_state(), account)

        self.assertEqual(old_bid.status, OrderStatus.CANCELLED)
        self.assertEqual(old_bid.cancelled_at_ns, 200)

    def test_skipped_action_timestamp_is_rejected(self) -> None:
        """Require the event loop to process scheduled actions exactly."""
        order_manager = manager()
        order_manager.reconcile(desired_quote(), 100)

        with self.assertRaises(ValueError):
            order_manager.process_actions_at(201, market_state(), Account())


if __name__ == "__main__":
    unittest.main()
