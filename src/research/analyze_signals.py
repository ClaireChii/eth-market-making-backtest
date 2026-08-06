"""Analyze B1 and B2 market signals on a causal one-second grid."""

from argparse import ArgumentParser, Namespace
from pathlib import Path

import polars as pl

from src.config import DATA_ROOT, EXPECTED_DATES
from src.features import FeatureEngineConfig
from src.market_data import daily_file


HORIZONS_SECONDS = (1, 5, 30)
IMBALANCE_BUCKETS = (
    "strong sell",
    "weak sell",
    "neutral",
    "weak buy",
    "strong buy",
)
VOLATILITY_BUCKETS = (
    "lowest 20%",
    "20%-40%",
    "40%-60%",
    "60%-80%",
    "highest 20%",
)


def build_signal_samples(
    days: tuple[str, ...],
    data_root: Path = DATA_ROOT,
    feature_config: FeatureEngineConfig | None = None,
) -> pl.DataFrame:
    """Build causal fixed-grid observations and future-return labels."""
    if not days:
        raise ValueError("at least one day is required")

    config = feature_config or FeatureEngineConfig()
    daily_samples = [
        _build_daily_samples(day, data_root, config)
        for day in days
    ]
    return pl.concat(daily_samples, how="vertical")


def _build_daily_samples(
    day: str,
    data_root: Path,
    config: FeatureEngineConfig,
) -> pl.DataFrame:
    """Sample the latest causally available L1 book within one day."""
    books = (
        pl.scan_parquet(daily_file("orderbook", day, data_root))
        .select(
            pl.col("datetime").cast(pl.Int64).alias("timestamp_ns"),
            pl.col("bid_price_1").cast(pl.Float64).alias("best_bid"),
            pl.col("ask_price_1").cast(pl.Float64).alias("best_ask"),
            pl.col("bid_qty_1").cast(pl.Float64).alias("bid_quantity"),
            pl.col("ask_qty_1").cast(pl.Float64).alias("ask_quantity"),
        )
        .collect()
        .set_sorted("timestamp_ns")
    )
    if books.is_empty():
        raise ValueError(f"order-book file is empty: {day}")

    interval = config.volatility_sample_interval_ns
    first_timestamp = int(books["timestamp_ns"][0])
    last_timestamp = int(books["timestamp_ns"][-1])
    first_grid_timestamp = (
        (first_timestamp + interval - 1) // interval
    ) * interval
    last_grid_timestamp = (last_timestamp // interval) * interval

    grid = pl.DataFrame(
        {
            "timestamp_ns": pl.int_range(
                first_grid_timestamp,
                last_grid_timestamp + interval,
                step=interval,
                eager=True,
            )
        }
    ).set_sorted("timestamp_ns")
    sampled = grid.join_asof(
        books,
        on="timestamp_ns",
        strategy="backward",
    )
    return _add_signal_columns(sampled, day, config)


def _add_signal_columns(
    sampled_books: pl.DataFrame,
    day: str,
    config: FeatureEngineConfig,
) -> pl.DataFrame:
    """Calculate current signals before attaching future-only labels."""
    total_quantity = pl.col("bid_quantity") + pl.col("ask_quantity")
    frame = sampled_books.with_columns(
        pl.lit(day).alias("day"),
        ((pl.col("best_bid") + pl.col("best_ask")) / 2).alias(
            "mid_price"
        ),
        pl.when(total_quantity > 0)
        .then(
            (pl.col("bid_quantity") - pl.col("ask_quantity"))
            / total_quantity
        )
        .otherwise(0.0)
        .alias("l1_imbalance"),
    ).with_columns(
        pl.col("mid_price").log().diff().alias("one_second_log_return")
    )

    rolling_mean_squared_return = (
        pl.col("one_second_log_return")
        .pow(2)
        .rolling_mean(
            window_size=config.volatility_window,
            min_samples=config.volatility_window,
        )
    )
    frame = frame.with_columns(
        (
            pl.col("mid_price")
            * rolling_mean_squared_return.sqrt()
        ).alias("price_volatility")
    )

    future_expressions: list[pl.Expr] = []
    for horizon in HORIZONS_SECONDS:
        future_return = (
            pl.col("mid_price").shift(-horizon)
            / pl.col("mid_price")
        ).log() * 10_000
        future_expressions.extend(
            [
                future_return.alias(f"future_return_{horizon}s_bps"),
                future_return.abs().alias(
                    f"absolute_future_return_{horizon}s_bps"
                ),
            ]
        )
    return frame.with_columns(future_expressions)


def summarize_imbalance(
    samples: pl.DataFrame,
    horizon_seconds: int,
) -> pl.DataFrame:
    """Summarize signed future returns across fixed imbalance buckets."""
    _validate_horizon(horizon_seconds)
    return_column = f"future_return_{horizon_seconds}s_bps"
    bucket_order = (
        pl.when(pl.col("l1_imbalance") < -0.6).then(pl.lit(0))
        .when(pl.col("l1_imbalance") < -0.2).then(pl.lit(1))
        .when(pl.col("l1_imbalance") <= 0.2).then(pl.lit(2))
        .when(pl.col("l1_imbalance") <= 0.6).then(pl.lit(3))
        .otherwise(pl.lit(4))
    )
    return (
        samples.with_columns(bucket_order.alias("bucket_order"))
        .filter(
            pl.col("l1_imbalance").is_finite()
            & pl.col(return_column).is_finite()
        )
        .group_by("bucket_order")
        .agg(
            pl.len().alias("count"),
            pl.col(return_column).mean().alias("mean_return_bps"),
            pl.col(return_column).median().alias("median_return_bps"),
            (pl.col(return_column) > 0).mean().alias("up_rate"),
        )
        .sort("bucket_order")
        .with_columns(
            pl.col("bucket_order")
            .replace_strict(
                list(range(len(IMBALANCE_BUCKETS))),
                list(IMBALANCE_BUCKETS),
            )
            .alias("bucket")
        )
        .select(
            "bucket",
            "count",
            "mean_return_bps",
            "median_return_bps",
            "up_rate",
        )
    )


def summarize_volatility(
    samples: pl.DataFrame,
    horizon_seconds: int,
) -> tuple[pl.DataFrame, tuple[float, float, float, float]]:
    """Summarize absolute future returns across volatility quintiles."""
    _validate_horizon(horizon_seconds)
    return_column = f"absolute_future_return_{horizon_seconds}s_bps"
    valid = samples.filter(
        pl.col("price_volatility").is_finite()
        & pl.col(return_column).is_finite()
    )
    if valid.is_empty():
        raise ValueError("no warmed-up volatility observations are available")

    cutoffs = tuple(
        float(valid["price_volatility"].quantile(quantile, "linear"))
        for quantile in (0.2, 0.4, 0.6, 0.8)
    )
    bucket_order = (
        pl.when(pl.col("price_volatility") <= cutoffs[0]).then(pl.lit(0))
        .when(pl.col("price_volatility") <= cutoffs[1]).then(pl.lit(1))
        .when(pl.col("price_volatility") <= cutoffs[2]).then(pl.lit(2))
        .when(pl.col("price_volatility") <= cutoffs[3]).then(pl.lit(3))
        .otherwise(pl.lit(4))
    )
    summary = (
        valid.with_columns(bucket_order.alias("bucket_order"))
        .group_by("bucket_order")
        .agg(
            pl.len().alias("count"),
            pl.col("price_volatility").mean().alias(
                "mean_price_volatility"
            ),
            pl.col(return_column).mean().alias(
                "mean_absolute_return_bps"
            ),
            pl.col(return_column).median().alias(
                "median_absolute_return_bps"
            ),
        )
        .sort("bucket_order")
        .with_columns(
            pl.col("bucket_order")
            .replace_strict(
                list(range(len(VOLATILITY_BUCKETS))),
                list(VOLATILITY_BUCKETS),
            )
            .alias("bucket")
        )
        .select(
            "bucket",
            "count",
            "mean_price_volatility",
            "mean_absolute_return_bps",
            "median_absolute_return_bps",
        )
    )
    return summary, cutoffs


def format_analysis(samples: pl.DataFrame, days: tuple[str, ...]) -> str:
    """Format B1 and B2 signal diagnostics as readable terminal tables."""
    lines = [
        "Causal signal diagnostics",
        f"Period: {', '.join(days)}",
        f"One-second observations: {samples.height:,}",
        "Future prices are analysis labels and never enter strategy decisions.",
    ]
    for horizon in HORIZONS_SECONDS:
        lines.extend(
            [
                "",
                f"B1 imbalance buckets - {horizon}s future return",
                _format_imbalance_table(
                    summarize_imbalance(samples, horizon)
                ),
            ]
        )

    lines.extend(
        [
            "",
            "B2 volatility buckets",
            "Quintile boundaries are descriptive and are not strategy inputs.",
        ]
    )
    for horizon in HORIZONS_SECONDS:
        summary, cutoffs = summarize_volatility(samples, horizon)
        lines.extend(
            [
                "",
                f"{horizon}s absolute future return",
                "Price-volatility cutoffs: "
                + ", ".join(f"{cutoff:.6f}" for cutoff in cutoffs),
                _format_volatility_table(summary),
            ]
        )
    return "\n".join(lines)


def _format_imbalance_table(summary: pl.DataFrame) -> str:
    """Render one compact fixed-width imbalance table."""
    rows = [
        "Bucket          Count      Mean bps   Median bps    Up rate"
    ]
    for row in summary.iter_rows(named=True):
        rows.append(
            f"{row['bucket']:<13} "
            f"{row['count']:>8,d} "
            f"{row['mean_return_bps']:>13.6f} "
            f"{row['median_return_bps']:>12.6f} "
            f"{100 * row['up_rate']:>9.2f}%"
        )
    return "\n".join(rows)


def _format_volatility_table(summary: pl.DataFrame) -> str:
    """Render one compact fixed-width volatility table."""
    rows = [
        "Bucket          Count    Mean vol   Mean abs bps  Median abs bps"
    ]
    for row in summary.iter_rows(named=True):
        rows.append(
            f"{row['bucket']:<13} "
            f"{row['count']:>8,d} "
            f"{row['mean_price_volatility']:>11.6f} "
            f"{row['mean_absolute_return_bps']:>14.6f} "
            f"{row['median_absolute_return_bps']:>15.6f}"
        )
    return "\n".join(rows)


def _validate_horizon(horizon_seconds: int) -> None:
    """Reject horizons whose future labels were not constructed."""
    if horizon_seconds not in HORIZONS_SECONDS:
        raise ValueError(f"unsupported horizon: {horizon_seconds}")


def _parse_args() -> Namespace:
    """Parse complete UTC days for the descriptive signal analysis."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        nargs="+",
        choices=EXPECTED_DATES,
        default=list(EXPECTED_DATES),
        help="complete UTC days to analyze; defaults to all three days",
    )
    return parser.parse_args()


def main() -> None:
    """Build causal observations and print B1/B2 bucket diagnostics."""
    arguments = _parse_args()
    days = tuple(arguments.days)
    samples = build_signal_samples(days)
    print(format_analysis(samples, days))


if __name__ == "__main__":
    main()
