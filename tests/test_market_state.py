"""Tests for current market-state updates and derived prices."""

import unittest

from src.events import FundingUpdateEvent, OrderBookEvent
from src.market_state import MarketState


def sample_book(timestamp_ns: int) -> OrderBookEvent:
    """Build a small valid order-book event for tests."""
    return OrderBookEvent(
        timestamp_ns=timestamp_ns,
        bid_prices=(99.0, 98.0),
        ask_prices=(101.0, 102.0),
        bid_quantities=(2.0, 3.0),
        ask_quantities=(4.0, 5.0),
    )


class MarketStateTest(unittest.TestCase):
    """Verify time, book, funding, and derived-price behavior."""

    def test_initial_state_has_no_market_prices(self) -> None:
        """Return None for prices before the first book snapshot."""
        state = MarketState()

        self.assertFalse(state.has_order_book)
        self.assertIsNone(state.best_bid)
        self.assertIsNone(state.best_ask)
        self.assertIsNone(state.mid_price)
        self.assertIsNone(state.spread)

    def test_order_book_updates_prices_and_time(self) -> None:
        """Replace book state and calculate top-of-book values."""
        state = MarketState()

        state.apply_order_book(sample_book(100))

        self.assertEqual(state.current_timestamp_ns, 100)
        self.assertEqual(state.last_book_update_ns, 100)
        self.assertEqual(state.best_bid, 99.0)
        self.assertEqual(state.best_ask, 101.0)
        self.assertEqual(state.mid_price, 100.0)
        self.assertEqual(state.spread, 2.0)

    def test_funding_update_does_not_change_book(self) -> None:
        """Update funding while retaining the latest order book."""
        state = MarketState()
        state.apply_order_book(sample_book(100))

        state.apply_funding_update(FundingUpdateEvent(200, 0.0001))

        self.assertEqual(state.current_timestamp_ns, 200)
        self.assertEqual(state.last_book_update_ns, 100)
        self.assertEqual(state.last_funding_update_ns, 200)
        self.assertEqual(state.funding_rate, 0.0001)
        self.assertEqual(state.mid_price, 100.0)

    def test_time_cannot_move_backwards(self) -> None:
        """Reject events older than the current simulation time."""
        state = MarketState()
        state.advance_to(200)

        with self.assertRaises(ValueError):
            state.advance_to(199)


if __name__ == "__main__":
    unittest.main()
