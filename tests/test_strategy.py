"""Tests for baseline fixed-spread and inventory-skew quoting."""

from dataclasses import replace
import unittest

from src.account import Account
from src.config import StrategyConfig
from src.events import FundingUpdateEvent, OrderBookEvent
from src.features import FeatureEngine, MarketFeatures
from src.fills import Fill
from src.market_state import MarketState
from src.orders import OrderSide
from src.strategy import (
    BaselineStrategy,
    DesiredQuote,
    FundingInventoryStrategy,
    ImbalanceStrategy,
    VolatilitySpreadStrategy,
)


def strategy() -> BaselineStrategy:
    """Build a baseline strategy with transparent synthetic parameters."""
    return BaselineStrategy(
        StrategyConfig(
            tick_size=0.1,
            order_quantity=0.2,
            max_inventory=1.0,
            fixed_spread_ticks=1.0,
            inventory_skew_ticks=2.0,
        )
    )


def market_state() -> MarketState:
    """Build a one-tick top-of-book market state centered at 100."""
    state = MarketState()
    state.apply_order_book(
        OrderBookEvent(
            timestamp_ns=100,
            bid_prices=(99.9,),
            ask_prices=(100.1,),
            bid_quantities=(2.0,),
            ask_quantities=(3.0,),
        )
    )
    return state


def account_with_inventory(inventory: float) -> Account:
    """Build an account with inventory opened at the synthetic mid price."""
    account = Account()
    if inventory == 0:
        return account
    side = OrderSide.BUY if inventory > 0 else OrderSide.SELL
    account.apply_fill(
        Fill(
            timestamp_ns=100,
            order_id="inventory-fill",
            side=side,
            price=100.0,
            quantity=abs(inventory),
        )
    )
    return account


class BaselineStrategyTest(unittest.TestCase):
    """Verify baseline quote direction, limits, passivity, and rounding."""

    def test_no_book_returns_empty_quote(self) -> None:
        """Avoid quoting before the first order-book snapshot."""
        quote = strategy().quote(MarketState(), Account())

        self.assertEqual(quote, DesiredQuote.empty())

    def test_zero_inventory_returns_symmetric_best_quotes(self) -> None:
        """Center the fixed-spread quote on mid when inventory is zero."""
        quote = strategy().quote(market_state(), Account())

        self.assertAlmostEqual(quote.bid_price, 99.9)
        self.assertAlmostEqual(quote.ask_price, 100.1)
        self.assertEqual(quote.bid_quantity, 0.2)
        self.assertEqual(quote.ask_quantity, 0.2)

    def test_long_inventory_moves_quote_down(self) -> None:
        """Lower reservation price to discourage additional long inventory."""
        quote = strategy().quote(
            market_state(),
            account_with_inventory(0.5),
        )

        self.assertAlmostEqual(quote.bid_price, 99.8)
        self.assertAlmostEqual(quote.ask_price, 100.1)

    def test_short_inventory_moves_quote_up(self) -> None:
        """Raise reservation price to encourage buying back short inventory."""
        quote = strategy().quote(
            market_state(),
            account_with_inventory(-0.5),
        )

        self.assertAlmostEqual(quote.bid_price, 99.9)
        self.assertAlmostEqual(quote.ask_price, 100.2)

    def test_inventory_limits_remove_risk_increasing_side(self) -> None:
        """Suppress bids at maximum long and asks at maximum short inventory."""
        long_quote = strategy().quote(
            market_state(),
            account_with_inventory(1.0),
        )
        short_quote = strategy().quote(
            market_state(),
            account_with_inventory(-1.0),
        )

        self.assertIsNone(long_quote.bid_price)
        self.assertEqual(long_quote.bid_quantity, 0.0)
        self.assertIsNotNone(long_quote.ask_price)
        self.assertIsNotNone(short_quote.bid_price)
        self.assertIsNone(short_quote.ask_price)
        self.assertEqual(short_quote.ask_quantity, 0.0)

    def test_remaining_capacity_reduces_order_quantity(self) -> None:
        """Resize a quote when current inventory is close to its limit."""
        quote = strategy().quote(
            market_state(),
            account_with_inventory(0.9),
        )

        self.assertAlmostEqual(quote.bid_quantity, 0.1)
        self.assertEqual(quote.ask_quantity, 0.2)

    def test_quotes_are_passive_and_tick_aligned(self) -> None:
        """Keep both quote prices on tick and no better than current best."""
        quote = strategy().quote(
            market_state(),
            account_with_inventory(0.37),
        )

        self.assertLessEqual(quote.bid_price, 99.9)
        self.assertGreaterEqual(quote.ask_price, 100.1)
        self.assertAlmostEqual(quote.bid_price / 0.1, round(quote.bid_price / 0.1))
        self.assertAlmostEqual(quote.ask_price / 0.1, round(quote.ask_price / 0.1))

    def test_invalid_config_is_rejected(self) -> None:
        """Reject non-positive risk and quoting parameters."""
        with self.assertRaises(ValueError):
            StrategyConfig(tick_size=0.0)
        with self.assertRaises(ValueError):
            StrategyConfig(inventory_skew_ticks=-1.0)


class ImbalanceStrategyTest(unittest.TestCase):
    """Verify that B1 changes only fair value using L1 microprice."""

    def test_bid_heavy_book_moves_ask_up_one_tick(self) -> None:
        """Reduce selling aggressiveness when displayed bid support dominates."""
        state = MarketState()
        state.apply_order_book(
            OrderBookEvent(
                timestamp_ns=100,
                bid_prices=(99.9,),
                ask_prices=(100.1,),
                bid_quantities=(9.0,),
                ask_quantities=(1.0,),
            )
        )
        config = strategy().config
        features = FeatureEngine().calculate(state)

        baseline_quote = BaselineStrategy(config).quote(state, Account())
        imbalance_quote = ImbalanceStrategy(config).quote(
            state,
            Account(),
            features,
        )

        self.assertAlmostEqual(baseline_quote.ask_price, 100.1)
        self.assertAlmostEqual(imbalance_quote.ask_price, 100.2)
        self.assertAlmostEqual(imbalance_quote.bid_price, 99.9)

    def test_features_are_required_when_book_exists(self) -> None:
        """Fail clearly if B1 is accidentally run without its feature engine."""
        with self.assertRaises(RuntimeError):
            ImbalanceStrategy(strategy().config).quote(
                market_state(),
                Account(),
            )


class VolatilitySpreadStrategyTest(unittest.TestCase):
    """Verify B2 spread expansion, warm-up, and observable-depth bounds."""

    @staticmethod
    def deep_market_state() -> MarketState:
        """Build four visible price levels around a midpoint of 100."""
        state = MarketState()
        state.apply_order_book(
            OrderBookEvent(
                timestamp_ns=100,
                bid_prices=(99.9, 99.8, 99.7, 99.6),
                ask_prices=(100.1, 100.2, 100.3, 100.4),
                bid_quantities=(1.0, 1.0, 1.0, 1.0),
                ask_quantities=(1.0, 1.0, 1.0, 1.0),
            )
        )
        return state

    def test_warmed_up_volatility_expands_each_quote_side(self) -> None:
        """Add one price-volatility unit to the fixed half-spread."""
        state = self.deep_market_state()
        features = MarketFeatures(0.0, 100.0, price_volatility=0.2)

        quote = VolatilitySpreadStrategy(
            replace(strategy().config, volatility_multiplier=1.0)
        ).quote(state, Account(), features)

        self.assertAlmostEqual(quote.bid_price, 99.7)
        self.assertAlmostEqual(quote.ask_price, 100.3)

    def test_multiplier_scales_the_volatility_adjustment(self) -> None:
        """Apply the configured multiplier to the price-volatility feature."""
        state = self.deep_market_state()
        features = MarketFeatures(0.0, 100.0, price_volatility=0.2)

        quote = VolatilitySpreadStrategy(
            replace(strategy().config, volatility_multiplier=0.5)
        ).quote(state, Account(), features)

        self.assertAlmostEqual(quote.bid_price, 99.8)
        self.assertAlmostEqual(quote.ask_price, 100.2)

    def test_invalid_multiplier_is_rejected(self) -> None:
        """Require a finite positive volatility multiplier."""
        with self.assertRaises(ValueError):
            StrategyConfig(volatility_multiplier=0.0)

    def test_warmup_matches_fixed_spread_baseline(self) -> None:
        """Use no volatility adjustment before the rolling window is full."""
        state = self.deep_market_state()
        features = MarketFeatures(0.0, 100.0, price_volatility=None)

        quote = VolatilitySpreadStrategy(strategy().config).quote(
            state,
            Account(),
            features,
        )

        self.assertAlmostEqual(quote.bid_price, 99.9)
        self.assertAlmostEqual(quote.ask_price, 100.1)

    def test_quotes_do_not_exceed_visible_depth(self) -> None:
        """Clamp a very wide quote to the deepest observable bid and ask."""
        state = self.deep_market_state()
        features = MarketFeatures(0.0, 100.0, price_volatility=2.0)

        quote = VolatilitySpreadStrategy(strategy().config).quote(
            state,
            Account(),
            features,
        )

        self.assertAlmostEqual(quote.bid_price, 99.6)
        self.assertAlmostEqual(quote.ask_price, 100.4)

    def test_features_are_required_when_book_exists(self) -> None:
        """Fail clearly if B2 is accidentally run without its feature engine."""
        with self.assertRaises(RuntimeError):
            VolatilitySpreadStrategy(strategy().config).quote(
                self.deep_market_state(),
                Account(),
            )


class FundingInventoryStrategyTest(unittest.TestCase):
    """Verify B3 funding direction, scale, bounds, and risk limits."""

    def setUp(self) -> None:
        """Build the B3 strategy and a synthetic market for each test."""
        self.strategy = FundingInventoryStrategy(strategy().config)
        self.state = market_state()

    def test_missing_funding_matches_zero_target_baseline(self) -> None:
        """Use the B0 target before the first funding observation."""
        baseline_quote = strategy().quote(self.state, Account())
        funding_quote = self.strategy.quote(self.state, Account())

        self.assertEqual(self.strategy.target_inventory(self.state), 0.0)
        self.assertEqual(funding_quote, baseline_quote)

    def test_positive_funding_targets_short_inventory(self) -> None:
        """Move quotes down when longs pay and short inventory is preferred."""
        self.state.apply_funding_update(FundingUpdateEvent(200, 0.0005))

        quote = self.strategy.quote(self.state, Account())

        self.assertAlmostEqual(
            self.strategy.target_inventory(self.state),
            -0.5,
        )
        self.assertAlmostEqual(quote.bid_price, 99.8)
        self.assertAlmostEqual(quote.ask_price, 100.1)

    def test_negative_funding_targets_long_inventory(self) -> None:
        """Move quotes up when shorts pay and long inventory is preferred."""
        self.state.apply_funding_update(FundingUpdateEvent(200, -0.0005))

        quote = self.strategy.quote(self.state, Account())

        self.assertAlmostEqual(
            self.strategy.target_inventory(self.state),
            0.5,
        )
        self.assertAlmostEqual(quote.bid_price, 99.9)
        self.assertAlmostEqual(quote.ask_price, 100.2)

    def test_target_scales_linearly_and_stops_at_half_limit(self) -> None:
        """Use the fixed 5 bps scale without exceeding half the risk limit."""
        self.state.apply_funding_update(FundingUpdateEvent(200, 0.0001))
        self.assertAlmostEqual(
            self.strategy.target_inventory(self.state),
            -0.1,
        )

        self.state.apply_funding_update(FundingUpdateEvent(300, -0.0020))
        self.assertAlmostEqual(
            self.strategy.target_inventory(self.state),
            0.5,
        )

    def test_funding_parameters_come_from_shared_config(self) -> None:
        """Read funding scale and target limit from StrategyConfig."""
        configured_strategy = FundingInventoryStrategy(
            replace(
                strategy().config,
                funding_rate_scale=0.001,
                funding_target_fraction=0.25,
            )
        )
        self.state.apply_funding_update(FundingUpdateEvent(200, 0.0005))

        self.assertAlmostEqual(
            configured_strategy.target_inventory(self.state),
            -0.125,
        )

    def test_absolute_inventory_limit_is_unchanged(self) -> None:
        """Suppress risk-increasing orders at the original hard limit."""
        self.state.apply_funding_update(FundingUpdateEvent(200, -0.0005))

        quote = self.strategy.quote(
            self.state,
            account_with_inventory(1.0),
        )

        self.assertIsNone(quote.bid_price)
        self.assertEqual(quote.bid_quantity, 0.0)


if __name__ == "__main__":
    unittest.main()
