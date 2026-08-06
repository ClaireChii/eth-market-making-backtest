"""Read and merge market data without losing nanosecond timestamps."""

from collections.abc import Iterable, Iterator
from heapq import merge
from itertools import groupby
from pathlib import Path

import polars as pl

from src.config import BOOK_LEVELS, DATASETS, DATA_ROOT, EXPECTED_DATES
from src.events import (
    FundingUpdateEvent,
    MarketEvent,
    MarketEventBatch,
    OrderBookEvent,
    TradeEvent,
)


def daily_file(
    dataset: str,
    day: str,
    data_root: Path = DATA_ROOT,
) -> Path:
    """Return one expected daily file and fail clearly when it is unavailable."""
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    if day not in EXPECTED_DATES:
        raise ValueError(f"Unsupported date: {day}")

    path = data_root / dataset / f"{day}.parquet"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _collect_rows(
    path: Path,
    expressions: list[pl.Expr],
    row_limit: int | None,
) -> Iterator[tuple[object, ...]]:
    """Collect selected columns and yield rows in their original source order."""
    lazy_frame = pl.scan_parquet(path).select(expressions)
    if row_limit is not None:
        if row_limit < 0:
            raise ValueError("row_limit must be non-negative")
        lazy_frame = lazy_frame.head(row_limit)
    return lazy_frame.collect().iter_rows()


def iter_trade_events(
    path: Path,
    row_limit: int | None = None,
) -> Iterator[TradeEvent]:
    """Yield trades while preserving the source order of equal timestamps."""
    rows = _collect_rows(
        path,
        [
            pl.col("datetime").cast(pl.Int64).alias("timestamp_ns"),
            pl.col("price"),
            pl.col("size"),
            pl.col("is_maker_ask"),
        ],
        row_limit,
    )
    for timestamp_ns, price, size, is_maker_ask in rows:
        yield TradeEvent(
            timestamp_ns=int(timestamp_ns),
            price=float(price),
            size=float(size),
            is_maker_ask=bool(is_maker_ask),
        )


def iter_order_book_events(
    path: Path,
    row_limit: int | None = None,
) -> Iterator[OrderBookEvent]:
    """Yield full-depth book snapshots with nanosecond timestamps."""
    bid_price_columns = [
        f"bid_price_{level}" for level in range(1, BOOK_LEVELS + 1)
    ]
    ask_price_columns = [
        f"ask_price_{level}" for level in range(1, BOOK_LEVELS + 1)
    ]
    bid_quantity_columns = [
        f"bid_qty_{level}" for level in range(1, BOOK_LEVELS + 1)
    ]
    ask_quantity_columns = [
        f"ask_qty_{level}" for level in range(1, BOOK_LEVELS + 1)
    ]
    market_columns = (
        bid_price_columns
        + ask_price_columns
        + bid_quantity_columns
        + ask_quantity_columns
    )
    rows = _collect_rows(
        path,
        [
            pl.col("datetime").cast(pl.Int64).alias("timestamp_ns"),
            *(pl.col(name) for name in market_columns),
        ],
        row_limit,
    )

    depth = BOOK_LEVELS
    for row in rows:
        yield OrderBookEvent(
            timestamp_ns=int(row[0]),
            bid_prices=tuple(row[1 : 1 + depth]),
            ask_prices=tuple(row[1 + depth : 1 + 2 * depth]),
            bid_quantities=tuple(row[1 + 2 * depth : 1 + 3 * depth]),
            ask_quantities=tuple(row[1 + 3 * depth : 1 + 4 * depth]),
        )


def iter_funding_events(
    path: Path,
    row_limit: int | None = None,
) -> Iterator[FundingUpdateEvent]:
    """Yield funding signal updates without treating them as settlements."""
    rows = _collect_rows(
        path,
        [
            pl.col("datetime").cast(pl.Int64).alias("timestamp_ns"),
            pl.col("funding_rate"),
        ],
        row_limit,
    )
    for timestamp_ns, funding_rate in rows:
        yield FundingUpdateEvent(
            timestamp_ns=int(timestamp_ns),
            funding_rate=float(funding_rate),
        )


def merge_market_events(
    event_streams: Iterable[Iterable[MarketEvent]],
) -> Iterator[MarketEvent]:
    """Stably merge sorted event streams by timestamp and processing stage."""
    return merge(
        *event_streams,
        key=lambda event: (event.timestamp_ns, event.stage),
    )


def batch_market_events(
    events: Iterable[MarketEvent],
) -> Iterator[MarketEventBatch]:
    """Group a sorted event stream so strategy logic runs once per timestamp."""
    for timestamp_ns, timestamp_events in groupby(
        events,
        key=lambda event: event.timestamp_ns,
    ):
        trades: list[TradeEvent] = []
        order_books: list[OrderBookEvent] = []
        funding_updates: list[FundingUpdateEvent] = []

        for event in timestamp_events:
            if isinstance(event, TradeEvent):
                trades.append(event)
            elif isinstance(event, OrderBookEvent):
                order_books.append(event)
            elif isinstance(event, FundingUpdateEvent):
                funding_updates.append(event)
            else:
                raise TypeError(f"Unsupported market event: {type(event)!r}")

        yield MarketEventBatch(
            timestamp_ns=timestamp_ns,
            trades=tuple(trades),
            order_books=tuple(order_books),
            funding_updates=tuple(funding_updates),
        )


def iter_day_batches(
    day: str,
    data_root: Path = DATA_ROOT,
    row_limit_per_stream: int | None = None,
) -> Iterator[MarketEventBatch]:
    """Yield one day's public market data as timestamp batches."""
    streams = (
        iter_trade_events(
            daily_file("trades", day, data_root),
            row_limit_per_stream,
        ),
        iter_order_book_events(
            daily_file("orderbook", day, data_root),
            row_limit_per_stream,
        ),
        iter_funding_events(
            daily_file("fundings", day, data_root),
            row_limit_per_stream,
        ),
    )
    return batch_market_events(merge_market_events(streams))


def iter_period_batches(
    days: Iterable[str],
    data_root: Path = DATA_ROOT,
    row_limit_per_stream: int | None = None,
) -> Iterator[MarketEventBatch]:
    """Yield multiple complete days as one chronological market stream."""
    previous_day: str | None = None
    for day in days:
        if previous_day is not None and day <= previous_day:
            raise ValueError("days must be strictly increasing")
        yield from iter_day_batches(
            day,
            data_root=data_root,
            row_limit_per_stream=row_limit_per_stream,
        )
        previous_day = day
