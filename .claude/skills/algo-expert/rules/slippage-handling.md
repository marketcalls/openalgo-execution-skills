---
name: slippage-handling
description: How slippage is modeled in backtest, controlled in live (LIMIT-with-offset), and measured for drift reports
---

# Slippage Handling

Slippage = the gap between the price you decided on and the price you actually filled at. Three places it shows up:

1. **Backtest** - VectorBT's `slippage` parameter shifts the fill unfavorably
2. **Live entry** - LIMIT-with-offset placement caps how far you'll let it slip
3. **Live measurement** - `SlippageTracker` records actual vs assumed, prints drift report

## Backtest

```python
pf = vbt.Portfolio.from_signals(..., slippage=COSTS.slippage, ...)
```

Default per-segment slippage values (from `core/cost_model.py`):
- Intraday equity: 0.0005 (5 bps)
- Delivery equity: 0.0003 (3 bps)
- F&O futures: 0.0002 (2 bps)
- F&O options: 0.0010 (10 bps)

If your strategy is illiquid-asset specific (low-volume mid/small caps), override with `0.003` (30 bps).

## Live entry (slippage protection)

For `EXECUTION_TYPE="limit"` strategies, place LIMIT orders pegged at LTP +/- offset:

```python
LIMIT_OFFSET_PCT = COSTS.slippage    # cap LIMIT distance at slippage assumption
LIMIT_TIMEOUT_SEC = 3                # fall back to MARKET after 3s

# Place LIMIT BUY at ltp + offset:
limit_buy_price = round(ltp * (1 + LIMIT_OFFSET_PCT), 2)
client.placeorder(price_type="LIMIT", action="BUY", price=limit_buy_price)

# Watch for fill; on timeout, cancel and place MARKET:
deadline = time.time() + LIMIT_TIMEOUT_SEC
while time.time() < deadline:
    status = client.orderstatus(order_id=oid, ...)
    if filled: break
    time.sleep(0.5)
else:
    client.cancelorder(order_id=oid)
    client.placeorder(price_type="MARKET", ...)   # fallback
```

For `EXECUTION_TYPE="eoc"` (default), entries are MARKET orders - slippage isn't capped, but `SlippageTracker` measures it for the drift report.

For `EXECUTION_TYPE="stop"`, use `STOP_TRIGGER_BUFFER` (default 5 bps) to model broker latency past the trigger - the backtest applies this on top of `COSTS.slippage`.

## Live measurement

Every fill is recorded with `decision_price` (what you targeted) vs `fill_price` (what you got):

```python
from core.cost_model import SlippageTracker

slip = SlippageTracker(assumed_pct=COSTS.slippage)

# In the entry path:
slip.record(decision_price=ltp_at_signal,
            fill_price=actual_avg_price,
            qty=QUANTITY, side="BUY")

# In the exit path (RiskManager does this internally):
slip.record(decision_price=trigger_ltp,
            fill_price=exit_avg, qty=QUANTITY, side="SELL")

# At end of session:
log.info("\n%s", slip.report())
```

Sample report:
```
Slippage Report:
  Fills:               14
  Assumed (per side):  0.0500%
  Measured (avg):      0.0823%
  Ratio measured/assumed: 1.65x  [OK]
  Worst slip: 0.2540% (BUY @ decided 1567.50, filled 1571.48)
```

## Drift detection

`SlippageTracker.report()` flags when measured slippage exceeds 2x the backtest assumption:

```
Ratio measured/assumed: 3.21x  [DRIFT - measured >2x assumed]
```

If you see drift consistently:
- Tighten LIMIT offset / MARKET fallback
- Switch from MARKET to LIMIT-with-offset entries
- Re-run backtests with a higher `slippage` value to confirm the strategy still has edge

## Anti-patterns

- **Backtest with `slippage=0`** - fantasy. Your live PnL will be worse, sometimes much worse.
- **Hardcoding slippage to 0.0001 (1 bp)** - too tight unless you're trading the deepest contracts (NIFTY futures, SBIN cash). For options use 10+ bps.
- **Ignoring measured slippage** - if SlippageTracker says 3x drift, your edge is being eaten. Don't blame the strategy until you've verified the cost model matches reality.

## Per-strategy override

If your strategy trades unusually liquid or illiquid contracts, override at the top:

```python
COSTS = cost_lookup(PRODUCT, EXCHANGE)
# Override for thin ATM weekly options:
COSTS = COSTS.__class__(fees=COSTS.fees, fixed_fees=COSTS.fixed_fees,
                        slippage=0.0030, label=f"{COSTS.label} - thin")
```

Or build a custom `CostBlock` from scratch (see `transaction-costs.md`).
