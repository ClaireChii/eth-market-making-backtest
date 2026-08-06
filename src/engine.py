"""Minimal event loop connecting market data, orders, fills, and account."""

from collections.abc import Iterable
from typing import Protocol

from src.account import Account
from src.features import FeatureEngine, MarketFeatures
from src.fill_model import FillModel
from src.fills import Fill
from src.market_state import MarketState
from src.events import MarketEventBatch
from src.order_manager import OrderManager
from src.performance import PerformanceTracker
from src.strategy import DesiredQuote


class QuotingStrategy(Protocol):
    """Describe the strategy interface consumed by the event loop."""

    def quote(
        self,
        market_state: MarketState,
        account: Account,
        features: MarketFeatures | None = None,
    ) -> DesiredQuote:
        """Return the quote desired after one public market timestamp."""


class BacktestEngine:
    """Process public market batches and private order actions causally."""

    def __init__(
        self,
        strategy: QuotingStrategy,
        order_manager: OrderManager,
        fill_model: FillModel,
        market_state: MarketState | None = None,
        account: Account | None = None,
        performance_tracker: PerformanceTracker | None = None,
        feature_engine: FeatureEngine | None = None,
    ) -> None:
        """Store the collaborating stateful backtest components."""
        self.strategy = strategy
        self.order_manager = order_manager
        self.fill_model = fill_model
        self.market_state = market_state or MarketState()
        self.account = account or Account()
        self.performance_tracker = performance_tracker
        self.feature_engine = feature_engine
        self.fills: list[Fill] = []

    def run(
        self,
        market_batches: Iterable[MarketEventBatch],
    ) -> tuple[Fill, ...]:
        """Run until the final public market batch and return generated fills."""
        batch_iterator = iter(market_batches)
        next_batch = next(batch_iterator, None)

        while next_batch is not None:
            action_timestamp = self.order_manager.next_action_timestamp_ns

            if (
                action_timestamp is not None
                and action_timestamp < next_batch.timestamp_ns
            ):
                self._process_action_timestamp(action_timestamp)
                continue

            self._process_market_timestamp(next_batch)
            next_batch = next(batch_iterator, None)

        if (
            self.performance_tracker is not None
            and self.market_state.current_timestamp_ns is not None
        ):
            self.performance_tracker.record_state(
                self.market_state.current_timestamp_ns,
                self.market_state,
                self.account,
                self.order_manager,
                force_sample=True,
            )

        return tuple(self.fills)

    def _process_action_timestamp(self, timestamp_ns: int) -> None:
        """Process private actions occurring between public market events."""
        self.market_state.advance_to(timestamp_ns)
        actions = self.order_manager.process_actions_at(
            timestamp_ns,
            self.market_state,
            self.account,
        )
        if self.performance_tracker is not None:
            self.performance_tracker.record_order_activations(
                actions,
                self.order_manager,
                timestamp_ns,
            )
            self.performance_tracker.record_state(
                timestamp_ns,
                self.market_state,
                self.account,
                self.order_manager,
            )

    def _process_market_timestamp(self, batch: MarketEventBatch) -> None:
        """Apply one public timestamp in the agreed causal order."""
        timestamp_ns = batch.timestamp_ns
        self.market_state.advance_to(timestamp_ns)

        # 1. Public trades can fill only orders already active at this time.
        for trade in batch.trades:
            fills = self.fill_model.process_trade(
                trade,
                self.order_manager.active_orders,
            )
            for fill in fills:
                self.account.apply_fill(fill)
                self.fills.append(fill)
                if self.performance_tracker is not None:
                    self.performance_tracker.record_fill(
                        fill,
                        self.market_state.mid_price,
                    )

        # 2. Cancellations and then placements become effective.
        if self.order_manager.next_action_timestamp_ns == timestamp_ns:
            actions = self.order_manager.process_actions_at(
                timestamp_ns,
                self.market_state,
                self.account,
            )
            if self.performance_tracker is not None:
                self.performance_tracker.record_order_activations(
                    actions,
                    self.order_manager,
                    timestamp_ns,
                )

        # 3. Public state updates replace the current book and funding signal.
        for order_book in batch.order_books:
            self.market_state.apply_order_book(order_book)
        for funding_update in batch.funding_updates:
            self.market_state.apply_funding_update(funding_update)

        if (
            self.performance_tracker is not None
            and batch.order_books
            and self.market_state.mid_price is not None
        ):
            self.performance_tracker.record_mark_price(
                timestamp_ns,
                self.market_state.mid_price,
            )

        # 4. Mark the account only when a valid book is available.
        if self.market_state.mid_price is not None:
            self.account.mark_to_market(self.market_state.mid_price)

        # 5. Make one strategy decision after the timestamp batch is complete.
        features = (
            self.feature_engine.calculate(self.market_state)
            if self.feature_engine is not None
            else None
        )
        desired_quote = self.strategy.quote(
            self.market_state,
            self.account,
            features,
        )
        scheduled_actions = self.order_manager.reconcile(
            desired_quote,
            timestamp_ns,
        )
        if self.performance_tracker is not None:
            self.performance_tracker.record_order_submissions(
                scheduled_actions,
                self.order_manager,
            )

        # Zero-latency orders become active now but cannot fill past trades.
        if self.order_manager.next_action_timestamp_ns == timestamp_ns:
            actions = self.order_manager.process_actions_at(
                timestamp_ns,
                self.market_state,
                self.account,
            )
            if self.performance_tracker is not None:
                self.performance_tracker.record_order_activations(
                    actions,
                    self.order_manager,
                    timestamp_ns,
                )

        if self.performance_tracker is not None:
            self.performance_tracker.record_state(
                timestamp_ns,
                self.market_state,
                self.account,
                self.order_manager,
            )
