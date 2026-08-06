# ETH Perpetual Market-Making Backtest

**Author:** Jiao Chi

This repository contains a causal, event-driven market-making simulator for the supplied ETH perpetual data from 2026-03-19 through 2026-03-21. The backtest compares a baseline with three single-change variants.

| Variant | Change |
|---|---|
| B0 | Midpoint fair value, fixed spread, and inventory skew |
| B1 | Replace midpoint with L1 microprice |
| B2 | Add a volatility-based spread adjustment |
| B3 | Move target inventory using the latest funding signal |

All variants use the same eight-tick inventory skew. B2 uses a volatility multiplier of 5.0. Both parameters were selected on the three-day evaluation sample and require out-of-sample validation.

All four variants lose money over the full sample. B2 has the smallest loss and drawdown because it quotes wider and trades less. Its execution markouts remain negative. Full results are in [`docs/results.md`](docs/results.md).

## Data

| Directory | Contents |
|---|---|
| `raw_data/orderbook/` | Twenty bid and ask levels with price and quantity |
| `raw_data/trades/` | Trade price, size, and aggressor direction |
| `raw_data/fundings/` | Rolling funding-rate observations |

The files do not identify the quote currency or timezone. Monetary values are reported in quote-currency units. Naive timestamps are treated as UTC for the backtest.

## Documentation

- [`docs/data_audit.md`](docs/data_audit.md): data coverage, quality checks, and observed distributions.
- [`docs/assumptions.md`](docs/assumptions.md): event ordering, order handling, fills, accounting, and strategy definitions.
- [`docs/results.md`](docs/results.md): results, sensitivity sweeps, figures and limitations.

## Implementation

- `src/market_data.py` batches trades, books, and funding updates without losing nanosecond precision.
- `src/engine.py` processes events in a fixed order to prevent look-ahead bias.
- `src/order_manager.py` handles latency, post-only activation, cancellations, queue position, and inventory limits.
- `src/fill_model.py` fills resting orders from matching trades after visible queue ahead is consumed.
- `src/account.py` tracks cash, inventory, fees, funding cashflow, and marked equity.
- `src/strategy.py` implements B0–B3.
- `src/features.py` calculates L1 microprice and rolling one-second volatility.
- `src/performance.py` records PnL, drawdown, inventory, fills, spread capture, and markouts.
- `src/config.py` contains the shared strategy and simulation parameters.

Strategy decisions occur once per public-event timestamp. Account and quote states are sampled at most once per second. Future returns used by the signal diagnostics never enter a strategy decision.

## Reproduction

Python 3.11 or later and `uv` are required. Install the locked environment:

```bash
uv sync --dev
```

Run the data audit:

```bash
uv run python -m src.commands.audit_data
```

Print the complete audit evidence as JSON:

```bash
uv run python -m src.commands.audit_data --json
```

Run one day for a quick check:

```bash
uv run python -m src.commands.backtest_cli --days 2026-03-19
```

Run the complete B0–B3 comparison:

```bash
uv run python -m src.commands.backtest_cli --strategy B0
uv run python -m src.commands.backtest_cli --strategy B1
uv run python -m src.commands.backtest_cli --strategy B2
uv run python -m src.commands.backtest_cli --strategy B3
```

Run the inventory-skew and volatility-multiplier sweeps:

```bash
uv run python -m src.commands.backtest_cli --inventory-skew-sensitivity
uv run python -m src.commands.backtest_cli --volatility-multiplier-sensitivity
```

Run the B1 and B2 signal diagnostics:

```bash
uv run python -m src.research.analyze_signals
```

Regenerate the report figures:

```bash
uv run python -m src.commands.plot_results
```

Run the test suite:

```bash
uv run python -m pytest -q
```
