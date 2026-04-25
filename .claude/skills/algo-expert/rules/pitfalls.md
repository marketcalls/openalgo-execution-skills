---
name: pitfalls
description: Common production mistakes when building OpenAlgo trading strategies and how to avoid them
---

# Pitfalls (and how to avoid them)

A checklist of mistakes that bite real strategies. Most are easy to prevent if you know about them.

## 1. Using `iloc[-1]` on live history

**Pitfall**: `df["close"].iloc[-1]` reads the forming bar, which keeps repainting until the bar closes.

**Fix**: Use `iloc[-2]` (the just-closed bar) for signal evaluation.

```python
# Wrong - signal repaints every poll
if entries.iloc[-1]: place_order()

# Right - signal stable from the moment the bar closes
if entries.iloc[-2]: place_order()
```

## 2. Backtesting with zero costs

**Pitfall**: `vbt.Portfolio.from_signals(close, entries, exits)` with no fees/slippage produces fantasy PnL.

**Fix**: Always pass costs from the cost model.

```python
COSTS = cost_lookup(PRODUCT, EXCHANGE)
pf = vbt.Portfolio.from_signals(
    close, entries, exits,
    fees=COSTS.fees, fixed_fees=COSTS.fixed_fees, slippage=COSTS.slippage,
    ...
)
```

## 2b. Backtesting with `size=0.95`

**Pitfall**: `size=0.95, size_type="percent"` puts 95% of equity per trade. 10 losers in a row = ~9% drawdown - and real strategies regularly run that many losers during regime mismatch.

**Fix**: Use fixed-fractional sizing keyed off the stop loss. See `position-sizing.md`.

```python
size_pct = fixed_fractional_size(
    risk_per_trade=0.005,    # 0.5% capital risk per trade
    sl_pct=RISK.sl_pct,
    max_size=0.50,
)
# For sl=1%, size_pct = 0.50 (50% of equity, max risk = 0.5% of capital)
```

## 2c. Backtest fill at signal-bar close

**Pitfall**: Default `from_signals(close, entries, exits)` fills at the SIGNAL bar's close. But live execution evaluates at signal bar close and fills at the NEXT bar's open. Backtest stats overstate returns.

**Fix**: Pass `price=df["open"].shift(-1)` to model next-bar open fill (already in all templates).

## 3. Hardcoded exchange when self-hosted

**Pitfall**: `EXCHANGE = "NSE"` in script but uploaded with `OPENALGO_STRATEGY_EXCHANGE=MCX` → broker rejects the order.

**Fix**: Read the env var first.

```python
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
```

## 4. Placing exit orders inside the WS callback

**Pitfall**: `on_ltp(data)` calls `client.placeorder(...)` directly. Network latency blocks the WS thread, ticks queue up, more SLs miss.

**Fix**: Spawn a worker thread.

```python
def on_ltp(data):
    if should_exit:
        threading.Thread(target=place_exit, daemon=True).start()
```

`core/risk_manager.py` does this internally.

## 5. `start_date == end_date` in history()

**Pitfall**: Returns 1 candle (or empty), strategy can't compute indicators.

**Fix**: Always use a multi-day lookback.

```python
end = datetime.now().date()
start = end - timedelta(days=7)
df = client.history(symbol=..., interval="1m",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"))
```

## 6. Forgetting to unsubscribe before disconnect

**Pitfall**: `client.disconnect()` without `client.unsubscribe_*()` first leaves stale subscriptions on the server.

**Fix**: Always pair them.

```python
try:
    client.unsubscribe_ltp(instruments)
except Exception:
    pass
try:
    client.disconnect()
except Exception:
    pass
```

## 7. Polling `orderstatus` faster than 1 Hz

**Pitfall**: Hammers the broker rate limit; your other orders get rejected.

**Fix**: 0.5-1 second sleep between polls. Cap retries at 10-20.

```python
for _ in range(10):
    r = client.orderstatus(order_id=oid, strategy=...)
    if r["data"]["order_status"] == "complete":
        break
    time.sleep(0.5)
```

## 8. Hardcoding lot sizes

**Pitfall**: NIFTY lot = 75 in your code, SEBI revises to 65 mid-cycle, your order qty becomes wrong.

**Fix**: Fetch live or read from `lot-sizes.md` (kept current). Better - call `client.symbol(...)` at startup.

```python
info = client.symbol(symbol="NIFTY30DEC25FUT", exchange="NFO")
LOT_SIZE = int(info["data"]["lotsize"])
```

## 9. Auto-rolling expiry in code

**Pitfall**: Code computes `expiry = next_expiry(today)` and trades it. New strikes might not have liquidity yet, or you skip a contract you wanted to trade.

**Fix**: Hardcode `EXPIRY_DATE` per cycle and roll manually.

```python
EXPIRY_DATE = "30DEC25"     # update weekly/monthly
```

## 10. Ignoring SIGTERM in self-hosted strategies

**Pitfall**: OpenAlgo `/python` host SIGTERMs your strategy at stop time. Without a handler, you get a hard kill - state may not flush, WS subscriptions linger.

**Fix**: Trap SIGTERM and SIGINT, set a `stop_event`, drain in `finally`.

```python
stop_event = threading.Event()
def _shutdown(signum, frame):
    log.info("Signal %d - shutting down", signum)
    stop_event.set()
signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)
```

## 11. Logging to a file in /python-hosted strategy

**Pitfall**: `logging.FileHandler("strategy.log")` conflicts with the host's log rotation.

**Fix**: stdout only.

```python
logging.basicConfig(stream=sys.stdout, ...)
```

## 12. Trusting `placeorder` response without polling fill

**Pitfall**: Assuming `response.get("orderid")` means filled. It only means the order was accepted by the broker. Could still reject.

**Fix**: Poll `orderstatus` until `complete` or `rejected`.

## 13. Reusing position quantity blindly on exit

**Pitfall**: You opened 5 lots, broker partially filled 3. Plain `placeorder(action="SELL", qty=5)` over-sells.

**Fix**: Use `placesmartorder(position_size=0)` - the SDK calculates the correct flatten qty.

## 14. Ignoring NaN at indicator warmup

**Pitfall**: `ind.rsi(close, 14)` returns NaN for the first 13 bars. Signal logic on NaN gives unpredictable behavior.

**Fix**: `.fillna(False)` on bool series, or skip the warmup period in your logic.

```python
buy_raw = ind.crossover(fast, slow).fillna(False).astype(bool)
```

## 15. Overfitting in ML strategies

**Pitfall**: Train accuracy 75%, walk-forward 51%. Live performance is closer to 51%, not 75%.

**Fix**: Trust walk-forward CV. Reduce features, regularize, retrain weekly.

## 16. WS reconnection without resubscription

**Pitfall**: SDK auto-pings, but on disconnect it won't auto-resubscribe. Your strategy goes blind.

**Fix**: Wrap WS lifecycle in retry loop:

```python
while not stop_event.is_set():
    try:
        client.connect()
        client.subscribe_ltp(instruments, on_data_received=on_tick)
        while not stop_event.is_set(): time.sleep(1)
    except Exception:
        log.exception("WS error - reconnecting in 5s")
        time.sleep(5)
    finally:
        try: client.disconnect()
        except Exception: pass
```

## 17. Square-off time mismatched to product

**Pitfall**: MIS auto-flat at 15:15 IST equity, 23:25 IST MCX. Your script's `SQUARE_OFF_TIME = 15:15` causes you to flatten an MCX strategy mid-session.

**Fix**: Make square-off time exchange-aware, or read from `OPENALGO_STRATEGY_EXCHANGE`.

## 18. Multiple strategies on the same symbol

**Pitfall**: Strategy A and B both trade RELIANCE. They each track their own state but share the broker's position. A sees B's fills as foreign.

**Fix**: Use distinct `strategy=...` tags - lets you `cancelallorder(strategy=A)` without affecting B's orders. But the broker position is shared - structure carefully or use one strategy per symbol.

## 19. Backtest period overlapping training period (ML)

**Pitfall**: Train on 2024 data, backtest on 2024 data → model has memorized; results are unrealistically good.

**Fix**: Train on `[t-N, t-1]`, backtest on `[t, t+M]`. Or trust walk-forward CV from `train.py`.

## 20. Forgetting to clean up state.db on strategy delete

**Pitfall**: Delete a strategy, recreate with same name → loads stale state from previous run.

**Fix**: `/algo-host` includes a `--reset-state` flag (or just delete `strategies/<name>/state.db` manually).

## Pre-go-live checklist

Before flipping `MODE=live` for the first time:

- [ ] Backtest covers 2+ years with realistic costs and slippage
- [ ] Walk-forward (or out-of-sample) results match in-sample within reason
- [ ] All threshold values in `RISK` are tuned via backtest
- [ ] Strategy runs cleanly in OpenAlgo sandbox (analyzer mode = ON) for 1 full day
- [ ] `/algo-risk-test` confirms SL/TP/trailing fire correctly
- [ ] Telegram alerts wired (or you accept blind operation)
- [ ] State persistence works across restart (kill -TERM and verify state.db was flushed)
- [ ] If self-hosted: `/algo-host` validation passes, exchange and schedule set correctly
