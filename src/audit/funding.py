"""Funding-observation quality and update-frequency audit."""

from pathlib import Path
from typing import Any

import polars as pl

from src.audit.common import common_audit, quantiles, scalar


def _funding_summary(lazy_frame: pl.LazyFrame) -> pl.DataFrame:
    """Count changes in the observed funding rate."""
    return lazy_frame.select(
        (pl.col("funding_rate").diff() != 0).sum().alias("rate_changes"),
    ).collect()


def audit_fundings(path: Path) -> dict[str, Any]:
    """Audit one daily funding file and return a structured result."""
    lazy_frame, result = common_audit(path)
    summary = _funding_summary(lazy_frame)
    result["market"] = {
        "funding_rate": quantiles(lazy_frame, "funding_rate"),
        "rate_changes": int(scalar(summary, "rate_changes")),
    }
    return result
