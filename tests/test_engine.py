"""End-to-end tests for causal market and order-action processing."""

import unittest

from src.config import StrategyConfig
from src.engine import BacktestEngine
from src.events import MarketEventBatch, OrderBookEvent, TradeEvent
from src.fill_model import FillModel, FillModelConfig
from src.order_manager import OrderManager, OrderManagerConfig
from src.orders import OrderSide, OrderStatus
from src.strategy import BaselineStrategy


def book_event(timestamp_ns: int, ask_quantity: float = 1.0) -> OrderBookEvent:
    """Build a one-level book centered at 100 for engine tests."""
    return OrderBookEvent(
        timestamp_ns=timestamp_ns,
        bid_prices=(99.9,),
        ask_prices=(100.1,),
        bid_quantities=(1.0,),
        ask_quantities=(ask_quantity,),
    )


def batch(
    timestamp_ns: int,
    trades: tuple[TradeEvent, ...] = (),
    books: tuple[OrderBookEvent, ...] = (),
) -> MarketEventBatch:
    """Build a timestamp batch with no funding updates."""
    return MarketEventBatch(timestamp_ns, trades, books, ())


def engine(latency_ns: int = 100) -> BacktestEngine:
    """Build a zero-skew baseline engine with synthetic timing."""
    strategy = BaselineStrategy(
        StrategyConfig(
            tick_size=0.1,
            order_quantity=0.2,
            max_inventory=1.0,
            fixed_spread_ticks=1.0,
            inventory_skew_ticks=0.0,
        )
    )
    order_manager = OrderManager(
        OrderManagerConfig(
            placement_latency_ns=latency_ns,
            cancellation_latency_ns=latency_ns,
            max_inventory=1.0,
        )
    )
    return BacktestEngine(
        strategy=strategy,
        order_manager=order_manager,
        fill_model=FillModel(FillModelConfig()),
    )


class BacktestEngineTest(unittest.TestCase):
    """Verify latency, no-lookahead ordering, fills, and account updates."""

    def test_private_action_between_market_events_activates_exactly(self) -> None:
        """Activate orders at their own timestamp before the next public trade."""
        backtest = engine(latency_ns=100)

        fills = backtest.run(
            (
                batch(100, books=(book_event(100, ask_quantity=0.0),)),
                batch(
                    300,
                    trades=(TradeEvent(300, 100.1, 0.2, True),),
                ),
            )
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].side, OrderSide.SELL)
        self.assertEqual(fills[0].timestamp_ns, 300)
        self.assertAlmostEqual(backtest.account.inventory, -0.2)
        self.assertAlmostEqual(backtest.account.equity, 0.02)

    def test_trade_at_activation_timestamp_cannot_fill_new_order(self) -> None:
        """Process a same-time trade before placement activation."""
        backtest = engine(latency_ns=100)

        fills = backtest.run(
            (
                batch(100, books=(book_event(100, ask_quantity=0.0),)),
                batch(
                    200,
                    trades=(TradeEvent(200, 100.1, 0.2, True),),
                ),
                batch(
                    300,
                    trades=(TradeEvent(300, 100.1, 0.2, True),),
                ),
            )
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].timestamp_ns, 300)

    def test_activation_uses_book_before_same_timestamp_update(self) -> None:
        """Initialize queue from causal state before applying a new book."""
        backtest = engine(latency_ns=100)

        backtest.run(
            (
                batch(100, books=(book_event(100, ask_quantity=1.0),)),
                batch(200, books=(book_event(200, ask_quantity=10.0),)),
            )
        )

        ask = backtest.order_manager.current_order(OrderSide.SELL)
        self.assertEqual(ask.status, OrderStatus.ACTIVE)
        self.assertEqual(ask.queue_ahead, 1.0)

    def test_zero_latency_order_cannot_fill_creating_timestamp(self) -> None:
        """Activate zero-latency quotes only after current public trades."""
        backtest = engine(latency_ns=0)

        fills = backtest.run(
            (
                batch(
                    100,
                    trades=(TradeEvent(100, 100.1, 1.0, True),),
                    books=(book_event(100, ask_quantity=0.0),),
                ),
                batch(
                    200,
                    trades=(TradeEvent(200, 100.1, 0.2, True),),
                ),
            )
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].timestamp_ns, 200)


if __name__ == "__main__":
    unittest.main()
