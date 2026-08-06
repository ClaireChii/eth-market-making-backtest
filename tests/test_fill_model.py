"""Tests for visible-queue fill simulation."""

import unittest

from src.events import TradeEvent
from src.fill_model import FillModel, FillModelConfig
from src.orders import Order, OrderSide, OrderStatus


def active_order(
    side: OrderSide,
    price: float,
    quantity: float = 0.5,
    queue_ahead: float = 0.0,
    order_id: str = "order-1",
) -> Order:
    """Build one active order with a known queue position."""
    order = Order(order_id, side, price, quantity, submitted_at_ns=100)
    order.activate(timestamp_ns=200, queue_ahead=queue_ahead)
    return order


class FillModelTest(unittest.TestCase):
    """Verify direction, queue, partial fill, pass-through, and fees."""

    def setUp(self) -> None:
        """Create the primary zero-fee fill model."""
        self.model = FillModel(FillModelConfig())

    def test_trade_direction_selects_the_resting_side(self) -> None:
        """Match buyer aggressors to asks and seller aggressors to bids."""
        bid = active_order(OrderSide.BUY, 99.9, order_id="bid")
        ask = active_order(OrderSide.SELL, 100.1, order_id="ask")

        buy_aggressor_fills = self.model.process_trade(
            TradeEvent(300, 100.1, 1.0, True),
            (bid, ask),
        )

        self.assertEqual([fill.order_id for fill in buy_aggressor_fills], ["ask"])
        self.assertEqual(bid.status, OrderStatus.ACTIVE)

    def test_touch_trade_consumes_queue_before_filling_order(self) -> None:
        """Use equal-price volume first against visible queue ahead."""
        order = active_order(
            OrderSide.BUY,
            price=99.9,
            quantity=0.5,
            queue_ahead=2.0,
        )

        no_fills = self.model.process_trade(
            TradeEvent(300, 99.9, 1.5, False),
            (order,),
        )
        fills = self.model.process_trade(
            TradeEvent(301, 99.9, 0.8, False),
            (order,),
        )

        self.assertEqual(no_fills, ())
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].quantity, 0.3)
        self.assertAlmostEqual(order.queue_ahead, 0.0)
        self.assertAlmostEqual(order.remaining_quantity, 0.2)

    def test_later_trade_completes_partially_filled_order(self) -> None:
        """Continue filling the remaining quantity across public trades."""
        order = active_order(OrderSide.SELL, 100.1, quantity=0.5)

        first = self.model.process_trade(
            TradeEvent(300, 100.1, 0.2, True),
            (order,),
        )
        second = self.model.process_trade(
            TradeEvent(301, 100.1, 0.4, True),
            (order,),
        )

        self.assertEqual(first[0].quantity, 0.2)
        self.assertAlmostEqual(second[0].quantity, 0.3)
        self.assertEqual(order.status, OrderStatus.FILLED)

    def test_trade_through_fills_entire_order(self) -> None:
        """Treat a worse public price as proof that better queue was consumed."""
        order = active_order(
            OrderSide.SELL,
            price=100.1,
            quantity=0.5,
            queue_ahead=10.0,
        )

        fills = self.model.process_trade(
            TradeEvent(300, 100.2, 0.01, True),
            (order,),
        )

        self.assertEqual(fills[0].quantity, 0.5)
        self.assertEqual(fills[0].price, 100.1)
        self.assertEqual(order.status, OrderStatus.FILLED)

    def test_cancel_pending_order_remains_fillable(self) -> None:
        """Allow fills until delayed cancellation becomes effective."""
        order = active_order(OrderSide.BUY, 99.9, quantity=0.5)
        order.request_cancel(timestamp_ns=250)

        fills = self.model.process_trade(
            TradeEvent(300, 99.9, 0.5, False),
            (order,),
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(order.status, OrderStatus.FILLED)

    def test_trade_volume_is_not_reused_across_orders(self) -> None:
        """Allocate one equal-price trade size across orders in priority order."""
        first = active_order(
            OrderSide.BUY,
            99.9,
            quantity=0.2,
            order_id="first",
        )
        second = active_order(
            OrderSide.BUY,
            99.9,
            quantity=0.2,
            order_id="second",
        )

        fills = self.model.process_trade(
            TradeEvent(300, 99.9, 0.3, False),
            (first, second),
        )

        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(fills[0].quantity, 0.2)
        self.assertAlmostEqual(fills[1].quantity, 0.1)
        self.assertEqual(first.status, OrderStatus.FILLED)
        self.assertAlmostEqual(second.remaining_quantity, 0.1)

    def test_maker_cost_and_rebate_have_opposite_cashflow_signs(self) -> None:
        """Convert positive fee bps to cost and negative bps to rebate."""
        cost_order = active_order(OrderSide.BUY, 100.0, quantity=1.0)
        rebate_order = active_order(
            OrderSide.BUY,
            100.0,
            quantity=1.0,
            order_id="rebate",
        )
        cost_model = FillModel(FillModelConfig(maker_fee_bps=1.0))
        rebate_model = FillModel(FillModelConfig(maker_fee_bps=-1.0))

        cost = cost_model.process_trade(
            TradeEvent(300, 100.0, 1.0, False),
            (cost_order,),
        )[0]
        rebate = rebate_model.process_trade(
            TradeEvent(300, 100.0, 1.0, False),
            (rebate_order,),
        )[0]

        self.assertAlmostEqual(cost.fee_cashflow, -0.01)
        self.assertAlmostEqual(rebate.fee_cashflow, 0.01)


if __name__ == "__main__":
    unittest.main()
