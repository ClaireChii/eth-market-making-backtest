"""Tests for the reproducible baseline backtest runner."""

from dataclasses import replace
import unittest

from src.backtest import (
    BacktestResult,
    build_backtest_engine,
)
from src.config import BacktestConfig, SimulationConfig
from src.research.reporting import (
    format_inventory_skew_sensitivity,
    format_result,
    format_volatility_multiplier_sensitivity,
)
from src.strategy import (
    FundingInventoryStrategy,
    ImbalanceStrategy,
    VolatilitySpreadStrategy,
)


def config_with_strategy(**changes: float) -> BacktestConfig:
    """Return a backtest config with selected strategy-field changes."""
    config = BacktestConfig()
    return replace(config, strategy=replace(config.strategy, **changes))


class BacktestRunnerTest(unittest.TestCase):
    """Verify component wiring and terminal result formatting."""

    def test_all_strategies_receive_the_same_shared_config(self) -> None:
        """Pass one StrategyConfig object unchanged to every B0-B3 class."""
        config = BacktestConfig()

        for strategy_id in ("B0", "B1", "B2", "B3"):
            engine, _ = build_backtest_engine(strategy_id, config)
            self.assertIs(engine.strategy.config, config.strategy)

    def test_simulation_config_is_forwarded_to_components(self) -> None:
        """Build simulator components from one public simulation config."""
        simulation = SimulationConfig(
            placement_latency_ns=7,
            cancellation_latency_ns=9,
            maker_fee_bps=1.25,
            sample_interval_ns=11,
            markout_horizons_ns=(13,),
        )

        engine, tracker = build_backtest_engine(
            "B0",
            BacktestConfig(simulation=simulation),
        )

        self.assertEqual(engine.order_manager.config.placement_latency_ns, 7)
        self.assertEqual(engine.order_manager.config.cancellation_latency_ns, 9)
        self.assertEqual(engine.fill_model.config.maker_fee_bps, 1.25)
        self.assertEqual(tracker.sample_interval_ns, 11)
        self.assertEqual(tracker.markout_horizons_ns, (13,))

    def test_builder_uses_one_shared_inventory_limit(self) -> None:
        """Keep strategy and order-manager risk limits consistent."""
        config = config_with_strategy(max_inventory=2.5)

        engine, performance_tracker = build_backtest_engine("B0", config)

        self.assertEqual(engine.strategy.config.max_inventory, 2.5)
        self.assertEqual(engine.order_manager.config.max_inventory, 2.5)
        self.assertIs(engine.performance_tracker, performance_tracker)

    def test_selected_inventory_skew_is_the_shared_default(self) -> None:
        """Use the selected eight-tick skew in final strategy comparisons."""
        config = BacktestConfig()

        engine, _ = build_backtest_engine("B0", config)

        self.assertEqual(config.strategy.inventory_skew_ticks, 8.0)
        self.assertEqual(engine.strategy.config.inventory_skew_ticks, 8.0)

    def test_b1_builder_adds_features_without_changing_risk(self) -> None:
        """Wire microprice features while retaining the shared inventory limit."""
        config = config_with_strategy(max_inventory=2.5)

        engine, _ = build_backtest_engine("B1", config)

        self.assertIsInstance(engine.strategy, ImbalanceStrategy)
        self.assertIsNotNone(engine.feature_engine)
        self.assertEqual(engine.strategy.config.max_inventory, 2.5)
        self.assertEqual(engine.order_manager.config.max_inventory, 2.5)

    def test_b2_builder_adds_volatility_without_changing_risk(self) -> None:
        """Wire adaptive spread while retaining the shared inventory limit."""
        config = config_with_strategy(
            max_inventory=2.5,
            volatility_multiplier=2.0,
        )

        engine, _ = build_backtest_engine("B2", config)

        self.assertIsInstance(
            engine.strategy,
            VolatilitySpreadStrategy,
        )
        self.assertIsNotNone(engine.feature_engine)
        self.assertEqual(engine.strategy.config.max_inventory, 2.5)
        self.assertEqual(engine.strategy.config.volatility_multiplier, 2.0)
        self.assertEqual(engine.order_manager.config.max_inventory, 2.5)

    def test_selected_volatility_multiplier_is_the_b2_default(self) -> None:
        """Use the selected five-times multiplier in final B2 comparisons."""
        config = BacktestConfig()

        engine, _ = build_backtest_engine("B2", config)

        self.assertEqual(config.strategy.volatility_multiplier, 5.0)
        self.assertEqual(
            engine.strategy.config.volatility_multiplier,
            5.0,
        )

    def test_b3_builder_uses_funding_without_changing_risk(self) -> None:
        """Wire the funding target without adding high-frequency features."""
        config = config_with_strategy(max_inventory=2.5)

        engine, _ = build_backtest_engine("B3", config)

        self.assertIsInstance(
            engine.strategy,
            FundingInventoryStrategy,
        )
        self.assertIsNone(engine.feature_engine)
        self.assertEqual(engine.strategy.config.max_inventory, 2.5)
        self.assertEqual(engine.order_manager.config.max_inventory, 2.5)

    def test_format_result_is_human_readable_and_not_json(self) -> None:
        """Print labeled sections instead of a serialized result object."""
        engine, performance_tracker = build_backtest_engine(
            "B0",
            BacktestConfig(),
        )
        result = BacktestResult(
            strategy_id="B0",
            days=("2026-03-19",),
            config=BacktestConfig(),
            metrics=performance_tracker.metrics(engine.account),
            elapsed_seconds=1.25,
            order_count=0,
            snapshot_count=0,
        )

        output = format_result(result)

        self.assertIn("B0 mid-price baseline", output)
        self.assertIn("Assumptions", output)
        self.assertIn("Results", output)
        self.assertIn("Elapsed time: 1.25 seconds", output)
        self.assertFalse(output.lstrip().startswith("{"))

    def test_format_inventory_skew_sensitivity_compares_cases(self) -> None:
        """Show the declared risk parameter and core comparison metrics."""
        engine, performance_tracker = build_backtest_engine(
            "B0",
            BacktestConfig(),
        )
        results = tuple(
            BacktestResult(
                strategy_id="B0",
                days=("2026-03-19",),
                config=config_with_strategy(
                    inventory_skew_ticks=skew_ticks
                ),
                metrics=performance_tracker.metrics(engine.account),
                elapsed_seconds=0.0,
                order_count=0,
                snapshot_count=0,
            )
            for skew_ticks in (
                1.0,
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                7.0,
                8.0,
                9.0,
                10.0,
            )
        )

        output = format_inventory_skew_sensitivity(results)

        self.assertIn("B0 inventory-skew sensitivity", output)
        self.assertIn("mean_abs_inventory", output)
        self.assertEqual(len(output.splitlines()), 12)

    def test_format_volatility_multiplier_sensitivity_compares_cases(
        self,
    ) -> None:
        """Show multiplier values and the metrics needed for interpretation."""
        engine, performance_tracker = build_backtest_engine(
            "B2",
            BacktestConfig(),
        )
        results = tuple(
            BacktestResult(
                strategy_id="B2",
                days=("2026-03-19",),
                config=config_with_strategy(
                    volatility_multiplier=multiplier
                ),
                metrics=performance_tracker.metrics(engine.account),
                elapsed_seconds=0.0,
                order_count=0,
                snapshot_count=0,
            )
            for multiplier in (
                0.5, 1.0, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0,
            )
        )

        output = format_volatility_multiplier_sensitivity(results)

        self.assertIn("B2 volatility-multiplier sensitivity", output)
        self.assertIn("fill_rate", output)
        self.assertIn("1s_markout", output)
        self.assertEqual(len(output.splitlines()), 12)


if __name__ == "__main__":
    unittest.main()
