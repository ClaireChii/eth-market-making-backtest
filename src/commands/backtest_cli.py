"""Command-line interface for reproducible market-making backtests."""

from argparse import ArgumentParser, Namespace

from src.backtest import STRATEGY_NAMES, run_backtest
from src.config import EXPECTED_DATES
from src.research.reporting import (
    format_inventory_skew_sensitivity,
    format_result,
    format_volatility_multiplier_sensitivity,
)
from src.research.sensitivity import (
    run_inventory_skew_sensitivity,
    run_volatility_multiplier_sensitivity,
)


def _parse_args() -> Namespace:
    """Parse the strategy and complete calendar days from the command line."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategy",
        choices=tuple(STRATEGY_NAMES),
        default="B0",
        help="strategy variant to run; defaults to B0",
    )
    parser.add_argument(
        "--days",
        nargs="+",
        choices=EXPECTED_DATES,
        default=list(EXPECTED_DATES),
        help="complete UTC days to run; defaults to all three days",
    )
    sensitivity_group = parser.add_mutually_exclusive_group()
    sensitivity_group.add_argument(
        "--inventory-skew-sensitivity",
        action="store_true",
        help="run the exploratory B0 sensitivity cases: 1 through 10 ticks",
    )
    sensitivity_group.add_argument(
        "--volatility-multiplier-sensitivity",
        action="store_true",
        help="run the exploratory B2 multiplier cases: 0.5 through 8.0",
    )
    return parser.parse_args()


def main() -> None:
    """Run the selected command and print its human-readable result."""
    arguments = _parse_args()
    days = tuple(arguments.days)
    if arguments.inventory_skew_sensitivity:
        results = run_inventory_skew_sensitivity(days=days)
        print(format_inventory_skew_sensitivity(results))
        return
    if arguments.volatility_multiplier_sensitivity:
        results = run_volatility_multiplier_sensitivity(days=days)
        print(format_volatility_multiplier_sensitivity(results))
        return

    result = run_backtest(strategy_id=arguments.strategy, days=days)
    print(format_result(result))


if __name__ == "__main__":
    main()
