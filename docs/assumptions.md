# Backtest Contract

This document defines the implemented backtest contract. Market-data checks are in [`data_audit.md`](data_audit.md). Results are in [`results.md`](results.md).

## 1. Evaluation scope

The evaluation compares B0 with three single-change variants:

| ID | Strategy |
|---|---|
| B0 | Mid-price + fixed spread + inventory skew |
| B1 | B0 + L1 microprice |
| B2 | B0 + volatility-adaptive spread |
| B3 | B0 + funding-aware inventory target |

Each variant changes one B0 component. All variants share order size, inventory limit, latency, fees, event ordering, fill model, and inventory skew.

| Parameter | Value |
|---|---:|
| Tick size | 0.10 quote-currency units |
| Order quantity | 0.1 ETH per side |
| Absolute inventory limit | 1.0 ETH |
| Fixed full spread | 1 tick |
| Inventory skew at limit | 8 ticks |
| Placement latency | 100 ms |
| Cancellation latency | 100 ms |
| Maker fee | 0 bps |
| Account and quote sampling | 1 second |
| Markout horizons | 1s, 5s, 30s |

No parameter is tuned by day. The initial inventory skew was two ticks at the inventory limit, equivalent to one tick at half the limit. The 1–10 tick sweep selected eight ticks based on PnL and drawdown. The B2 multiplier sweep selected 5. Both sweeps use the same three days as the final comparison. The selected parameters require out-of-sample validation.

## 2. Data interpretation

- Instrument: one ETH perpetual market; no symbol column is expected.
- Timestamps are timezone-naive nanosecond timestamps. They are treated as UTC for the backtest.
- `is_maker_ask = 1` means the resting ask was the maker and the aggressor bought. `0` means the resting bid was the maker and the aggressor sold.
- Order books are snapshots with 20 levels on each side.
- Funding observations arrive roughly every 20 seconds. They are interpreted as a rolling funding signal or forecast, not as a stream of funding payments.

## 3. Event ordering and look-ahead prevention

The simulator processes every timestamp in this fixed order:

- Apply trades only to orders that were already live before the timestamp.
- Activate pending cancellations whose latency has elapsed.
- Activate pending placements whose latency has elapsed.
- Apply order-book snapshots to market state.
- Apply funding signal updates; these are not cash settlements.
- Mark the account using the latest available mid-price.
- Let the strategy make one decision after the timestamp batch is complete.

At the same timestamp, trades are processed before book updates. Events of the same type keep their original file order. Quotes created from a book update cannot fill against trades already processed at that timestamp.

Order placements and cancellations take effect at their scheduled time, even if no market update occurs then.

Funding updates only change the latest funding signal. They do not create fills or cashflows.

## 4. Orders and cancellation

- The primary strategy maintains at most one bid and one ask.
- Prices are rounded passively to the inferred tick: bid down, ask up.
- Orders are post-only. A price is passive when submitted, and placement is rejected if latency makes it cross the current best opposite quote by the time it reaches the simulated exchange.
- Repricing cancels the old order and submits a new one. A new order at the same price loses queue priority unless its live order was left unchanged.
- Placements and cancellations share the configured latency.
- Placement and cancellation latency are both 100 ms.
- Partial fills are allowed.
- Inventory limits are checked against current inventory plus live buy/sell exposure. Quotes that could breach the hard limit are suppressed or resized.

## 5. Fill model

### Visible queue

At activation, an order at a visible price records the displayed quantity in front of it as `queue_ahead`. Subsequent matching aggressive trade volume first depletes that queue and then fills our order, allowing partial fills.

- A resting bid is eligible only for seller-aggressor trades.
- A resting ask is eligible only for buyer-aggressor trades.
- Trade volume is matched at the order price; trades through the price also exhaust the queue and fill the order.
- Book-size changes alone neither create fills nor change `queue_ahead`, because displayed changes cannot be separated reliably into trades, cancellations, and new orders. Only matching prints consume the initial visible queue. This is deliberately conservative.

This model is still an approximation: market-by-order data and the venue's matching rules are unavailable.

## 6. Accounting

The simulator calculates account equity as:

```text
equity_t =
    cash_t
    + inventory_t * mark_price_t
    + cumulative_funding_t
    + cumulative_fees_t
```

- Buy fill: cash decreases; inventory increases.
- Sell fill: cash increases; inventory decreases.
- Positive fee/funding values benefit the account; negative values are costs.
- Mark price is the contemporaneous order-book mid-price.
- Total PnL is the change in ledger equity and must not depend on a cost-basis convention.
- Realized and unrealized PnL use average cost only as analytical attribution.
- Gross trading PnL, fees, funding, and net PnL are reported separately.
- No fee schedule is supplied. Maker fee is set to zero, so gross and net-of-fee PnL are equal.
- End-of-day inventory is marked to market. No terminal fill or liquidation cost is added.

The data contains rolling funding observations but no verified settlement events.
The backtest:

1. uses funding as a slow strategy signal;
2. does not count each observation as a payment;
3. reports funding cashflow as zero.

## 7. Strategy definitions

For B0, the reservation price is mid-price adjusted by inventory deviation, and the half-spread is fixed:

```text
inventory_deviation = inventory
inventory_ratio = clip(inventory_deviation / maximum_inventory, -1, 1)
reservation_price = mid - inventory_skew_ticks * tick_size * inventory_ratio
bid = reservation_price - fixed_half_spread
ask = reservation_price + fixed_half_spread
```

B1 uses parameter-free L1 microprice as fair value:

```text
imbalance = (bid_quantity_1 - ask_quantity_1)
            / (bid_quantity_1 + ask_quantity_1)
microprice = (ask_price_1 * bid_quantity_1
              + bid_price_1 * ask_quantity_1)
             / (bid_quantity_1 + ask_quantity_1)
```

This moves fair value by at most half the current spread and introduces no fitted coefficient.

B2 keeps midpoint as fair value and changes only the half-spread. Midpoint is sampled on a fixed one-second UTC grid using the latest causally available book. The volatility estimate uses the most recent 60 one-second log returns:

```text
return_t = log(mid_t / mid_(t-1))
return_volatility = sqrt(mean(return_t^2))
price_volatility = current_mid * return_volatility
adaptive_half_spread = fixed_half_spread
                       + volatility_multiplier * price_volatility
```

B2 does not estimate volatility until 60 one-second returns are available. Until then, it uses the same spread as B0.

A multiplier of 1.0 adds one estimated one-second root-mean-square price move to each quote side. The final comparison uses a multiplier of 5.0, selected from the reported sensitivity sweep. The tested values are `0.5/1.0/2.0/2.5/3.0/4.0/5.0/6.0/7.0/8.0`.

B2 quotes are clipped to the deepest visible bid and ask levels because the fill model does not model liquidity beyond the supplied book. This limit affects higher multipliers more often.

B3 replaces zero target inventory with a bounded, slowly changing funding-aware target. The latest causally observed funding rate is carried forward until the next update:

```text
funding_rate_scale = 0.0005
funding_target_limit = 0.5 * maximum_inventory
scaled_funding = clip(funding_rate / funding_rate_scale, -1, 1)
target_inventory = -scaled_funding * funding_target_limit
inventory_deviation = inventory - target_inventory
```

Positive funding therefore targets short inventory and negative funding targets long inventory. Before the first funding observation, the target is zero. The absolute inventory limit is unchanged.
The backtest uses a 5 bps scale and caps the funding target at 50% of the inventory limit. No additional smoothing is applied because the funding forecast updates about every 20 seconds. Funding is not used as a high-frequency fair-price signal.

## 8. Primary metrics

- Total and daily gross/net PnL
- Fee and funding cashflows
- Equity and inventory paths
- Maximum drawdown
- Maximum absolute inventory
- Fill count and base/notional fill volume
- Fill rate with an explicit denominator
- Average quoted spread
- Captured half-spread
- Execution markouts at 1s, 5s, and 30s

Captured half-spread and post-fill markout use the same maker-side sign:

```text
side_sign = +1 for buy fills, -1 for sell fills
captured_half_spread = side_sign * (mid_at_fill - fill_price)
markout_h = side_sign * (mid_at_or_after(fill_time + h) - fill_price)
```

The reported values are fill-quantity weighted. Positive is favorable to the maker and negative indicates adverse selection. The mark price is the first observed order-book midpoint at or after the target horizon; unresolved fills
near the end of the sample are excluded and the resolved-volume coverage is reported for every horizon.

Fill rate uses both quantity-based and order-based definitions, with the denominator stated explicitly.
