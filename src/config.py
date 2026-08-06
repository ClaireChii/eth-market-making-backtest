"""Shared configuration for data, strategies, and simulation."""

from dataclasses import dataclass, field
import math
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "raw_data"

DATASETS = ("orderbook", "trades", "fundings")
EXPECTED_DATES = ("2026-03-19", "2026-03-20", "2026-03-21")
BOOK_LEVELS = 20
AUDIT_QUANTILES = (0.0, 0.01, 0.5, 0.99, 1.0)


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Configure parameters shared by all quoting strategies."""

    # Market, order size, and risk.
    tick_size: float = 0.1
    order_quantity: float = 0.1
    max_inventory: float = 1.0

    # Baseline quote construction.
    fixed_spread_ticks: float = 1.0
    inventory_skew_ticks: float = 8.0

    # Volatility-spread enhancement.
    volatility_multiplier: float = 5.0
    volatility_sample_interval_ns: int = 1_000_000_000
    volatility_window: int = 60

    # Funding-aware inventory target.
    funding_rate_scale: float = 0.0005
    funding_target_fraction: float = 0.5

    def __post_init__(self) -> None:
        """Validate all strategy and feature parameters centrally."""
        positive_values = {
            "tick_size": self.tick_size,
            "order_quantity": self.order_quantity,
            "max_inventory": self.max_inventory,
            "fixed_spread_ticks": self.fixed_spread_ticks,
            "volatility_multiplier": self.volatility_multiplier,
            "funding_rate_scale": self.funding_rate_scale,
        }
        for name, value in positive_values.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

        if (
            not math.isfinite(self.inventory_skew_ticks)
            or self.inventory_skew_ticks < 0
        ):
            raise ValueError(
                "inventory_skew_ticks must be finite and non-negative"
            )

        if self.volatility_sample_interval_ns <= 0:
            raise ValueError("volatility_sample_interval_ns must be positive")

        if self.volatility_window <= 0:
            raise ValueError("volatility_window must be positive")

        if (
            not math.isfinite(self.funding_target_fraction)
            or not 0 <= self.funding_target_fraction <= 1
        ):
            raise ValueError(
                "funding_target_fraction must be finite and between 0 and 1"
            )


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Configure latency, fees, sampling, and execution measurement."""

    placement_latency_ns: int = 100_000_000
    cancellation_latency_ns: int = 100_000_000
    maker_fee_bps: float = 0.0
    sample_interval_ns: int = 1_000_000_000
    markout_horizons_ns: tuple[int, ...] = (
        1_000_000_000,
        5_000_000_000,
        30_000_000_000,
    )

    def __post_init__(self) -> None:
        """Validate simulator parameters centrally."""
        if self.placement_latency_ns < 0:
            raise ValueError("placement_latency_ns must be non-negative")
        if self.cancellation_latency_ns < 0:
            raise ValueError("cancellation_latency_ns must be non-negative")
        if not math.isfinite(self.maker_fee_bps):
            raise ValueError("maker_fee_bps must be finite")
        if self.sample_interval_ns <= 0:
            raise ValueError("sample_interval_ns must be positive")
        if (
            any(horizon <= 0 for horizon in self.markout_horizons_ns)
            or len(set(self.markout_horizons_ns)) != len(self.markout_horizons_ns)
        ):
            raise ValueError("markout horizons must be positive and unique")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Combine strategy and simulation configuration for one backtest."""

    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
