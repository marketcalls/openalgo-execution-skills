---
name: event-loop
description: Three event-loop patterns for live execution - end-of-candle polling, tick-driven LIMIT, broker-side stop trigger
---

# Event Loops

Each `EXECUTION_TYPE` has a different live event loop. The risk manager runs concurrently regardless.

## eoc loop

Polls `client.history()` every `POLL_INTERVAL_SEC` (15s default). Detects new closed bars by comparing the second-to-last timestamp:

```python
class BarCloseWatcher:
    def __init__(self, ...):
        self.last_seen_ts = None

    def run(self):
        while not self.stop_event.is_set():
            df = poll_history(...)
            if len(df) >= 2:
                closed_ts = df.index[-2]
                if closed_ts > self.last_seen_ts:
                    self.last_seen_ts = closed_ts
                    self.on_bar_close(df)
            self.stop_event.wait(self.poll_interval_sec)
```

`on_bar_close(df)` is called once per closed bar. It evaluates `signals(df)` on `iloc[-2]` and places orders.

This is in `core/data_router.py` - already wired into every eoc strategy template.

## limit loop (tick-driven)

Subscribes LTP, modifies LIMIT orders on each tick:

```python
def run_live_limit():
    # Initial LIMIT placement
    buy_oid  = client.placeorder(price_type="LIMIT", price=initial_buy)["orderid"]
    sell_oid = client.placeorder(price_type="LIMIT", price=initial_sell)["orderid"]

    last_modify = 0
    def on_tick(data):
        nonlocal last_modify
        if state["position"] is not None: return  # don't modify after fill
        ltp = float(data["data"]["ltp"])
        # Throttle: don't modify more than once per second
        if time.time() - last_modify < LIMIT_MODIFY_THROTTLE: return
        new_buy  = compute_new_band(ltp, side="BUY")
        new_sell = compute_new_band(ltp, side="SELL")
        if abs(new_buy - state["buy_price"]) >= 0.05:
            client.modifyorder(order_id=buy_oid, price=str(new_buy), ...)
            state["buy_price"] = new_buy
        last_modify = time.time()

    client.subscribe_ltp(instruments, on_data_received=on_tick)

    # Outer loop: OCO + risk handoff
    while not stop_event.is_set():
        if any_filled():
            cancel_other_leg()
            arm_risk_manager()
            break
        if past_squareoff_time():
            cleanup_and_exit()
            break
        stop_event.wait(1)
```

Pattern from `atr_breakout/strategy.py`.

## stop loop (broker-side trigger)

Place SL/SL-M orders, broker activates them. Light client polling for OCO:

```python
def run_live_stop():
    # Place broker-side trigger orders
    buy_oid  = client.placeorder(price_type="SL-M", action="BUY",
                                 trigger_price=high_trigger, ...)["orderid"]
    sell_oid = client.placeorder(price_type="SL-M", action="SELL",
                                 trigger_price=low_trigger, ...)["orderid"]

    while not stop_event.is_set():
        if past_squareoff_time():
            client.cancelallorder(strategy=STRATEGY_NAME)
            client.closeposition(strategy=STRATEGY_NAME)
            break

        if state["filled_side"] is None:
            for side, oid_key in [("BUY","buy_oid"), ("SELL","sell_oid")]:
                oid = state.get(oid_key)
                r = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
                if r["data"]["order_status"] == "complete":
                    state["filled_side"] = side
                    cancel_other_oco_leg()
                    arm_risk_manager()
                    break
        stop_event.wait(2)
```

Pattern from `opening_range/strategy.py`.

## Risk manager (always running)

Independent of which event loop is active. Subscribes to LTP for the open position only. Runs in the WS callback thread (one). Spawns a worker thread per exit (transient).

So a typical live process has:
- Main thread - event loop (eoc / limit / stop polling)
- WS thread - tick callback dispatcher (handled internally by openalgo SDK)
- Risk manager exit worker(s) - one-off worker per exit firing

## APScheduler for time-based entries

```python
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

ist = pytz.timezone("Asia/Kolkata")
scheduler = BackgroundScheduler(timezone=ist)
scheduler.add_job(
    place_straddle_entry,
    trigger="cron",
    day_of_week="mon-fri",
    hour=9, minute=20,
    id="nifty_0920",
)
scheduler.start()

# Keep main alive
try:
    while not stop_event.is_set():
        time.sleep(1)
finally:
    scheduler.shutdown()
```

Pattern from `examples/python/straddle_scheduler.py`. Used in options strategies (`short_straddle`, `iron_condor`) for scheduled entries.

## Don't mix paradigms

Don't run two event loops in the same process. Pick one:
- Trend strategy → eoc with risk manager
- Breakout-on-touch → limit with risk manager
- ORB → stop with risk manager
- Scheduled options entry → eoc-after-time + risk manager (no APScheduler if you control the wait yourself)

Multi-strategy multi-symbol = `/algo-portfolio` with subprocess isolation.

## Loop heartbeat

For long-running processes, log a periodic heartbeat:

```python
HEARTBEAT_SEC = 60
last_hb = time.time()
while not stop_event.is_set():
    if time.time() - last_hb >= HEARTBEAT_SEC:
        log.info("heartbeat: pos=%s ltp=%s", state["position"], current_ltp)
        last_hb = time.time()
    ...
```

Easy to grep when reviewing `logs/strategies/*.log` from the OpenAlgo host.
