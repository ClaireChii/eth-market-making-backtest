"""Construct and run reproducible market-making backtests."""

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from src.config import BacktestConfig, DATA_ROOT, EXPECTED_DATES
from src.engine import BacktestEngine
from src.features import FeatureEngine, FeatureEngineConfig
from src.fill_model import FillModel, FillModelConfig
from src.market_data import iter_period_batches
from src.order_manager import OrderManager, OrderManagerConfig
from src.performance import PerformanceMetrics, PerformanceTracker
from src.strategy import (
    BaselineStrategy,
    FundingInventoryStrategy,
    ImbalanceStrategy,
    VolatilitySpreadStrategy,
)


STRATEGY_NAMES = {
    "B0": "mid-price baseline",
    "B1": "L1 microprice enhancement",
    "B2": "volatility-adaptive spread enhancement",
    "B3": "funding-aware inventory target enhancement",
}

@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Return one completed backtest and its measured duration."""

    strategy_id: str
    days: tuple[str, ...]
    config: BacktestConfig
    metrics: PerformanceMetrics
    elapsed_seconds: float
    order_count: int
    snapshot_count: int


def build_backtest_engine(
    strategy_id: str,
    config: BacktestConfig,
) -> tuple[BacktestEngine, PerformanceTracker]:
    """Construct one variant with shared risk and execution assumptions."""
    strategy_config = config.strategy
    simulation_config = config.simulation
    feature_config = FeatureEngineConfig(
        volatility_sample_interval_ns=(
            strategy_config.volatility_sample_interval_ns
        ),
        volatility_window=strategy_config.volatility_window,
    )
    if strategy_id == "B0":
        strategy = BaselineStrategy(strategy_config)
        feature_engine = None

    elif strategy_id == "B1":
        strategy = ImbalanceStrategy(strategy_config)
        feature_engine = FeatureEngine(feature_config)

    elif strategy_id == "B2":
        strategy = VolatilitySpreadStrategy(strategy_config)
        feature_engine = FeatureEngine(feature_config)

    elif strategy_id == "B3":
        strategy = FundingInventoryStrategy(strategy_config)
        feature_engine = None

    else:
        raise ValueError(f"unknown strategy: {strategy_id}")

    order_manager = OrderManager(
        OrderManagerConfig(
            placement_latency_ns=simulation_config.placement_latency_ns,
            cancellation_latency_ns=simulation_config.cancellation_latency_ns,
            max_inventory=strategy_config.max_inventory,
        )
    )

    performance_tracker = PerformanceTracker(
        sample_interval_ns=simulation_config.sample_interval_ns,
        markout_horizons_ns=simulation_config.markout_horizons_ns,
    )

    engine = BacktestEngine(
        strategy=strategy,
        order_manager=order_manager,
        fill_model=FillModel(
            FillModelConfig(maker_fee_bps=simulation_config.maker_fee_bps)
        ),
        performance_tracker=performance_tracker,
        feature_engine=feature_engine,
    )
    return engine, performance_tracker


def run_backtest(
    strategy_id: str = "B0",
    days: tuple[str, ...] = EXPECTED_DATES,
    config: BacktestConfig | None = None,
    data_root: Path = DATA_ROOT,
) -> BacktestResult:
    """Run one strategy over complete selected days and return its result."""
    if not days:
        raise ValueError("at least one day is required")

    selected_config = config or BacktestConfig()
    engine, performance_tracker = build_backtest_engine(
        strategy_id,
        selected_config,
    )
    started_at = perf_counter()
    engine.run(iter_period_batches(days, data_root=data_root))
    elapsed_seconds = perf_counter() - started_at

    return BacktestResult(
        strategy_id=strategy_id,
        days=days,
        config=selected_config,
        metrics=performance_tracker.metrics(engine.account),
        elapsed_seconds=elapsed_seconds,
        order_count=len(engine.order_manager.orders),
        snapshot_count=len(performance_tracker.snapshots),
    )
