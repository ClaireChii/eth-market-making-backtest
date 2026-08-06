"""Tests for causal signal sampling and descriptive bucket summaries."""

from pathlib import Path
from tempfile import TemporaryDirectory
import math
import unittest

import polars as pl

from src.research.analyze_signals import (
    build_signal_samples,
    summarize_imbalance,
    summarize_volatility,
)
from src.features import FeatureEngineConfig


class SignalAnalysisTest(unittest.TestCase):
    """Verify causal sampling, future labels, and ordered buckets."""

    def test_grid_uses_latest_book_and_exact_boundary_update(self) -> None:
        """Carry old books forward but use a new book exactly on the grid."""
        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory)
            book_directory = data_root / "orderbook"
            book_directory.mkdir()
            pl.DataFrame(
                {
                    "datetime": [0, 500_000_000, 1_000_000_000, 2_000_000_000],
                    "bid_price_1": [99.0, 109.0, 89.0, 94.0],
                    "ask_price_1": [101.0, 111.0, 91.0, 96.0],
                    "bid_qty_1": [1.0, 3.0, 1.0, 1.0],
                    "ask_qty_1": [1.0, 1.0, 3.0, 1.0],
                }
            ).write_parquet(book_directory / "2026-03-19.parquet")

            samples = build_signal_samples(
                ("2026-03-19",),
                data_root=data_root,
                feature_config=FeatureEngineConfig(volatility_window=1),
            )

        self.assertEqual(samples["mid_price"].to_list(), [100.0, 90.0, 95.0])
        self.assertEqual(samples["l1_imbalance"].to_list(), [0.0, -0.5, 0.0])
        self.assertAlmostEqual(
            samples["future_return_1s_bps"][0],
            10_000 * math.log(90.0 / 100.0),
        )
        self.assertIsNone(samples["future_return_1s_bps"][-1])
        self.assertIsNone(samples["price_volatility"][0])
        self.assertIsNotNone(samples["price_volatility"][1])

    def test_imbalance_summary_preserves_fixed_bucket_order(self) -> None:
        """Report the five economic imbalance states from sell to buy."""
        samples = pl.DataFrame(
            {
                "l1_imbalance": [-0.8, -0.4, 0.0, 0.4, 0.8, math.nan],
                "future_return_1s_bps": [
                    -2.0,
                    -1.0,
                    0.0,
                    1.0,
                    2.0,
                    math.nan,
                ],
            }
        )

        summary = summarize_imbalance(samples, 1)

        self.assertEqual(
            summary["bucket"].to_list(),
            ["strong sell", "weak sell", "neutral", "weak buy", "strong buy"],
        )
        self.assertEqual(summary["mean_return_bps"].to_list(), [-2, -1, 0, 1, 2])

    def test_volatility_summary_orders_descriptive_quintiles(self) -> None:
        """Order volatility buckets from quiet to volatile conditions."""
        samples = pl.DataFrame(
            {
                "price_volatility": [
                    1.0,
                    2.0,
                    3.0,
                    4.0,
                    5.0,
                    math.nan,
                ],
                "absolute_future_return_1s_bps": [
                    1.0,
                    2.0,
                    3.0,
                    4.0,
                    5.0,
                    math.nan,
                ],
            }
        )

        summary, cutoffs = summarize_volatility(samples, 1)

        self.assertEqual(len(cutoffs), 4)
        self.assertEqual(
            summary["bucket"].to_list(),
            ["lowest 20%", "20%-40%", "40%-60%", "60%-80%", "highest 20%"],
        )
        self.assertEqual(summary["count"].sum(), 5)


if __name__ == "__main__":
    unittest.main()
