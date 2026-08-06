"""Tests for causal top-of-book feature calculations."""

import math
import unittest

from src.events import OrderBookEvent
from src.features import FeatureEngine, FeatureEngineConfig
from src.market_state import MarketState


def state_with_quantities(
    bid_quantity: float,
    ask_quantity: float,
) -> MarketState:
    """Build a one-tick book with configurable L1 quantities."""
    state = MarketState()
    state.apply_order_book(
        OrderBookEvent(
            timestamp_ns=100,
            bid_prices=(99.9,),
            ask_prices=(100.1,),
            bid_quantities=(bid_quantity,),
            ask_quantities=(ask_quantity,),
        )
    )
    return state


def apply_mid(state: MarketState, timestamp_ns: int, mid_price: float) -> None:
    """Apply a one-tick book centered on a requested midpoint."""
    state.apply_order_book(
        OrderBookEvent(
            timestamp_ns=timestamp_ns,
            bid_prices=(mid_price - 0.1,),
            ask_prices=(mid_price + 0.1,),
            bid_quantities=(1.0,),
            ask_quantities=(1.0,),
        )
    )


class FeatureEngineTest(unittest.TestCase):
    """Verify L1 imbalance, microprice, and empty-book behavior."""

    def test_balanced_book_returns_mid_price(self) -> None:
        """Map equal displayed quantities to zero imbalance and midpoint."""
        features = FeatureEngine().calculate(state_with_quantities(2.0, 2.0))

        self.assertEqual(features.l1_imbalance, 0.0)
        self.assertEqual(features.microprice, 100.0)

    def test_bid_heavy_book_moves_microprice_toward_ask(self) -> None:
        """Weight microprice toward the side likely to be depleted next."""
        features = FeatureEngine().calculate(state_with_quantities(3.0, 1.0))

        self.assertAlmostEqual(features.l1_imbalance, 0.5)
        self.assertAlmostEqual(features.microprice, 100.05)

    def test_empty_book_has_no_features(self) -> None:
        """Avoid inventing features before the first order-book snapshot."""
        self.assertIsNone(FeatureEngine().calculate(MarketState()))

    def test_zero_total_quantity_falls_back_to_mid(self) -> None:
        """Use a neutral feature when both displayed L1 quantities are zero."""
        features = FeatureEngine().calculate(state_with_quantities(0.0, 0.0))

        self.assertEqual(features.l1_imbalance, 0.0)
        self.assertEqual(features.microprice, 100.0)

    def test_volatility_uses_fixed_grid_locf_and_full_warmup(self) -> None:
        """Carry mid forward on the grid and wait for the complete window."""
        feature_engine = FeatureEngine(
            FeatureEngineConfig(
                volatility_sample_interval_ns=100,
                volatility_window=2,
            )
        )
        state = MarketState()
        apply_mid(state, 0, 100.0)
        first = feature_engine.calculate(state)
        apply_mid(state, 50, 110.0)
        before_boundary = feature_engine.calculate(state)
        state.advance_to(100)
        one_return = feature_engine.calculate(state)
        state.advance_to(200)
        warmed_up = feature_engine.calculate(state)

        expected = 110.0 * (
            (math.log(110.0 / 100.0) ** 2) / 2
        ) ** 0.5
        self.assertIsNone(first.price_volatility)
        self.assertIsNone(before_boundary.price_volatility)
        self.assertIsNone(one_return.price_volatility)
        self.assertAlmostEqual(warmed_up.price_volatility, expected)

    def test_book_on_grid_boundary_uses_current_mid(self) -> None:
        """Use a new causal book when it arrives exactly on a sample time."""
        feature_engine = FeatureEngine(
            FeatureEngineConfig(
                volatility_sample_interval_ns=100,
                volatility_window=1,
            )
        )
        state = MarketState()
        apply_mid(state, 0, 100.0)
        feature_engine.calculate(state)
        apply_mid(state, 50, 110.0)
        feature_engine.calculate(state)
        apply_mid(state, 100, 90.0)

        features = feature_engine.calculate(state)

        expected = 90.0 * abs(math.log(90.0 / 100.0))
        self.assertAlmostEqual(features.price_volatility, expected)

    def test_invalid_volatility_config_is_rejected(self) -> None:
        """Require positive sampling interval and rolling window."""
        with self.assertRaises(ValueError):
            FeatureEngineConfig(volatility_sample_interval_ns=0)
        with self.assertRaises(ValueError):
            FeatureEngineConfig(volatility_window=0)


if __name__ == "__main__":
    unittest.main()
