"""Format backtest and sensitivity research results."""

from src.backtest import BacktestResult, STRATEGY_NAMES


def format_inventory_skew_sensitivity(
    results: tuple[BacktestResult, ...],
) -> str:
    """Format the B0 sensitivity cases as one compact comparison table."""
    lines = [
        "B0 inventory-skew sensitivity",
        "skew_ticks | total_pnl | max_drawdown | mean_abs_inventory | "
        "max_abs_inventory | fills | fill_volume",
    ]
    for result in results:
        metrics = result.metrics
        lines.append(
            f"{result.config.strategy.inventory_skew_ticks:10.1f} | "
            f"{_number(metrics.total_pnl):>9} | "
            f"{metrics.max_drawdown:12.6f} | "
            f"{metrics.mean_absolute_inventory:18.6f} | "
            f"{metrics.max_absolute_inventory:17.6f} | "
            f"{metrics.fill_count:5d} | "
            f"{metrics.fill_base_volume:11.6f}"
        )
    return "\n".join(lines)


def format_volatility_multiplier_sensitivity(
    results: tuple[BacktestResult, ...],
) -> str:
    """Format the B2 multiplier cases as one compact comparison table."""
    lines = [
        "B2 volatility-multiplier sensitivity",
        "multiplier | total_pnl | max_drawdown | mean_abs_inventory | "
        "fill_volume | fill_rate | avg_spread | 1s_markout",
    ]
    for result in results:
        metrics = result.metrics
        lines.append(
            f"{result.config.strategy.volatility_multiplier:10.1f} | "
            f"{_number(metrics.total_pnl):>9} | "
            f"{metrics.max_drawdown:12.6f} | "
            f"{metrics.mean_absolute_inventory:18.6f} | "
            f"{metrics.fill_base_volume:11.6f} | "
            f"{_percent(metrics.quantity_fill_rate):>9} | "
            f"{_number(metrics.average_quoted_spread):>10} | "
            f"{_number(metrics.markout_by_horizon['1s']):>10}"
        )
    return "\n".join(lines)


def format_result(result: BacktestResult) -> str:
    """Format one result as readable terminal text without serialization."""
    config = result.config
    strategy_config = config.strategy
    simulation_config = config.simulation
    metrics = result.metrics
    strategy_assumptions = [
        "  Fair price: "
        + ("L1 microprice" if result.strategy_id == "B1" else "mid price")
    ]
    if result.strategy_id == "B2":
        strategy_assumptions.extend(
            [
                "  Volatility: "
                f"{strategy_config.volatility_window} log returns sampled "
                "every "
                f"{strategy_config.volatility_sample_interval_ns / 1e9:g}s, RMS",
                "  Volatility multiplier: "
                f"{strategy_config.volatility_multiplier:.2f} per quote side",
                "  Volatility history required: "
                f"{strategy_config.volatility_window} returns",
                "  Quote depth: limited to visible order-book levels",
            ]
        )
    if result.strategy_id == "B3":
        strategy_assumptions.extend(
            [
                "  Funding use: latest observed rate moves inventory target",
                "  Funding direction: positive rate targets short inventory",
                "  Funding rate scale: "
                f"{strategy_config.funding_rate_scale:.4f}",
                "  Maximum funding target: "
                f"{100 * strategy_config.funding_target_fraction:.0f}% "
                "of inventory limit",
            ]
        )
    lines = [
        f"{result.strategy_id} {STRATEGY_NAMES[result.strategy_id]}",
        f"Period: {', '.join(result.days)}",
        "",
        "Assumptions",
        f"  Tick size: {strategy_config.tick_size:.4f}",
        f"  Order quantity: {strategy_config.order_quantity:.4f} ETH",
        f"  Maximum inventory: {strategy_config.max_inventory:.4f} ETH",
        "  Fixed quoted spread: "
        f"{strategy_config.fixed_spread_ticks:.2f} ticks",
        "  Inventory skew at limit: "
        f"{strategy_config.inventory_skew_ticks:.2f} ticks",
        "  Placement latency: "
        f"{simulation_config.placement_latency_ns / 1_000_000:.0f} ms",
        "  Cancellation latency: "
        f"{simulation_config.cancellation_latency_ns / 1_000_000:.0f} ms",
        f"  Maker fee: {simulation_config.maker_fee_bps:.4f} bps",
        "  Funding cashflow: 0 (updates are signals, not settlements)",
        *strategy_assumptions,
        "",
        "Results",
        f"  Total PnL: {_number(metrics.total_pnl)}",
        f"  Gross trading PnL: {_number(metrics.gross_trading_pnl)}",
        f"  Realized PnL: {metrics.realized_pnl:.6f}",
        f"  Unrealized PnL: {_number(metrics.unrealized_pnl)}",
        f"  Fee cashflow: {metrics.fee_cashflow:.6f}",
        f"  Funding cashflow: {metrics.funding_cashflow:.6f}",
        f"  Maximum drawdown: {metrics.max_drawdown:.6f}",
        f"  Maximum absolute inventory: {metrics.max_absolute_inventory:.6f} ETH",
        f"  Mean absolute inventory: {metrics.mean_absolute_inventory:.6f} ETH",
        f"  Fill count: {metrics.fill_count}",
        f"  Fill base volume: {metrics.fill_base_volume:.6f} ETH",
        f"  Fill notional: {metrics.fill_notional:.6f}",
        "  Fill rate (filled/submitted quantity): "
        f"{_percent(metrics.quantity_fill_rate)}",
        f"  Order fill rate: {_percent(metrics.order_fill_rate)}",
        f"  Average quoted spread: {_number(metrics.average_quoted_spread)}",
        "  Average captured half-spread: "
        f"{_number(metrics.average_captured_half_spread)}",
        "  Execution markout versus fill price:",
    ]
    lines.extend(
        f"    {horizon}: {_number(markout)} "
        f"(coverage {_percent(metrics.markout_coverage_by_horizon[horizon])})"
        for horizon, markout in metrics.markout_by_horizon.items()
    )
    lines.extend(["", "Daily PnL"])
    lines.extend(
        f"  {day}: {pnl:.6f}"
        for day, pnl in metrics.daily_pnl.items()
    )
    lines.extend(
        [
            "",
            "Run diagnostics",
            f"  Orders created: {result.order_count}",
            f"  Recorded snapshots: {result.snapshot_count}",
            f"  Elapsed time: {result.elapsed_seconds:.2f} seconds",
        ]
    )
    return "\n".join(lines)


def _number(value: float | None) -> str:
    """Format an optional floating-point metric."""
    return "N/A" if value is None else f"{value:.6f}"


def _percent(value: float | None) -> str:
    """Format an optional ratio as a percentage."""
    return "N/A" if value is None else f"{100 * value:.2f}%"
