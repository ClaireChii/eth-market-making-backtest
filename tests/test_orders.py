"""Tests for order lifecycle transitions and delayed actions."""

import unittest

from src.events import TimestampStage
from src.orders import (
    CancelOrderAction,
    Order,
    OrderSide,
    OrderStatus,
    PlaceOrderAction,
)


def pending_order() -> Order:
    """Build a valid pending buy order for tests."""
    return Order(
        order_id="order-1",
        side=OrderSide.BUY,
        price=100.0,
        total_quantity=1.0,
        submitted_at_ns=100,
    )


class OrderTest(unittest.TestCase):
    """Verify order validation, fills, cancellation, and action priority."""

    def test_new_order_is_pending_with_full_remaining_quantity(self) -> None:
        """Initialize a submitted order without making it live."""
        order = pending_order()

        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(order.remaining_quantity, 1.0)
        self.assertEqual(order.filled_quantity, 0.0)
        self.assertFalse(order.is_live)

    def test_invalid_price_and_quantity_are_rejected(self) -> None:
        """Reject invalid side, price, and quantity values."""
        with self.assertRaises(TypeError):
            Order("bad-side", "buy", 100.0, 1.0, 100)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Order("bad-price", OrderSide.BUY, 0.0, 1.0, 100)
        with self.assertRaises(ValueError):
            Order("bad-quantity", OrderSide.SELL, 100.0, 0.0, 100)

    def test_activation_assigns_visible_queue(self) -> None:
        """Make a pending order live after its placement delay."""
        order = pending_order()

        order.activate(timestamp_ns=200, queue_ahead=3.5)

        self.assertEqual(order.status, OrderStatus.ACTIVE)
        self.assertEqual(order.activated_at_ns, 200)
        self.assertEqual(order.queue_ahead, 3.5)
        self.assertTrue(order.is_live)

    def test_partial_and_complete_fills(self) -> None:
        """Keep partial fills active and make full fills terminal."""
        order = pending_order()
        order.activate(timestamp_ns=200, queue_ahead=0.0)

        order.apply_fill(0.4)

        self.assertEqual(order.status, OrderStatus.ACTIVE)
        self.assertTrue(order.is_partially_filled)
        self.assertEqual(order.total_quantity, 1.0)
        self.assertAlmostEqual(order.filled_quantity, 0.4)
        self.assertAlmostEqual(order.remaining_quantity, 0.6)
        self.assertAlmostEqual(
            order.total_quantity,
            order.filled_quantity + order.remaining_quantity,
        )

        order.apply_fill(0.6)

        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.remaining_quantity, 0.0)
        self.assertFalse(order.is_live)

    def test_cancel_request_does_not_immediately_remove_active_order(self) -> None:
        """Keep an order fillable until its cancellation becomes effective."""
        order = pending_order()
        order.activate(timestamp_ns=200, queue_ahead=1.0)

        order.request_cancel(timestamp_ns=250)

        self.assertEqual(order.status, OrderStatus.ACTIVE)
        self.assertTrue(order.is_live)
        self.assertTrue(order.is_cancel_pending)

        order.cancel(timestamp_ns=350)

        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.cancelled_at_ns, 350)
        self.assertFalse(order.is_live)

    def test_pending_order_can_be_cancelled_before_activation(self) -> None:
        """Allow a cancellation action to terminate a still-pending order."""
        order = pending_order()
        order.request_cancel(timestamp_ns=110)

        order.cancel(timestamp_ns=210)

        self.assertEqual(order.status, OrderStatus.CANCELLED)
        with self.assertRaises(ValueError):
            order.activate(timestamp_ns=220, queue_ahead=0.0)

    def test_fill_cannot_exceed_remaining_quantity(self) -> None:
        """Reject fills larger than the live order's remaining size."""
        order = pending_order()
        order.activate(timestamp_ns=200, queue_ahead=0.0)

        with self.assertRaises(ValueError):
            order.apply_fill(1.1)

    def test_cancellation_precedes_placement_at_equal_time(self) -> None:
        """Expose action stages that implement the agreed timestamp order."""
        cancel = CancelOrderAction("order-1", effective_at_ns=200)
        place = PlaceOrderAction("order-1", effective_at_ns=200)

        self.assertEqual(cancel.stage, TimestampStage.CANCEL_ACTIVATION)
        self.assertEqual(place.stage, TimestampStage.ORDER_ACTIVATION)
        self.assertLess(cancel.stage, place.stage)


if __name__ == "__main__":
    unittest.main()
