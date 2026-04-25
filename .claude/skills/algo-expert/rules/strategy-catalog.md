---
name: strategy-catalog
description: Reference table of all 12 strategy templates - signal logic, default execution type, recommended timeframe, risk profile
---

# Strategy Catalog

All 12 templates in `assets/`. Each is a single-file dual-mode strategy (backtest + live).

## Standard strategies (8)

| Template | Type | Default Execution | Default Interval | Logic |
|----------|------|-------------------|-------------------|-------|
| `ema_crossover` | Trend | eoc | 5m / D | Fast EMA crosses slow EMA. exrem-cleaned signals. |
| `rsi` | Mean reversion | eoc | 15m | RSI(14) crosses above 30 (oversold reversal) / below 70 (OB reversal) |
| `supertrend` | Trend | eoc | 5m | Supertrend direction flip from down→up (long) / up→down (exit) |
| `donchian` | Breakout | eoc | D | Close breaks prior 20-bar high / low. `.shift(1)` to avoid lookahead |
| `macd` | Trend | eoc | D | MACD crosses signal AND MACD>0 (regime filter) |
| `opening_range` | Breakout | **stop** | 5m | First 15-min OR. Broker-side SL-M trigger orders, OCO |
| `atr_breakout` | Volatility | **limit** | 5m | LIMIT pegged at close +/- N*ATR. Modifies on tick |
| `bb_squeeze` | Volatility | eoc | 15m | BB width contraction → entry on band break |

## Options strategies (2) - execution-only

| Template | Type | Logic | Notes |
|----------|------|-------|-------|
| `short_straddle` | Premium short | Sell ATM CE + ATM PE at scheduled time, per-leg SL at 1.30x premium | Backtest disabled |
| `iron_condor` | Defined-risk credit | OTM4 short body + OTM8 long wings | Backtest disabled |

## ML strategies (2)

| Template | Type | Files |
|----------|------|-------|
| `ml_logistic` | Probabilistic | `train.py` + `strategy.py`. sklearn LogisticRegression pipeline. Triple-barrier label |
| `ml_xgb` | Probabilistic | `train.py` + `strategy.py`. XGBoost classifier with walk-forward CV. Triple-barrier label |

## Advanced strategies (3)

| Template | Type | Description |
|----------|------|-------------|
| `pairs_trading` | Stat-arb | Long Y / short X spread mean-reversion. Z-score entry/exit, configurable hedge lookback |
| `regime_trend` | Filtered trend | EMA crossover gated by ADX > 25, INDIAVIX < 22, volume > 1.2x avg |
| `event_driven` | Scheduled | Time-based entries on configured event dates (results, dividends). HOLDING_DAYS exit |

## Picking a template

| Goal | Template |
|---|---|
| Simple intraday trend | `ema_crossover` |
| Daily swing trend | `donchian` or `macd` |
| Mean reversion on liquid stocks | `rsi` |
| Volatility expansion | `atr_breakout` |
| Squeeze/breakout setup | `bb_squeeze` |
| Index futures trend | `supertrend` |
| ORB intraday | `opening_range` |
| Premium-selling on index options | `short_straddle` or `iron_condor` |
| Predictive ML approach | `ml_logistic` (simple) or `ml_xgb` (complex) |

## Risk defaults per template

These are starting points - tune via backtest before going live.

| Template | sl_pct | tp_pct | trail_pct | time_exit_min |
|----------|--------|--------|-----------|----------------|
| `ema_crossover` | 0.010 | 0.020 | 0.008 | 240 |
| `rsi` | 0.015 | 0.030 | 0.012 | 180 |
| `supertrend` | 0.012 | None | 0.015 | None |
| `donchian` | 0.025 | None | 0.020 | None |
| `macd` | 0.020 | None | 0.018 | None |
| `opening_range` | 0.005 | None | 0.005 | None |
| `atr_breakout` | 0.012 | 0.025 | 0.010 | 240 |
| `bb_squeeze` | 0.012 | 0.025 | 0.010 | 180 |
| `ml_logistic` | 0.012 | 0.025 | 0.010 | 180 |
| `ml_xgb` | 0.012 | 0.025 | 0.010 | 180 |

Options strategies (`short_straddle`, `iron_condor`) use per-leg `SL_MULTIPLIER * premium` (e.g. 1.30x) instead of position-level percentages - rules differ for premium-short structures.

## Default symbols

Templates ship with reasonable defaults that the user is expected to override:

| Template | Default symbol | Default exchange |
|---|---|---|
| `ema_crossover` | SBIN | NSE |
| `rsi` | RELIANCE | NSE |
| `supertrend` | NIFTY | NSE_INDEX |
| `donchian` | NIFTY | NSE_INDEX |
| `macd` | INFY | NSE |
| `opening_range` | SBIN | NSE |
| `atr_breakout` | RELIANCE | NSE |
| `bb_squeeze` | TCS | NSE |
| `short_straddle` | NIFTY | NSE_INDEX |
| `iron_condor` | NIFTY | NSE_INDEX |
| `ml_logistic` | RELIANCE | NSE |
| `ml_xgb` | RELIANCE | NSE |

## Indicator library compatibility

All templates default to `INDICATOR_LIB="openalgo"`. Switch to `"talib"` is one line. Templates that use specialty indicators (`supertrend`, `donchian`) automatically fall back to openalgo for those calls even if talib is set globally.

## Multi-template portfolios

Use `/algo-portfolio config.yaml` to run multiple strategies under one supervisor:

```yaml
capital: 1000000
portfolio_caps:
  portfolio_sl_pct: 0.02
  daily_loss_pct: 0.015
  max_concurrent_positions: 4
strategies:
  - name: ema_sbin
    path: strategies/ema_sbin/strategy.py
  - name: rsi_reliance
    path: strategies/rsi_reliance/strategy.py
  - name: supertrend_nifty
    path: strategies/supertrend_nifty/strategy.py
  - name: orb_sbin
    path: strategies/orb_sbin/strategy.py
```
