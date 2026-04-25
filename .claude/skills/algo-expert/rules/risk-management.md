---
name: risk-management
description: Per-position risk - SL, TP, trailing stop, time exit. Same config drives backtest and live.
---

# Per-Position Risk Management

Each strategy declares one `RiskConfig`. The same values are used in both modes:
- **Backtest** - passed to `vbt.Portfolio.from_signals(sl_stop=, tp_stop=, sl_trail=)`
- **Live** - the `RiskManager` (in `core/risk_manager.py`) subscribes to LTP and fires `client.placesmartorder(position_size=0)` when a threshold breaks

## RiskConfig

```python
from core.risk_manager import RiskConfig

RISK = RiskConfig(
    sl_pct=0.01,          # 1% stop loss (decimal of entry)
    tp_pct=0.02,          # 2% take profit
    trail_pct=0.008,      # 0.8% trailing stop (only after favourable move)
    time_exit_min=240,    # exit after 240 minutes regardless
    sl_abs=None,          # absolute price level (overrides sl_pct if set)
    tp_abs=None,          # absolute take profit price
)
```

Set any field to `None` to disable that check.

## Threshold logic (live)

```
For BUY positions:
  watermark = max(watermark, ltp)            # high-water mark
  SL hit if   ltp <= entry * (1 - sl_pct)
  TP hit if   ltp >= entry * (1 + tp_pct)
  TRAIL hit if ltp <= watermark * (1 - trail_pct)  AND watermark > entry
  TIME hit if  now - entry_time >= time_exit_min

For SELL positions: mirror image.
```

The risk manager updates `watermark` on every favorable tick, so trailing stop locks in profits as the trade goes well.

## Pattern (already wired into core/risk_manager.py)

```python
from core.risk_manager import RiskManager, RiskConfig, Position

risk_mgr = RiskManager(client, STRATEGY_NAME, RISK,
                       on_exit_callback=on_position_closed,
                       slippage_tracker=slip)

# After a fill:
pos = Position(SYMBOL, EXCHANGE, "BUY", QUANTITY, fill_price,
               time.time(), PRODUCT, STRATEGY_NAME)
risk_mgr.set_position(pos)        # subscribes WS, starts checking thresholds

# When the strategy's own exit signal fires:
risk_mgr.clear_position()         # unsubscribes, returns to standby
```

## How the live risk manager actually fires

1. `RiskManager._on_tick(data)` - the WS callback. Reads LTP, updates watermark. Calls `_check_exits()` to decide if any threshold breaks.
2. If yes - **spawns a worker thread** that calls `client.placesmartorder(position_size=0)`. The WS callback never blocks on broker latency.
3. The worker polls `orderstatus()` for the fill, records slippage, fires `on_exit_callback`.
4. The position is marked `closed`, the WS subscription is dropped.

This is the canonical pattern from `D:/openalgo-python/openalgo/examples/python/emacrossover_strategy_python.py` - the WS callback never blocks; exits go through worker threads.

## Why `placesmartorder(position_size=0)` for exits

`placesmartorder` considers the current broker-side position. By passing `position_size=0`, you say "make my position size 0" - the SDK calculates the correct quantity to flatten cleanly:
- If you opened 1 lot, it sells 1 lot
- If you have a partial fill, it sells only what was filled
- Avoids over-selling on partial-fill scenarios that a plain `placeorder(action=SELL)` would cause

## Backtest behavior

VectorBT applies the thresholds during simulation:

```python
pf = vbt.Portfolio.from_signals(
    close, entries, exits,
    sl_stop=RISK.sl_pct,                          # 0.01
    tp_stop=RISK.tp_pct,                          # 0.02
    sl_trail=False if RISK.trail_pct is None else RISK.trail_pct,  # 0.008
    ...
)
```

VectorBT doesn't have native `time_exit_min` - if you need that in backtest, generate exit signals separately:

```python
# Add time-based exit to the exits Series
import pandas as pd
time_exits = pd.Series(False, index=df.index)
# mark exits at entry_time + time_exit_min (post-process the entries Series)
exits = exits | time_exits
```

## Threshold tuning

| Strategy character | Suggested SL | Suggested TP | Suggested trail |
|---|---|---|---|
| Trend follower (slow) | 2-3% | None | 1.5-2% |
| Trend follower (intraday) | 0.8-1.2% | None | 0.5-0.8% |
| Mean reversion | 1.5-2% | 2-4% | None or wider |
| Volatility breakout | 1-1.5% | 2-3% | 0.8-1.2% |
| Options short premium | None (rely on per-leg SL) | None | None |

**Always** factor in costs - if `sl_pct=0.005` (0.5%) but round-trip cost is 0.2%, you only have 0.3% real risk before fees eat your edge. See `transaction-costs.md`.

## Validating risk fires correctly

Use `/algo-risk-test` (covered in `algo-risk-test/SKILL.md`) to inject synthetic ticks and verify each threshold triggers as expected, in OpenAlgo's sandbox mode.

## State persistence (restart-safe)

Pass a `StrategyState` to RiskManager to persist watermark + position metadata to SQLite. After a SIGTERM and restart, `reconcile_with_broker()` resumes the position with its prior watermark intact - trailing stops keep their lock-in.

```python
from core.state import StrategyState, reconcile_with_broker

state_db = StrategyState(_HERE / "state.db")
risk_mgr = RiskManager(client, STRATEGY_NAME, RISK,
                       state=state_db,
                       slippage_tracker=slip)

resumed = reconcile_with_broker(state_db, client, SYMBOL, EXCHANGE)
if resumed is not None:
    pos = Position(...)   # rebuild from resumed
    risk_mgr.set_position(pos, restore_watermark=resumed.watermark)
```

See `state-persistence.md` for the SQLite schema and reconciliation pattern.

## Coupling with position sizing

Risk thresholds drive position sizing - see `position-sizing.md`. The default fixed-fractional sizer is `RISK_PER_TRADE / RISK.sl_pct`, capped at `MAX_SIZE_PCT`. Wider stops = smaller positions = constant per-trade risk. Don't separately tune `size` in the backtest - let the formula derive it.
