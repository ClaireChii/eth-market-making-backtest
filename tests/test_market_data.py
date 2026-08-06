"""Tests for event ordering and nanosecond-safe market-data loading."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import polars as pl

from src.events import FundingUpdateEvent, OrderBookEvent, TradeEvent
from src.market_data import (
    batch_market_events,
    iter_trade_events,
    merge_market_events,
)


class MarketDataTest(unittest.TestCase):
    """Verify the event-layer contract with small synthetic inputs."""

    def test_same_timestamp_order_and_source_stability(self) -> None:
        """Place trades before books and funding while preserving trade order."""
        timestamp_ns = 100
        trades = [
            TradeEvent(timestamp_ns, 10.0, 1.0, False),
            TradeEvent(timestamp_ns, 11.0, 2.0, True),
        ]
        books = [
            OrderBookEvent(
                timestamp_ns,
                (9.0,),
                (12.0,),
                (3.0,),
                (4.0,),
            )
        ]
        fundings = [FundingUpdateEvent(timestamp_ns, 0.0001)]

        merged = list(
            merge_market_events((trades, books, fundings))
        )

        self.assertEqual(merged, trades + books + fundings)

    def test_one_strategy_batch_per_timestamp(self) -> None:
        """Group all market events at one timestamp into a single batch."""
        timestamp_ns = 100
        events = [
            TradeEvent(timestamp_ns, 10.0, 1.0, False),
            OrderBookEvent(
                timestamp_ns,
                (9.0,),
                (11.0,),
                (2.0,),
                (2.0,),
            ),
            FundingUpdateEvent(timestamp_ns, 0.0001),
        ]

        batches = list(batch_market_events(events))

        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0].trades), 1)
        self.assertEqual(len(batches[0].order_books), 1)
        self.assertEqual(len(batches[0].funding_updates), 1)

    def test_parquet_reader_preserves_nanoseconds(self) -> None:
        """Cast timestamps to integers before Python row conversion."""
        timestamp_ns = 1_774_000_000_004_299_483
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trades.parquet"
            frame = pl.DataFrame(
                {
                    "datetime": pl.Series(
                        [timestamp_ns],
                        dtype=pl.Int64,
                    ).cast(pl.Datetime("ns")),
                    "price": [2200.0],
                    "size": [0.1],
                    "is_maker_ask": [1],
                }
            )
            frame.write_parquet(path)

            event = next(iter_trade_events(path))

        self.assertEqual(event.timestamp_ns, timestamp_ns)


if __name__ == "__main__":
    unittest.main()
