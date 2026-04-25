---
name: execution-types
description: Three live execution patterns - end-of-candle, real-time limit, broker-side stop trigger - with code skeletons and tradeoffs
---

# Execution Types

Asked at `/algo-strategy` time. Set in the file as `EXECUTION_TYPE = "eoc" | "limit" | "stop"`. Determines how `run_live()` is structured.

| Type | Best for | Latency | Slippage profile |
|---|---|---|---|
| `eoc` | Trend / momentum / mean-reversion | Bar interval (1m-D) | Full bid-ask + market impact |
| `limit` | Breakout-on-touch, scalping, depth-aware entries | Tick latency | 0-1 tick if filled; full slippage on MARKET fallback |
| `stop` | ORB, fail-safe stops, lower-latency triggers | Broker-side latency | 1-3 ticks past trigger |

## eoc (end-of-candle)

Polls `client.history()` every `POLL_INTERVAL_SEC`. When a new bar closes, evaluates `signals(df)` on `iloc[-2]` and places a MARKET order.

```python
def run_live_eoc():
    client.connect()
    risk_mgr = RiskManager(...); state = {"position": None}
    warmup_live_data(client, SYMBOL, EXCHANGE, INTERVAL)

    def on_bar_close(df):
        entries, exits = signals(df)
        if entries.iloc[-2] and state["position"] is None:
            r = client.placeorder(price_type="MARKET", action="BUY", ...)
            # ... fill polling, set position, arm risk_mgr ...
        elif exits.iloc[-2] and state["position"] is not None:
            client.placesmartorder(action="SELL", price_type="MARKET",
                                   position_size=0, ...)

    BarCloseWatcher(client, SYMBOL, EXCHANGE, INTERVAL, on_bar_close,
                    poll_interval_sec=POLL_INTERVAL_SEC,
                    stop_event=stop_event).run()
```

The risk manager is **always tick-driven** even in eoc mode. Only the entry signal is bar-driven.

Templates: `ema_crossover`, `rsi`, `supertrend`, `donchian`, `macd`, `bb_squeeze`, `ml_logistic`, `ml_xgb`.

## limit (real-time limit)

Pre-places LIMIT orders at calculated levels. Modifies them as the level shifts (e.g. trailing breakout band). Cancels via `client.cancelorder()` when the opposite leg fills (OCO).

```python
def run_live_limit():
    client.connect()
    # 1. Compute initial bands from history
    df = warmup_live_data(...)
    upper, lower = compute_bands(df)

    # 2. Place LIMIT BUY at upper band, LIMIT SELL at lower
    buy_oid  = client.placeorder(price_type="LIMIT", action="BUY",  price=upper)
    sell_oid = client.placeorder(price_type="LIMIT", action="SELL", price=lower)

    # 3. Subscribe LTP, modify orders on tick
    def on_tick(data):
        ltp = float(data["data"]["ltp"])
        if abs(new_upper - upper) >= tick_size:
            client.modifyorder(order_id=buy_oid, price=new_upper)

    client.subscribe_ltp(instruments, on_data_received=on_tick)

    # 4. OCO fill polling + risk handoff
    while not stop_event.is_set():
        for oid in [buy_oid, sell_oid]:
            if check_filled(oid):
                cancel_other_leg()
                arm_risk_manager()
                break
        time.sleep(1)
```

**LIMIT_TIMEOUT_SEC fallback**: if the level isn't hit by some deadline, fall back to MARKET to guarantee execution. Add this if your strategy MUST trade today (e.g. opening range), skip it if MISSING the trade is fine (e.g. swing breakout - wait for tomorrow).

Throttle modifies via `LIMIT_MODIFY_THROTTLE` (1-2s) to avoid broker rate-limit hits.

Templates: `atr_breakout`.

## stop (broker-side trigger)

Places `SL` or `SL-M` orders that the **broker** activates when LTP hits the trigger. Lower client-side latency (no need for OpenAlgo + your script to be reachable at trigger time).

```python
def run_live_stop():
    client.connect()
    # 1. Compute trigger levels at session start
    high, low = compute_orb_levels(client.history(...))

    # 2. Place broker-side SL-M trigger orders (broker fires them)
    buy_oid  = client.placeorder(
        price_type="SL-M", action="BUY",  trigger_price=high, ...
    )["orderid"]
    sell_oid = client.placeorder(
        price_type="SL-M", action="SELL", trigger_price=low, ...
    )["orderid"]

    # 3. Light polling on orderstatus for OCO + risk handoff
    while not stop_event.is_set():
        for oid in [buy_oid, sell_oid]:
            r = client.orderstatus(order_id=oid, strategy=...)
            if r["data"]["order_status"] == "complete":
                client.cancelorder(order_id=other_oid)
                arm_risk_manager()
                break
        time.sleep(2)

    # 4. End-of-day cleanup
    client.cancelallorder(strategy=...)
```

Use `SL` (stop-limit) over `SL-M` (stop-market) when:
- You want a price ceiling on your fill (acceptable to miss the move at extreme prices)
- Don't use SL on illiquid options - the LIMIT half can fail to fill

Templates: `opening_range` (uses SL-M for both legs).

## Hybrid: eoc entry + stop exit

Options strategies typically combine: enter at scheduled time via `optionsmultiorder()` (MARKET), then place per-leg `SL` orders for risk:

```python
# Entry
client.optionsmultiorder(legs=[...])

# Per-leg SL
client.placeorder(symbol=ce_sym, action="BUY", price_type="SL",
                  trigger_price=ce_sl, price=ce_sl_buffered, ...)
client.placeorder(symbol=pe_sym, action="BUY", price_type="SL",
                  trigger_price=pe_sl, price=pe_sl_buffered, ...)
```

Templates: `short_straddle`, `iron_condor`.

## Backtest behavior per type

VectorBT can't perfectly simulate broker-side latency or LIMIT-fill ambiguity. We approximate:

| Type | Backtest treatment |
|---|---|
| eoc | `from_signals` standard; entry at next-bar open; `slippage` = COSTS.slippage |
| limit | `from_signals` with `slippage` = COSTS.slippage (assumes most LIMITs fill cleanly) |
| stop | `from_signals` with `slippage` = COSTS.slippage + STOP_TRIGGER_BUFFER (1-3 tick latency past trigger) |

All three honor the same `fees` and `fixed_fees` from the cost model.

## Picking the right type

| Strategy character | Recommended |
|---|---|
| Slow trend follower (D/1h) | `eoc` |
| Mean reversion (multi-bar hold) | `eoc` |
| Intraday momentum (5m/15m) | `eoc` |
| Breakout-on-touch (5m, want to lift offer) | `limit` |
| Pin-bar / pullback at level | `limit` |
| Opening Range Breakout | `stop` |
| Pivot break with broker latency tolerance | `stop` |
| Anything where missing the trade beats slipping | `limit` (no MARKET fallback) |
| Anything that MUST trade today | `eoc` or `limit` with MARKET fallback |
