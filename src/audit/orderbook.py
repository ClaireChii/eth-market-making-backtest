"""Order-book integrity and market-structure audit."""
from pathlib import Path
from typing import Any

import polars as pl

from src.audit.common import common_audit, quantiles
from src.config import BOOK_LEVELS


def _book_columns() -> tuple[list[str], list[str], list[str]]:
    """Build the expected price and quantity column names for all book levels."""
    bid_prices = [f"bid_price_{i}" for i in range(1, BOOK_LEVELS + 1)]
    ask_prices = [f"ask_price_{i}" for i in range(1, BOOK_LEVELS + 1)]
    quantities = [
        f"{side}_qty_{i}"
        for side in ("bid", "ask")
        for i in range(1, BOOK_LEVELS + 1)
    ]
    return bid_prices, ask_prices, quantities


def _quality_summary(
    lazy_frame: pl.LazyFrame,
    bid_prices: list[str],
    ask_prices: list[str],
    quantities: list[str],
) -> dict[str, int]:
    """Count crossed books, malformed levels, and invalid displayed quantities."""
    bad_bid_order = pl.any_horizontal(
        pl.col(bid_prices[i]) <= pl.col(bid_prices[i + 1])
        for i in range(BOOK_LEVELS - 1)
    )
    bad_ask_order = pl.any_horizontal(
        pl.col(ask_prices[i]) >= pl.col(ask_prices[i + 1])
        for i in range(BOOK_LEVELS - 1)
    )
    summary = lazy_frame.select(
        (pl.col("ask_price_1") <= pl.col("bid_price_1"))
        .sum().alias("crossed_or_locked"),
        bad_bid_order.sum().alias("bad_bid_level_order"),
        bad_ask_order.sum().alias("bad_ask_level_order"),
        pl.any_horizontal(pl.col(name) <= 0 for name in quantities)
        .sum().alias("nonpositive_qty_rows"),
    ).collect()
    return {name: int(summary[name][0]) for name in summary.columns}


def _market_metrics_frame(lazy_frame: pl.LazyFrame) -> pl.LazyFrame:
    """Create mid, spread, and imbalance columns used by the audit report."""
    return lazy_frame.select(
        ((pl.col("bid_price_1") + pl.col("ask_price_1")) / 2).alias("mid"),
        (pl.col("ask_price_1") - pl.col("bid_price_1")).alias("spread"),
        (
            (pl.col("bid_qty_1") - pl.col("ask_qty_1"))
            / (pl.col("bid_qty_1") + pl.col("ask_qty_1"))
        ).alias("l1_imbalance"),
    )


def _tick_size_candidates(
    lazy_frame: pl.LazyFrame,
) -> list[dict[str, Any]]:
    """Return the five most frequent tick-size candidates."""
    bid_differences = [
        (pl.col(f"bid_price_{i}") - pl.col(f"bid_price_{i + 1}"))
        .round(10).alias(f"bid_tick_size_{i}")
        for i in range(1, BOOK_LEVELS)
    ]
    ask_differences = [
        (pl.col(f"ask_price_{i + 1}") - pl.col(f"ask_price_{i}"))
        .round(10).alias(f"ask_tick_size_{i}")
        for i in range(1, BOOK_LEVELS)
    ]
    candidates = lazy_frame.select(bid_differences + ask_differences).select(
        pl.concat_list(pl.all())
        .list.explode(empty_as_null=True)
        .alias("tick_size")
    )
    return (
        candidates.group_by("tick_size")
        .len()
        .with_columns(
            (pl.col("len") / pl.col("len").sum()).alias("share")
        )
        .sort("len", descending=True)
        .limit(5)
        .collect()
        .to_dicts()
    )


def audit_orderbook(path: Path) -> dict[str, Any]:
    """Audit one daily order-book file and return a structured result."""
    lazy_frame, result = common_audit(path)
    bid_prices, ask_prices, quantities = _book_columns()
    market_metrics = _market_metrics_frame(lazy_frame)

    result["quality"].update(
        _quality_summary(
            lazy_frame,
            bid_prices,
            ask_prices,
            quantities,
        )
    )
    result["market"] = {
        "mid": quantiles(market_metrics, "mid"),
        "spread": quantiles(market_metrics, "spread"),
        "l1_imbalance": quantiles(market_metrics, "l1_imbalance"),
        "top_tick_size_candidates": _tick_size_candidates(lazy_frame),
    }
    return result
