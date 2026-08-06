"""Define the declared inventory and volatility parameter sweeps."""

from dataclasses import replace
from pathlib import Path

from src.backtest import BacktestResult, run_backtest
from src.config import BacktestConfig, DATA_ROOT, EXPECTED_DATES


INITIAL_INVENTORY_SKEW_TICKS = 2.0
INITIAL_VOLATILITY_MULTIPLIER = 1.0
INVENTORY_SKEW_SENSITIVITY_TICKS = (
    1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
)
VOLATILITY_MULTIPLIER_SENSITIVITY = (
    0.5, 1.0, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0,
)


def run_inventory_skew_sensitivity(
    days: tuple[str, ...] = EXPECTED_DATES,
    data_root: Path = DATA_ROOT,
) -> tuple[BacktestResult, ...]:
    """Run the exploratory B0 inventory-skew sensitivity cases."""
    base_config = BacktestConfig()
    return tuple(
        run_backtest(
            strategy_id="B0",
            days=days,
            config=replace(
                base_config,
                strategy=replace(
                    base_config.strategy,
                    inventory_skew_ticks=skew_ticks,
                ),
            ),
            data_root=data_root,
        )
        for skew_ticks in INVENTORY_SKEW_SENSITIVITY_TICKS
    )


def run_volatility_multiplier_sensitivity(
    days: tuple[str, ...] = EXPECTED_DATES,
    data_root: Path = DATA_ROOT,
) -> tuple[BacktestResult, ...]:
    """Run the exploratory B2 volatility-multiplier sensitivity cases."""
    base_config = BacktestConfig()
    return tuple(
        run_backtest(
            strategy_id="B2",
            days=days,
            config=replace(
                base_config,
                strategy=replace(
                    base_config.strategy,
                    volatility_multiplier=multiplier,
                ),
            ),
            data_root=data_root,
        )
        for multiplier in VOLATILITY_MULTIPLIER_SENSITIVITY
    )
