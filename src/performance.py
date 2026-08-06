"""Compact backtest recording and core performance metrics."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from src.account import Account
from src.fills import Fill
from src.market_state import MarketState
from src.order_manager import OrderManager
from src.orders import (
    OrderAction,
    OrderSide,
    PlaceOrderAction,
)


@dataclass(frozen=True, slots=True)
class RecordedFill:
    """Pair one fill with the causal mid price available at execution."""

    fill: Fill
    mid_price: float | None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Store a sampled account and active-quote state."""

    timestamp_ns: int
    mid_price: float | None
    cash: float
    inventory: float
    realized_pnl: float
    unrealized_pnl: float | None
    equity: float | None
    active_bid: float | None
    active_ask: float | None


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Contain strategy metrics; quantity fill rate uses submitted volume."""

    total_pnl: float | None
    gross_trading_pnl: float | None
    realized_pnl: float
    unrealized_pnl: float | None
    fee_cashflow: float
    funding_cashflow: float
    daily_pnl: dict[str, float]
    max_drawdown: float
    max_absolute_inventory: float
    mean_absolute_inventory: float
    fill_count: int
    fill_base_volume: float
    fill_notional: float
    quantity_fill_rate: float | None
    order_fill_rate: float | None
    average_quoted_spread: float | None
    average_captured_half_spread: float | None
    markout_by_horizon: dict[str, float | None]
    markout_coverage_by_horizon: dict[str, float | None]


class PerformanceTracker:
    """Record compact time series and update core metrics online."""

    def __init__(
        self,
        sample_interval_ns: int,
        markout_horizons_ns: tuple[int, ...],
    ) -> None:
        """Initialize empty logs and online metric accumulators."""

        # Time horizon.
        self.sample_interval_ns = sample_interval_ns
        self.markout_horizons_ns = tuple(sorted(markout_horizons_ns))

        # Recorded fills and snapshorts.
        self.fills: list[RecordedFill] = []
        self.snapshots: list[AccountSnapshot] = []

        # Lifecycle of orders.
        self.submitted_order_count = 0
        self.submitted_quantity = 0.0
        self.activated_order_count = 0
        self._orders_with_fills: set[str] = set()

        # Sampling and risk metrics.
        self._last_sample_timestamp_ns: int | None = None
        self._peak_equity = 0.0
        self._max_drawdown = 0.0
        self._max_absolute_inventory = 0.0
        self._daily_close_equity: dict[str, float] = {}

        # Pending and resolved execution markouts.
        self._pending_markouts = {
            horizon: deque[RecordedFill]()
            for horizon in self.markout_horizons_ns
        }

        self._quantity_weighted_markout_sums = {
            horizon: 0.0 for horizon in self.markout_horizons_ns
        }

        self._markout_resolved_quantities = {
            horizon: 0.0 for horizon in self.markout_horizons_ns
        }


    def record_order_submissions(
        self,
        actions: tuple[OrderAction, ...],
        order_manager: OrderManager,
    ) -> None:
        """Count submitted orders and quantity."""
        for action in actions:
            if isinstance(action, PlaceOrderAction):
                order = order_manager.orders[action.order_id]
                self.submitted_order_count += 1
                self.submitted_quantity += order.total_quantity

    def record_order_activations(
        self,
        actions: tuple[OrderAction, ...],
        order_manager: OrderManager,
        timestamp_ns: int,
    ) -> None:
        """Count placements that actually took effect."""
        for action in actions:
            if not isinstance(action, PlaceOrderAction):
                continue
            order = order_manager.orders[action.order_id]
            if order.activated_at_ns == timestamp_ns:
                self.activated_order_count += 1

    def record_fill(self, fill: Fill, mid_price: float | None) -> None:
        """Store one fill and identify orders receiving any execution."""
        recorded_fill = RecordedFill(fill, mid_price)
        self.fills.append(recorded_fill)
        self._orders_with_fills.add(fill.order_id)
        for pending in self._pending_markouts.values():
            pending.append(recorded_fill)

    def record_mark_price(self, timestamp_ns: int, mid_price: float) -> None:
        """Resolve due post-fill markouts at the next observed book midpoint."""
        if not math.isfinite(mid_price) or mid_price <= 0:
            raise ValueError("mid_price must be finite and positive")

        for horizon, pending in self._pending_markouts.items():
            while (
                pending
                and pending[0].fill.timestamp_ns + horizon <= timestamp_ns
            ):
                recorded_fill = pending.popleft()
                fill = recorded_fill.fill
                execution_markout = self._maker_price_edge(fill, mid_price)
                self._quantity_weighted_markout_sums[horizon] += (
                    execution_markout * fill.quantity
                )
                self._markout_resolved_quantities[horizon] += fill.quantity

    def record_state(
        self,
        timestamp_ns: int,
        market_state: MarketState,
        account: Account,
        order_manager: OrderManager,
        force_sample: bool = False,
    ) -> None:
        """Update online metrics and optionally append a sampled state."""
        equity = account.equity
        if equity is not None:
            self._peak_equity = max(self._peak_equity, equity)
            self._max_drawdown = max(
                self._max_drawdown,
                self._peak_equity - equity,
            )
            self._daily_close_equity[self._utc_day(timestamp_ns)] = equity
        self._max_absolute_inventory = max(
            self._max_absolute_inventory,
            abs(account.inventory),
        )

        if not self._should_sample(timestamp_ns, force_sample):
            return

        active_orders = order_manager.active_orders
        active_bids = [
            order.price
            for order in active_orders
            if order.side is OrderSide.BUY
        ]
        active_asks = [
            order.price
            for order in active_orders
            if order.side is OrderSide.SELL
        ]
        snapshot = AccountSnapshot(
            timestamp_ns=timestamp_ns,
            mid_price=market_state.mid_price,
            cash=account.cash,
            inventory=account.inventory,
            realized_pnl=account.realized_pnl,
            unrealized_pnl=account.unrealized_pnl,
            equity=equity,
            active_bid=max(active_bids) if active_bids else None,
            active_ask=min(active_asks) if active_asks else None,
        )
        if (
            self.snapshots
            and self.snapshots[-1].timestamp_ns == timestamp_ns
        ):
            self.snapshots[-1] = snapshot
        else:
            self.snapshots.append(snapshot)
        self._last_sample_timestamp_ns = timestamp_ns

    def metrics(self, account: Account) -> PerformanceMetrics:
        """Build the current core performance summary."""
        fill_base_volume = sum(item.fill.quantity for item in self.fills)
        fill_notional = sum(
            item.fill.price * item.fill.quantity
            for item in self.fills
        )
        captured_half_spread_value_sum = 0.0
        captured_half_spread_quantity = 0.0
        for item in self.fills:
            if item.mid_price is None:
                continue
            captured_half_spread = self._maker_price_edge(
                item.fill,
                item.mid_price,
            )
            captured_half_spread_value_sum += (
                captured_half_spread * item.fill.quantity
            )
            captured_half_spread_quantity += item.fill.quantity

        quoted_spreads = [
            snapshot.active_ask - snapshot.active_bid
            for snapshot in self.snapshots
            if snapshot.active_bid is not None
            and snapshot.active_ask is not None
        ]
        mean_absolute_inventory = (
            sum(abs(snapshot.inventory) for snapshot in self.snapshots)
            / len(self.snapshots)
            if self.snapshots
            else 0.0
        )

        return PerformanceMetrics(
            total_pnl=account.net_pnl,
            gross_trading_pnl=account.gross_trading_pnl,
            realized_pnl=account.realized_pnl,
            unrealized_pnl=account.unrealized_pnl,
            fee_cashflow=account.cumulative_fee_cashflow,
            funding_cashflow=account.cumulative_funding_cashflow,
            daily_pnl=self._daily_pnl(),
            max_drawdown=self._max_drawdown,
            max_absolute_inventory=self._max_absolute_inventory,
            mean_absolute_inventory=mean_absolute_inventory,
            fill_count=len(self.fills),
            fill_base_volume=fill_base_volume,
            fill_notional=fill_notional,
            quantity_fill_rate=(
                fill_base_volume / self.submitted_quantity
                if self.submitted_quantity > 0
                else None
            ),
            order_fill_rate=(
                len(self._orders_with_fills) / self.activated_order_count
                if self.activated_order_count > 0
                else None
            ),
            average_quoted_spread=(
                sum(quoted_spreads) / len(quoted_spreads)
                if quoted_spreads
                else None
            ),
            average_captured_half_spread=(
                captured_half_spread_value_sum
                / captured_half_spread_quantity
                if captured_half_spread_quantity > 0
                else None
            ),
            markout_by_horizon={
                self._horizon_label(horizon): (
                    self._quantity_weighted_markout_sums[horizon]
                    / self._markout_resolved_quantities[horizon]
                    if self._markout_resolved_quantities[horizon] > 0
                    else None
                )
                for horizon in self.markout_horizons_ns
            },
            markout_coverage_by_horizon={
                self._horizon_label(horizon): (
                    self._markout_resolved_quantities[horizon]
                    / fill_base_volume
                    if fill_base_volume > 0
                    else None
                )
                for horizon in self.markout_horizons_ns
            },
        )

    def _should_sample(self, timestamp_ns: int, force_sample: bool) -> bool:
        """Return whether the current state should enter the sampled series."""
        if force_sample or self._last_sample_timestamp_ns is None:
            return True
        return (
            timestamp_ns - self._last_sample_timestamp_ns
            >= self.sample_interval_ns
        )

    def _daily_pnl(self) -> dict[str, float]:
        """Convert UTC daily closing equity into daily PnL changes."""
        daily_pnl: dict[str, float] = {}
        previous_close = 0.0
        for day in sorted(self._daily_close_equity):
            close = self._daily_close_equity[day]
            daily_pnl[day] = close - previous_close
            previous_close = close
        return daily_pnl

    @staticmethod
    def _utc_day(timestamp_ns: int) -> str:
        """Return the assumed UTC calendar date for a nanosecond timestamp."""
        return datetime.fromtimestamp(
            timestamp_ns / 1_000_000_000,
            tz=timezone.utc,
        ).date().isoformat()

    @staticmethod
    def _horizon_label(horizon_ns: int) -> str:
        """Return a compact label for a configured markout horizon."""
        if horizon_ns % 1_000_000_000 == 0:
            return f"{horizon_ns // 1_000_000_000}s"
        if horizon_ns % 1_000_000 == 0:
            return f"{horizon_ns // 1_000_000}ms"
        return f"{horizon_ns}ns"

    @staticmethod
    def _maker_price_edge(fill: Fill, reference_mid: float) -> float:
        """Return maker-side edge versus a reference midpoint.

        Positive values are favorable: reference mid above a buy fill or
        below a sell fill. The execution-time reference gives captured
        half-spread; a future reference gives execution markout.
        """
        side_sign = 1.0 if fill.side is OrderSide.BUY else -1.0
        return side_sign * (reference_mid - fill.price)
