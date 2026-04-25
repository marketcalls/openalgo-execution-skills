---
name: position-sizing
description: Fixed-fractional and volatility-targeted position sizing - applies in both backtest and live. Default 0.5% risk-per-trade, 50% capital cap.
---

# Position Sizing

Naive backtests with `size=0.95` (95% of equity per trade) produce fantasy returns. Real strategies use **fixed-fractional sizing** keyed off the strategy's own stop loss.

## Fixed-fractional (default for all templates)

```python
RISK_PER_TRADE = 0.005      # 0.5% of capital per trade
MAX_SIZE_PCT   = 0.50       # never deploy more than 50% of capital

# In backtest:
size_pct = fixed_fractional_size(
    risk_per_trade=RISK_PER_TRADE,
    sl_pct=RISK.sl_pct,
    max_size=MAX_SIZE_PCT,
)
# size_pct = min(RISK_PER_TRADE / RISK.sl_pct, MAX_SIZE_PCT)

vbt.Portfolio.from_signals(..., size=size_pct, size_type="percent", ...)
```

For `RISK.sl_pct=0.01` (1% stop): `size_pct = 0.5` → 50% of equity per trade. Worst case = 0.5% loss on capital.
For `RISK.sl_pct=0.025`: `size_pct = 0.20` → 20% of equity. Wider stops shrink positions automatically.

10 consecutive losers at 0.5% each = ~5% drawdown. Recoverable. Compare with naive `size=0.95` where 10 losers @ 0.95% = ~9% drawdown and growing.

## Live mode

`compute_live_qty()` (in `core/sizing.py`) computes integer share/lot count:

```python
qty = compute_live_qty(
    client, SYMBOL, EXCHANGE,
    sl_pct=RISK.sl_pct,
    risk_per_trade=RISK_PER_TRADE,
    lot_size=LOT_SIZE,
    min_qty=LOT_SIZE,
    max_capital_pct=MAX_SIZE_PCT,
)
```

It enforces TWO constraints (the smaller wins):
1. **Risk budget**: `qty * sl_distance_rs <= available_cash * risk_per_trade`
2. **Notional cap**: `qty * ltp <= available_cash * max_capital_pct`

For futures/options, rounds down to lot multiples. Returns 0 if not even one lot fits.

## Volatility-targeted sizing (alternative)

For volatility-regime strategies, scale position inversely to current ATR:

```python
from core.sizing import vol_targeted_size

target_vol = 0.005                  # 0.5% target daily vol
atr_pct = atr.iloc[-1] / close.iloc[-1]
size_pct = vol_targeted_size(target_vol, atr_pct, max_size=1.0)
```

High-vol days → smaller positions. Calm days → larger. Pairs naturally with `atr_breakout`, `bb_squeeze`.

## Don't backtest with too much exposure

The single biggest backtest fantasy is `size=0.95`. The strategy looks great until live trading reveals that 8 consecutive losers wiped out a month of gains. Use the fixed-fractional default everywhere unless you have a specific reason not to.

## Tuning RISK_PER_TRADE

| Strategy character | Suggested RISK_PER_TRADE |
|---|---|
| Conservative trend follower | 0.002 (0.2%) |
| Standard | 0.005 (0.5%) |
| Aggressive (high-edge ML, validated) | 0.01 (1%) |
| Event-driven (high-conviction, infrequent) | 0.01-0.015 |

Higher than 1.5% is dangerous unless you have very strong out-of-sample validation. Below 0.2% means transaction costs eat most of your edge.
