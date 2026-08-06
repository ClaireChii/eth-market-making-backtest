"""Trade validity, direction, and volume audit."""
from pathlib import Path
from typing import Any

import polars as pl

from src.audit.common import common_audit, quantiles, scalar


def _trade_summary(lazy_frame: pl.LazyFrame) -> pl.DataFrame:
    """Aggregate trade-quality counts and daily volume statistics."""
    return lazy_frame.select(
        (pl.col("size") <= 0).sum().alias("nonpositive_size"),
        (pl.col("price") <= 0).sum().alias("nonpositive_price"),
        (~pl.col("is_maker_ask").is_in([0, 1])).sum().alias("invalid_side"),
        (pl.col("is_maker_ask") == 1).sum().alias("buyer_aggressor_count"),
        (pl.col("is_maker_ask") == 0).sum().alias("seller_aggressor_count"),
        pl.col("size").sum().alias("total_base_volume"),
        (pl.col("size") * pl.col("price")).sum().alias("total_notional"),
        pl.struct(pl.all()).n_unique().alias("unique_rows"),
    ).collect()


def audit_trades(path: Path) -> dict[str, Any]:
    """Audit one daily trade file and return a structured result."""
    lazy_frame, result = common_audit(path)
    summary = _trade_summary(lazy_frame)
    buyer_count = int(scalar(summary, "buyer_aggressor_count"))
    seller_count = int(scalar(summary, "seller_aggressor_count"))
    aggressor_count = buyer_count + seller_count

    result["quality"].update(
        {
            "nonpositive_size": int(scalar(summary, "nonpositive_size")),
            "nonpositive_price": int(scalar(summary, "nonpositive_price")),
            "invalid_side": int(scalar(summary, "invalid_side")),
            "exact_duplicate_rows": (
                result["rows"] - int(scalar(summary, "unique_rows"))
            ),
        }
    )
    result["market"] = {
        "price": quantiles(lazy_frame, "price"),
        "size": quantiles(lazy_frame, "size"),
        "buyer_aggressor_count": buyer_count,
        "seller_aggressor_count": seller_count,
        "buyer_aggressor_share": (
            buyer_count / aggressor_count if aggressor_count else None
        ),
        "seller_aggressor_share": (
            seller_count / aggressor_count if aggressor_count else None
        ),
        "total_base_volume": float(scalar(summary, "total_base_volume")),
        "total_notional": float(scalar(summary, "total_notional")),
    }
    return result
