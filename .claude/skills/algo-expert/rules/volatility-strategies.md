---
name: volatility-strategies
description: ATR breakouts, Bollinger Band squeeze, Donchian channel, VIX-gated entries
---

# Volatility Strategies

Family of strategies that act on volatility expansion or contraction.

## Categories

| Strategy | Logic | Template |
|---|---|---|
| ATR breakout | Enter on price beyond Close +/- N*ATR | `atr_breakout/` |
| Bollinger squeeze | Wait for narrow band width, enter on band break | `bb_squeeze/` |
| Donchian | Enter on prior N-bar high/low break | `donchian/` |
| VIX-gated | Trade trend strategies only when VIX < threshold | (no template - extension) |
| Keltner channel | Like Bollinger but ATR-based; range-bound | (no template - extension) |

## ATR breakout (real-time LIMIT execution)

Computes a per-bar volatility band:
```
upper = close.shift(1) + ATR_MULTIPLIER * ATR(N).shift(1)
lower = close.shift(1) - ATR_MULTIPLIER * ATR(N).shift(1)
```

Pre-places LIMIT orders at the bands so you lift the offer/hit the bid the moment price reaches the level. Modifies the LIMIT prices on each tick as ATR drifts.

See `atr_breakout/strategy.py` for the canonical implementation.

Tuning:
- `ATR_PERIOD = 14` for intraday, 20 for daily
- `ATR_MULTIPLIER = 1.5` standard; 2.0+ for slower bands
- `LIMIT_OFFSET_PCT = 0.0005` peg distance (5 bps); tighter for liquid contracts

## Bollinger Band squeeze (eoc execution)

Squeeze = current bandwidth is at a recent minimum. Often precedes a breakout move.
```
upper, mid, lower = bbands(close, BB_PERIOD, BB_STD)
width = (upper - lower) / mid
in_squeeze = width <= width.rolling(SQUEEZE_LOOKBACK).min() * 1.05
```
Entry on band break **after** a squeeze:
```
long_entry = (close > upper) & in_squeeze.shift(1)
```

See `bb_squeeze/strategy.py`.

Tuning:
- `BB_PERIOD = 20`, `BB_STD = 2.0` (standard)
- `SQUEEZE_LOOKBACK = 50` to define "recent minimum" - higher = pickier
- `1.05` tolerance allows for jitter in the minimum detection

## Donchian breakout (eoc execution)

Long when close breaks above prior 20-bar high. Sell when close breaks below prior 20-bar low.
```
upper, _, lower = donchian(high, low, DONCHIAN_PERIOD)
entries = close > upper.shift(1)
exits   = close < lower.shift(1)
```
Always `.shift(1)` to compare against the **prior** bar's channel - prevents lookahead.

See `donchian/strategy.py`.

Tuning:
- 20-bar standard for daily, 60-bar for intraday breakouts
- Pair with a trend filter (ADX > 25) to avoid false breakouts in chop

## VIX-gated trend (extension - not in templates)

Add an INDIA VIX filter to any trend strategy:
```python
vix_data = client.history(symbol="INDIAVIX", exchange="NSE_INDEX", interval="D", ...)
vix_ok = vix_data["close"].iloc[-2] < 18.0
```
Only trade when VIX is below a threshold (e.g. 18). Trend strategies often fail in high-VIX regimes; mean-reversion thrives.

## Keltner channel (extension)

Similar to Bollinger but uses ATR for band width:
```python
ema_mid = ind.ema(close, 20)
atr = ind.atr(high, low, close, 14)
upper = ema_mid + 1.5 * atr
lower = ema_mid - 1.5 * atr
```
Less prone to false squeeze signals than Bollinger in high-volume names.

## Combining volatility filters with other strategies

Volatility strategies pair well with:
- **Trend filter** (`ADX > 25`, `EMA50 > EMA200`) - reduces chop
- **Time filter** (only first hour of trading) - more breakouts on opening drives
- **Volume filter** (`volume > rolling_avg_volume * 1.5`) - confirms breakout participation

```python
ind = get_indicators(INDICATOR_LIB)
adx = ind.adx(df["high"], df["low"], df["close"], 14)
trend_ok = adx > 25

vol_filter = df["volume"] > df["volume"].rolling(20).mean() * 1.5

entries = base_entry & trend_ok & vol_filter
```

## Risk profiles

| Strategy | SL | TP | Trail |
|---|---|---|---|
| ATR breakout | 1.0-1.5 * ATR | 2-3 * ATR | After 1*ATR profit |
| BB squeeze | 1% (tight - mean reversion if break fails) | 2-3% | 1% |
| Donchian | 2-2.5% | None | 1.5-2% |

Always express stops as **multiples of ATR** (or % of the band width) rather than fixed % - the strategy adapts to changing volatility regimes.
