---
name: transaction-costs
description: Real-world Indian market transaction costs - 4-segment fee model (Intraday, Delivery, Futures, Options). Mirrors vectorbt-backtesting-skills.
---

# Transaction Costs (Indian Market)

The cost model matches the conventions in `vectorbt-backtesting-skills/indian-market-costs.md`. Centralized in `core/cost_model.py`.

## Segment fee table

| Segment | `fees` | `fixed_fees` | `slippage` |
|---|---|---|---|
| Intraday Equity (MIS) | 0.000225 | Rs 20 | 0.0005 |
| Delivery Equity (CNC) | 0.00111 | Rs 20 | 0.0003 |
| F&O Futures (NRML) | 0.00018 | Rs 20 | 0.0002 |
| F&O Options (NRML) | 0.00098 | Rs 20 | 0.0010 |

`fees` = decimal applied to turnover per side. `fixed_fees` = Rs per order (brokerage). `slippage` = decimal added by VectorBT and tracked live.

These derive from STT + exchange transaction + GST + SEBI + stamp duty across a Rs 10L turnover, plus a conservative Rs 20 brokerage estimate. **Override these constants** for your specific broker if you have lower brokerage.

## Auto-resolution by product + exchange

```python
from core.cost_model import lookup as cost_lookup

# In the strategy file:
COSTS = cost_lookup(PRODUCT, EXCHANGE)
# PRODUCT="MIS", EXCHANGE="NSE"  -> INTRADAY_EQ
# PRODUCT="CNC", EXCHANGE="NSE"  -> DELIVERY_EQ
# PRODUCT="NRML", EXCHANGE="NFO" -> FUT_NRML by default
# PRODUCT="NRML", EXCHANGE="NFO", instrument_type="OPT" -> OPT_NRML
```

For options strategies (`short_straddle`, `iron_condor`), import `OPT_NRML` directly:

```python
from core.cost_model import OPT_NRML
COSTS = OPT_NRML
```

## Backtest application

```python
import vectorbt as vbt

pf = vbt.Portfolio.from_signals(
    close, entries, exits,
    fees=COSTS.fees,
    fixed_fees=COSTS.fixed_fees,
    slippage=COSTS.slippage,
    init_cash=INIT_CASH, ...,
)
```

VectorBT applies `fees` as a percentage to each side's turnover, `fixed_fees` per order (entry + exit), and `slippage` shifts the fill price unfavorably.

## Live application

In live mode, the broker takes fees from your funds - no parameter needed. But the strategy:

1. **Reports the assumption** before placing orders:
   ```python
   log.info("\n%s", format_cost_report(COSTS, INIT_CASH))
   ```
2. **Uses `COSTS.slippage` as a LIMIT-with-offset cap** when `EXECUTION_TYPE="limit"` (see `slippage-handling.md`)
3. **Tracks measured slippage** via `SlippageTracker` and prints a drift report at end of session

```python
from core.cost_model import SlippageTracker
slip = SlippageTracker(assumed_pct=COSTS.slippage)

# After each fill:
slip.record(decision_price=ltp, fill_price=fill, qty=qty, side="BUY")

# At end of run:
log.info("\n%s", slip.report())
```

## Customizing for your broker

Edit `core/cost_model.py` or override per-strategy:

```python
from core.cost_model import CostBlock

CUSTOM_COSTS = CostBlock(
    fees=0.00018,           # 0.018% statutory
    fixed_fees=10.0,        # Rs 10/order (cheaper broker)
    slippage=0.0002,        # 2 bps
    label="My broker - F&O",
)

# Then in run_backtest():
fees=CUSTOM_COSTS.fees, fixed_fees=CUSTOM_COSTS.fixed_fees, slippage=CUSTOM_COSTS.slippage
```

## Cost breakdown printout

Backtest mode prints this at the start so the user sees what's assumed:

```
=== Cost Model: F&O Futures (NRML) ===
  Statutory + Exchange:  0.0180% per side
  Brokerage:             Rs 20 per order
  Slippage:              0.0200% per side
  On Rs 1,000,000 turnover:
    statutory  = Rs 180.00 (per side)
    slippage   = Rs 200.00 (per side)
    brokerage  = Rs 20 (per order)
    round-trip = Rs 800.00
```

Use this to sanity-check that your strategy's edge is larger than its costs. If your average win is 0.5% and round-trip is 0.4%, the edge is razor-thin.

## Don't backtest with zero costs

The most common backtest fantasy is `fees=0, slippage=0`. The result is unreliable - real PnL will be far worse. The templates always pass cost values from the cost model.

If you want to simulate "what would this strategy look like at a different fee tier?", change the constant explicitly so you see what assumption you made.

## Cross-reference

- US/Crypto market costs: not implemented in this pack (focus is OpenAlgo + Indian markets). Refer to `vectorbt-backtesting-skills/us-market-costs.md` and `crypto-market-costs.md` for templates.
- Slippage measurement and drift reporting: see `slippage-handling.md`.
