---
name: indicator-libraries
description: Choosing between openalgo.ta and talib at strategy creation; specialty indicators always use openalgo
---

# Indicator Libraries

The user picks an indicator library at `/algo-strategy` time:

```
Indicator library?
(1) openalgo  [default]   Numba JIT, 100+ indicators, includes specialty (Supertrend, Donchian, Ichimoku, HMA, KAMA)
(2) talib                  Standard C library; very fast for basic indicators; missing specialty ones
```

The choice is baked into the generated file via:
```python
INDICATOR_LIB = "openalgo"   # or "talib"
```

And used through the `core/indicator_adapter.py` shim:
```python
from core.indicator_adapter import get_indicators
ind = get_indicators(INDICATOR_LIB)
ema = ind.ema(close, 20)
rsi = ind.rsi(close, 14)
```

## Why an adapter

- One-line library switch in the strategy file (no other changes needed)
- Both libraries return `pd.Series` indexed by close.index
- Specialty indicators always come from openalgo (talib doesn't have them)
- Backtests and live mode use the same indicator code

## What's in each library

### `openalgo.ta` (default)
100+ indicators, Numba-JIT compiled. From `D:/openalgo-python/openalgo/docs/prompt/openalgo indicators - introduction.md`:

- **Trend** (20): SMA, EMA, WMA, DEMA, TEMA, HMA, VWMA, ALMA, KAMA, ZLEMA, T3, FRAMA, TRIMA, McGinley, VIDYA, Alligator, MA Envelopes, **Supertrend**, **Ichimoku**, ChandeKrollStop
- **Momentum** (9): RSI, MACD, Stochastic, CCI, WilliamsR, BOP, ElderRay, Fisher, CRSI
- **Volatility** (16): ATR, BollingerBands, Keltner, **Donchian**, Chaikin, NATR, RVI, ULTOSC, TRANGE, MASS, BBPercent, BBWidth, ChandelierExit, HistoricalVolatility, UlcerIndex, STARC
- **Volume** (15): OBV, OBVSmoothed, VWAP, MFI, ADL, CMF, EMV, FI, NVI, PVI, VOLOSC, VROC, KVO, PVT, RVOL
- **Oscillators** (20+): ROC, CMO, TRIX, UO, AO, AC, PPO, PO, DPO, AROONOSC, StochRSI, RVI, CHO, CHOP, KST, TSI, VI, STC, GatorOscillator, Coppock
- **Statistical** (9): LINREG, LRSLOPE, CORREL, BETA, VAR, TSF, MEDIAN, MedianBands, MODE
- **Hybrid** (7): ADX, Aroon, PivotPoints, SAR, DMI, WilliamsFractals, RWI
- **Utilities**: crossover, crossunder, exrem, flip, valuewhen, rising, falling, cross, highest, lowest, change, roc, stdev

### `talib`
Standard library - has SMA, EMA, RSI, MACD, BBANDS, ATR, ADX, STDDEV, STOCH, OBV, MFI, AROON, SAR, etc. - but **NOT**:
- Supertrend
- Donchian
- Ichimoku
- HMA, KAMA, ZLEMA, ALMA, VWMA
- exrem, valuewhen, flip

When a strategy uses these, the adapter falls back to openalgo automatically (no error). Document this in the strategy comments if it matters.

## Adapter API

```python
ind.sma(close, period)
ind.ema(close, period)
ind.rsi(close, period=14)
ind.macd(close, fast=12, slow=26, signal=9)        # -> (macd, signal, hist)
ind.atr(high, low, close, period=14)
ind.bbands(close, period=20, std=2.0)              # -> (upper, mid, lower)
ind.adx(high, low, close, period=14)
ind.stochastic(high, low, close, k=14, d=3, smooth=3)  # -> (k_line, d_line)
ind.stdev(close, period)

# openalgo-only (talib backend will fallback to openalgo for these)
ind.supertrend(high, low, close, period=10, multiplier=3.0)  # -> (st, direction)
ind.donchian(high, low, period=20)                            # -> (upper, mid, lower)
ind.hma(close, period)
ind.kama(close, period=10)
ind.crossover(a, b)
ind.crossunder(a, b)
ind.exrem(primary, secondary)
```

All functions return `pd.Series` (or tuple of Series). Index matches the input. NaN-padded at the start where the indicator hasn't warmed up.

## When to pick talib

- Already familiar with talib's parameter naming
- Comfortable that strategy uses only basic indicators (SMA/EMA/RSI/MACD/BBANDS/ATR/ADX/STOCH)
- Need to keep indicator code identical with an existing TA-Lib codebase

## When to pick openalgo (recommended default)

- Want consistency across backtest and analysis (other skill packs use openalgo)
- Need any specialty indicator (Supertrend is the most common)
- New to algo trading - openalgo is curated and avoids pitfalls (e.g. talib's `STOCH` parameter order is non-obvious)
