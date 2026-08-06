"""Tests for compact recording and core performance metrics."""

import unittest

from src.account import Account
from src.config import StrategyConfig
from src.engine import BacktestEngine
from src.events import MarketEventBatch, OrderBookEvent, TradeEvent
from src.fill_model import FillModel, FillModelConfig
from src.fills import Fill
from src.market_state import MarketState
from src.order_manager import OrderManager, OrderManagerConfig
from src.orders import OrderSide
from src.performance import PerformanceTracker
from src.strategy import BaselineStrategy


def book(timestamp_ns: int) -> OrderBookEvent:
    """Build a queue-free synthetic order book."""
    return OrderBookEvent(
        timestamp_ns,
        (99.9,),
        (100.1,),
        (0.0,),
        (0.0,),
    )


def batch(
    timestamp_ns: int,
    trades: tuple[TradeEvent, ...] = (),
    books: tuple[OrderBookEvent, ...] = (),
) -> MarketEventBatch:
    """Build a public timestamp batch for tracker integration tests."""
    return MarketEventBatch(timestamp_ns, trades, books, ())


def tracked_engine(
    performance_tracker: PerformanceTracker,
) -> BacktestEngine:
    """Build a zero-latency baseline engine with tracking enabled."""
    return BacktestEngine(
        strategy=BaselineStrategy(
            StrategyConfig(
                tick_size=0.1,
                order_quantity=0.2,
                max_inventory=1.0,
                fixed_spread_ticks=1.0,
                inventory_skew_ticks=0.0,
            )
        ),
        order_manager=OrderManager(
            OrderManagerConfig(0, 0, 1.0)
        ),
        fill_model=FillModel(FillModelConfig()),
        performance_tracker=performance_tracker,
    )


class PerformanceTrackerTest(unittest.TestCase):
    """Verify online metrics, sampling, and event-loop integration."""

    def test_engine_records_fills_orders_and_core_metrics(self) -> None:
        """Summarize one ask fill from an end-to-end engine run."""
        performance_tracker = PerformanceTracker(
            sample_interval_ns=50,
            markout_horizons_ns=(),
        )
        backtest = tracked_engine(performance_tracker)

        backtest.run(
            (
                batch(100, books=(book(100),)),
                batch(
                    200,
                    trades=(TradeEvent(200, 100.1, 0.2, True),),
                ),
            )
        )
        metrics = performance_tracker.metrics(backtest.account)

        self.assertEqual(performance_tracker.submitted_order_count, 3)
        self.assertEqual(performance_tracker.activated_order_count, 3)
        self.assertEqual(metrics.fill_count, 1)
        self.assertAlmostEqual(metrics.fill_base_volume, 0.2)
        self.assertAlmostEqual(metrics.total_pnl, 0.02)
        self.assertAlmostEqual(metrics.quantity_fill_rate, 0.2 / 0.6)
        self.assertAlmostEqual(metrics.order_fill_rate, 1 / 3)
        self.assertAlmostEqual(metrics.average_captured_half_spread, 0.1)
        self.assertEqual(len(performance_tracker.snapshots), 2)

    def test_quantity_fill_rate_uses_all_submitted_quantity(self) -> None:
        """Divide filled quantity by submissions, not activated quantity."""
        performance_tracker = PerformanceTracker(
            sample_interval_ns=1,
            markout_horizons_ns=(),
        )
        performance_tracker.submitted_quantity = 1.0
        performance_tracker.record_fill(
            Fill(100, "buy", OrderSide.BUY, 100.0, 0.2),
            mid_price=100.0,
        )

        metrics = performance_tracker.metrics(Account())

        self.assertAlmostEqual(metrics.quantity_fill_rate, 0.2)

    def test_drawdown_and_daily_pnl_are_updated_online(self) -> None:
        """Track peak-to-trough loss and UTC daily equity changes."""
        performance_tracker = PerformanceTracker(
            sample_interval_ns=1,
            markout_horizons_ns=(),
        )
        account = Account()
        order_manager = OrderManager(OrderManagerConfig(0, 0, 1.0))
        market_state = MarketState()
        market_state.apply_order_book(book(100))
        account.mark_to_market(100.0)

        account.apply_funding_cashflow(5.0)
        performance_tracker.record_state(
            1_767_225_600_000_000_000,
            market_state,
            account,
            order_manager,
        )
        account.apply_funding_cashflow(-3.0)
        performance_tracker.record_state(
            1_767_225_601_000_000_000,
            market_state,
            account,
            order_manager,
        )
        account.apply_funding_cashflow(5.0)
        performance_tracker.record_state(
            1_767_312_000_000_000_000,
            market_state,
            account,
            order_manager,
        )

        metrics = performance_tracker.metrics(account)

        self.assertEqual(metrics.max_drawdown, 3.0)
        self.assertEqual(list(metrics.daily_pnl.values()), [2.0, 5.0])

    def test_markout_uses_first_book_at_or_after_horizon(self) -> None:
        """Calculate side-adjusted quantity-weighted post-fill markout."""
        performance_tracker = PerformanceTracker(
            sample_interval_ns=1,
            markout_horizons_ns=(100,),
        )
        account = Account()
        fill = Fill(100, "buy", OrderSide.BUY, 100.0, 0.2)
        performance_tracker.record_fill(fill, mid_price=100.05)

        performance_tracker.record_mark_price(199, 100.1)
        unresolved_metrics = performance_tracker.metrics(account)
        performance_tracker.record_mark_price(200, 100.2)
        resolved_metrics = performance_tracker.metrics(account)

        self.assertIsNone(unresolved_metrics.markout_by_horizon["100ns"])
        self.assertEqual(
            unresolved_metrics.markout_coverage_by_horizon["100ns"],
            0.0,
        )
        self.assertAlmostEqual(
            resolved_metrics.markout_by_horizon["100ns"],
            0.2,
        )
        self.assertEqual(
            resolved_metrics.markout_coverage_by_horizon["100ns"],
            1.0,
        )

    def test_markout_sign_is_favorable_for_buys_and_sells(self) -> None:
        """Use positive markout when future mid moves in the maker's favor."""
        buy_tracker = PerformanceTracker(
            sample_interval_ns=1,
            markout_horizons_ns=(100,),
        )
        sell_tracker = PerformanceTracker(
            sample_interval_ns=1,
            markout_horizons_ns=(100,),
        )
        account = Account()
        buy_tracker.record_fill(
            Fill(100, "buy", OrderSide.BUY, 100.0, 0.2),
            mid_price=100.0,
        )
        sell_tracker.record_fill(
            Fill(100, "sell", OrderSide.SELL, 100.0, 0.2),
            mid_price=100.0,
        )

        buy_tracker.record_mark_price(200, 100.2)
        sell_tracker.record_mark_price(200, 99.8)

        self.assertAlmostEqual(
            buy_tracker.metrics(account).markout_by_horizon["100ns"],
            0.2,
        )
        self.assertAlmostEqual(
            sell_tracker.metrics(account).markout_by_horizon["100ns"],
            0.2,
        )


if __name__ == "__main__":
    unittest.main()
