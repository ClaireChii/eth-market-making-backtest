"""Shared aggregation helpers for data audits."""
from pathlib import Path
from typing import Any

import polars as pl

from src.config import AUDIT_QUANTILES


def scalar(frame: pl.DataFrame, column: str) -> Any:
    """Return a scalar from a one-row aggregation frame."""
    if frame.height != 1:
        raise ValueError(
            f"Expected one aggregation row, got {frame.height}"
        )
    return frame[column][0]


def quantiles(
    lazy_frame: pl.LazyFrame,
    column: str,
) -> dict[str, float | None]:
    """Calculate the configured quantiles for one numeric column."""
    expressions = [
        pl.col(column)
        .quantile(q, interpolation="linear")
        .alias(str(q))
        for q in AUDIT_QUANTILES
    ]
    row = lazy_frame.select(expressions).collect().row(0, named=True)
    return dict(row)


def common_audit(path: Path) -> tuple[pl.LazyFrame, dict[str, Any]]:
    """Collect checks shared by books, trades, and funding observations."""
    lazy_frame = pl.scan_parquet(path)
    schema = lazy_frame.collect_schema()

    # 1. Validate the timestamp column.
    if "datetime" not in schema:
        raise ValueError(f"Missing required 'datetime' column: {path}")

    datetime_dtype = schema["datetime"]
    if not datetime_dtype.is_temporal():
        raise TypeError(
            f"'datetime' must be temporal, got {datetime_dtype}: {path}"
        )

    valid_datetimes = lazy_frame.filter(
        pl.col("datetime").is_not_null()
    )

    # 2. Aggregate row counts, null counts, and timestamp checks.
    summary_expressions: list[pl.Expr] = [
        pl.len().alias("audit_row_count"),
        pl.col("datetime").min().alias("audit_start_time"),
        pl.col("datetime").max().alias("audit_end_time"),
        pl.col("datetime")
        .drop_nulls()
        .n_unique()
        .alias("audit_unique_timestamp_count"),
        (
            pl.col("datetime").drop_nulls().diff()
            < pl.duration(nanoseconds=0)
        )
        .sum()
        .alias("audit_out_of_order_count"),
    ]
    summary_expressions.extend(
        pl.col(name).null_count().alias(f"audit_null_{name}")
        for name in schema.names()
    )
    summary = lazy_frame.select(summary_expressions).collect()

    # 3. Calculate the timestamp-gap distribution.
    gap_frame = valid_datetimes.select(
        (
            pl.col("datetime").diff()
            .dt.total_nanoseconds()
            / 1_000_000_000
        ).alias("gap_seconds")
    ).filter(pl.col("gap_seconds").is_not_null())
    gap_quantiles = quantiles(gap_frame, "gap_seconds")

    rows = int(scalar(summary, "audit_row_count") or 0)
    datetime_nulls = int(
        scalar(summary, "audit_null_datetime") or 0
    )
    unique_timestamps = int(
        scalar(summary, "audit_unique_timestamp_count") or 0
    )
    start = scalar(summary, "audit_start_time")
    end = scalar(summary, "audit_end_time")
    filename_date = path.stem

    non_datetime_nulls = sum(
        int(scalar(summary, f"audit_null_{name}") or 0)
        for name in schema.names()
        if name != "datetime"
    )
    filename_date_mismatch = int(
        start is None
        or end is None
        or start.date().isoformat() != filename_date
        or end.date().isoformat() != filename_date
    )

    # 4. Build the shared quality result.
    result = {
        "rows": rows,
        "duplicate_timestamp_rows": max(
            rows - datetime_nulls - unique_timestamps,
            0,
        ),
        "gap_seconds": gap_quantiles,
        "quality": {
            "datetime_nulls": datetime_nulls,
            "non_datetime_nulls": non_datetime_nulls,
            "out_of_order_rows": int(
                scalar(summary, "audit_out_of_order_count") or 0
            ),
            "filename_date_mismatch": filename_date_mismatch,
        },
    }
    return lazy_frame, result
