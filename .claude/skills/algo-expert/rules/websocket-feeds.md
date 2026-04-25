---
name: websocket-feeds
description: WebSocket subscription lifecycle - LTP, Quote, Depth modes; reconnection; verbose levels
---

# WebSocket Feeds

OpenAlgo's unified WebSocket runs on `ws://<host>:8765` (or `wss://<domain>/ws` in prod). The Python SDK wraps the protocol so you call `client.connect()` / `subscribe_*()` / `unsubscribe_*()` / `disconnect()`.

## Three modes

| Mode | API method | Returns |
|---|---|---|
| LTP | `subscribe_ltp(...)` | `ltp + timestamp` only |
| Quote | `subscribe_quote(...)` | OHLC + LTP + volume + change + change_percent + last_trade_quantity + avg_trade_price |
| Depth | `subscribe_depth(...)` | LTP + bids/asks 5L (or 20/30/50 if broker supports) |

## Lifecycle (canonical pattern)

```python
client = api(api_key="...", host="...", ws_url="ws://127.0.0.1:8765",
             verbose=False)

instruments = [
    {"exchange": "NSE", "symbol": "RELIANCE"},
    {"exchange": "NSE", "symbol": "INFY"},
]

def on_data(data):
    # data["data"]["ltp"] etc - see payload shapes in sdk-reference.md
    ltp = float(data["data"]["ltp"])
    print(f"{data['data']['symbol']} LTP: {ltp}")

client.connect()
client.subscribe_ltp(instruments, on_data_received=on_data)

try:
    while True:
        time.sleep(1)
finally:
    client.unsubscribe_ltp(instruments)
    client.disconnect()
```

## Two-thread live model

WebSocket callbacks must stay fast. Don't place orders or poll long-running APIs inside them. Pattern from `examples/python/emacrossover_strategy_python.py`:

```python
class StrategyBot:
    def __init__(self):
        self.position = None
        self.exit_in_progress = False
        self.stop_event = threading.Event()

    def on_ltp(self, data):
        if not self.position or self.exit_in_progress:
            return
        ltp = float(data["data"]["ltp"])
        if ltp <= self.stoploss_price:
            self.exit_in_progress = True
            # Spawn worker - DO NOT block the WS callback
            threading.Thread(
                target=self.place_exit_order,
                args=("STOPLOSS",), daemon=True,
            ).start()

    def signal_thread(self):
        while not self.stop_event.is_set():
            df = self.client.history(...)
            sig = self.check_signals(df)
            if sig:
                self.place_entry(sig)
            time.sleep(SIGNAL_CHECK_INTERVAL)   # 5-15s

    def ws_thread(self):
        self.client.connect()
        self.client.subscribe_ltp(self.instruments, on_data_received=self.on_ltp)
        while not self.stop_event.is_set():
            time.sleep(1)
```

This is exactly what `core/risk_manager.py` implements internally.

## Polling fallback

If you need a snapshot rather than callback, use the rolling-buffer accessors:

```python
client.get_ltp()       # all subscribed LTPs
client.get_quotes()    # all subscribed quotes
client.get_depth()     # all subscribed depth
```

Don't poll faster than 0.5s. For tick-sensitive logic, use the callback.

## Verbose levels

```python
api(..., verbose=False)   # silent (default), errors only
api(..., verbose=True)    # connection / auth / subscribe logs
api(..., verbose=2)       # full debug, prints every tick
```

Use `verbose=True` for development. Production should always be `False` (default) to keep the OpenAlgo `/python` host log files small.

## Heartbeat / reconnection

Server pings every 30s; SDK auto-responds with pong. On disconnect, the SDK does not auto-resubscribe - your strategy must handle the reconnect path:

```python
def ws_loop_with_reconnect():
    while not stop_event.is_set():
        try:
            client.connect()
            client.subscribe_ltp(instruments, on_data_received=on_tick)
            while not stop_event.is_set():
                time.sleep(1)
        except Exception:
            log.exception("WS loop error - reconnecting in 5s")
            time.sleep(5)
        finally:
            try: client.disconnect()
            except Exception: pass
```

`core/risk_manager.py` handles WS errors via try/except in the callback; for higher-grade resilience wrap the subscribe step as above.

## Symbol limits

Per `MAX_SYMBOLS_PER_WEBSOCKET=1000` and `MAX_WEBSOCKET_CONNECTIONS=3` in OpenAlgo - max 3000 symbols across all clients on one OpenAlgo instance. Stay well below for healthy throughput; scanners and dashboards eat into this budget.
