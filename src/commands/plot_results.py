"""Command-line entry point for generating the core report figures."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from src.backtest import build_backtest_engine
from src.config import BacktestConfig, EXPECTED_DATES, PROJECT_ROOT
from src.market_data import iter_period_batches
from src.performance import AccountSnapshot, PerformanceMetrics


STRATEGY_IDS = ("B0", "B1", "B2", "B3")
STRATEGY_COLORS = {
    "B0": "#4C566A",
    "B1": "#5E81AC",
    "B2": "#3F8F8C",
    "B3": "#A66F9B",
}
FIGURE_DIRECTORY = PROJECT_ROOT / "docs" / "figures"
SNAPSHOT_STRIDE = 10


@dataclass(frozen=True, slots=True)
class StrategyPlotData:
    """Store downsampled paths and final metrics for one strategy."""

    timestamps: tuple[datetime, ...]
    equity: tuple[float, ...]
    inventory: tuple[float, ...]
    metrics: PerformanceMetrics


def generate_figures(
    output_directory: Path = FIGURE_DIRECTORY,
) -> tuple[Path, Path, Path]:
    """Run all variants and save cumulative, inventory, and comparison plots."""
    plot_data = _run_strategies()
    output_directory.mkdir(parents=True, exist_ok=True)

    cumulative_pnl_path = output_directory / "cumulative-pnl.png"
    inventory_path = output_directory / "inventory-path.png"
    comparison_path = output_directory / "strategy-comparison.png"

    _plot_cumulative_pnl(plot_data, cumulative_pnl_path)
    _plot_inventory(plot_data, inventory_path)
    _plot_comparison(plot_data, comparison_path)
    return cumulative_pnl_path, inventory_path, comparison_path


def _run_strategies() -> dict[str, StrategyPlotData]:
    """Run each strategy independently under the shared evaluation contract."""
    results: dict[str, StrategyPlotData] = {}
    config = BacktestConfig()
    for strategy_id in STRATEGY_IDS:
        print(f"Running {strategy_id} for report figures...")
        engine, performance_tracker = build_backtest_engine(
            strategy_id,
            config,
        )
        engine.run(iter_period_batches(EXPECTED_DATES))
        results[strategy_id] = _extract_plot_data(
            performance_tracker.snapshots,
            performance_tracker.metrics(engine.account),
        )
    return results


def _extract_plot_data(
    snapshots: list[AccountSnapshot],
    metrics: PerformanceMetrics,
) -> StrategyPlotData:
    """Downsample dense snapshots while retaining the terminal observation."""
    selected = snapshots[::SNAPSHOT_STRIDE]
    if snapshots and (not selected or selected[-1] is not snapshots[-1]):
        selected = [*selected, snapshots[-1]]
    valid = [snapshot for snapshot in selected if snapshot.equity is not None]
    if not valid:
        raise RuntimeError("plotting requires at least one marked snapshot")
    return StrategyPlotData(
        timestamps=tuple(
            datetime.fromtimestamp(
                snapshot.timestamp_ns / 1_000_000_000,
                tz=timezone.utc,
            )
            for snapshot in valid
        ),
        equity=tuple(float(snapshot.equity) for snapshot in valid),
        inventory=tuple(snapshot.inventory for snapshot in valid),
        metrics=metrics,
    )


def _plot_cumulative_pnl(
    plot_data: dict[str, StrategyPlotData],
    output_path: Path,
) -> None:
    """Plot marked equity, which equals cumulative PnL from zero capital."""
    figure, axis = plt.subplots(figsize=(11, 4.8))
    for strategy_id in STRATEGY_IDS:
        data = plot_data[strategy_id]
        axis.plot(
            data.timestamps,
            data.equity,
            label=strategy_id,
            color=STRATEGY_COLORS[strategy_id],
            linewidth=1.25,
        )
    axis.axhline(0.0, color="#9CA3AF", linewidth=0.8)
    axis.set_title("Cumulative PnL by Strategy")
    axis.set_ylabel("PnL (quote currency)")
    _format_time_axis(axis)
    axis.legend(ncols=4, frameon=False, loc="lower left")
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def _plot_inventory(
    plot_data: dict[str, StrategyPlotData],
    output_path: Path,
) -> None:
    """Plot inventory paths against the shared absolute risk limit."""
    figure, axis = plt.subplots(figsize=(11, 4.8))
    for strategy_id in STRATEGY_IDS:
        data = plot_data[strategy_id]
        axis.plot(
            data.timestamps,
            data.inventory,
            label=strategy_id,
            color=STRATEGY_COLORS[strategy_id],
            linewidth=1.0,
            alpha=0.9,
        )
    axis.axhline(0.0, color="#9CA3AF", linewidth=0.8)
    axis.axhline(1.0, color="#C08497", linewidth=0.8, linestyle="--")
    axis.axhline(-1.0, color="#C08497", linewidth=0.8, linestyle="--")
    axis.set_ylim(-1.08, 1.08)
    axis.set_title("Inventory Path by Strategy")
    axis.set_ylabel("Inventory (ETH)")
    _format_time_axis(axis)
    axis.legend(ncols=4, frameon=False, loc="lower left")
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def _plot_comparison(
    plot_data: dict[str, StrategyPlotData],
    output_path: Path,
) -> None:
    """Compare final PnL, drawdown, and fill volume on separate scales."""
    metrics = [plot_data[strategy_id].metrics for strategy_id in STRATEGY_IDS]
    total_pnl = [metric.total_pnl for metric in metrics]
    if any(value is None for value in total_pnl):
        raise RuntimeError("plotting requires marked total PnL")
    values = (
        [float(value) for value in total_pnl if value is not None],
        [metric.max_drawdown for metric in metrics],
        [metric.fill_base_volume for metric in metrics],
    )
    titles = ("Total PnL", "Maximum Drawdown", "Fill Volume")
    y_labels = ("Quote currency", "Quote currency", "ETH")
    colors = [STRATEGY_COLORS[strategy_id] for strategy_id in STRATEGY_IDS]

    figure, axes = plt.subplots(1, 3, figsize=(12, 4.5))
    for axis, panel_values, title, y_label in zip(
        axes,
        values,
        titles,
        y_labels,
        strict=True,
    ):
        bars = axis.bar(STRATEGY_IDS, panel_values, color=colors, width=0.68)
        axis.bar_label(bars, fmt="%.1f", padding=3, fontsize=8)
        axis.set_title(title)
        axis.set_ylabel(y_label)
        axis.grid(axis="y", alpha=0.2)
        axis.margins(y=0.15)
    figure.suptitle("Strategy Outcome Comparison")
    figure.tight_layout()
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def _format_time_axis(axis: plt.Axes) -> None:
    """Use compact UTC date labels for a three-day time series."""
    axis.set_xlabel("UTC date")
    axis.xaxis.set_major_locator(mdates.DayLocator(tz=timezone.utc))
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d", tz=timezone.utc))


def main() -> None:
    """Generate all final-report figures from fresh strategy runs."""
    paths = generate_figures()
    for path in paths:
        print(f"Saved {path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
