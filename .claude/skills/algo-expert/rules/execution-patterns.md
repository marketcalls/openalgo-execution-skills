---
name: execution-patterns
description: Order placement idioms - smart order, basket, split, modify, cancel, OCO, fill polling
---

# Execution Patterns

## Smart order (clean position management)

`placesmartorder` considers existing position size. Pass `position_size` = what you want the final size to be:

```python
# Flatten cleanly regardless of current size:
client.placesmartorder(
    strategy=..., symbol=..., exchange=...,
    action="SELL", price_type="MARKET", product="MIS",
    quantity=QUANTITY,           # SDK uses this as a hint; actual will be calc'd
    position_size=0,             # final size
)

# Scale up to 5 lots from current N:
client.placesmartorder(
    action="BUY", quantity=5*lot, position_size=5,
)
```

Use this for:
- Risk-manager exits (always `position_size=0`)
- Strategy exit signals (cleanest flat-out)
- Building a position in stages (incremental scale-in)

Plain `placeorder(action="SELL")` requires the caller to know the exact open quantity - a partial fill earlier means you'll over-sell. Smart order avoids this.

## Basket order (multi-symbol atomic)

```python
basket = [
    {"symbol":"BHEL","exchange":"NSE","action":"BUY","quantity":1,
     "pricetype":"MARKET","product":"MIS"},
    {"symbol":"ZOMATO","exchange":"NSE","action":"SELL","quantity":1,
     "pricetype":"MARKET","product":"MIS"},
]
response = client.basketorder(orders=basket)
# response: {status, results: [{symbol, status, orderid}, ...]}
```

Useful for:
- Pairs trading (long one symbol, short another)
- Sector rotation (rebalance across N symbols)
- Multi-asset hedging

## Split order (large quantity to chunks)

```python
client.splitorder(
    symbol="YESBANK", exchange="NSE", action="SELL",
    quantity=105, splitsize=20,        # 6 orders: 20+20+20+20+20+5
    price_type="MARKET", product="MIS",
)
```

When to use:
- Quantity > broker's per-order freeze limit (NIFTY freeze = 1800)
- Want to TWAP-spread an order to reduce market impact
- Limit per-order risk to avoid one bad fill destroying the average

## Modify / cancel

```python
client.modifyorder(
    order_id=oid,
    strategy=..., symbol=..., exchange=...,
    action="...", price_type="LIMIT", product="...",
    quantity=..., price=...,
)
# Common pattern: re-peg a LIMIT order as price drifts (atr_breakout strategy)

client.cancelorder(order_id=oid, strategy=...)
# Cancels a single order

client.cancelallorder(strategy=...)
# Cancels everything tagged with this strategy (or all strategies if blank)
```

`modifyorder` requires re-passing all order params, not just the changed ones. The SDK doesn't let you patch only price.

## OCO (one-cancels-other)

OpenAlgo doesn't have native OCO - you implement via tick polling:

```python
# Place both legs
buy_oid  = client.placeorder(action="BUY",  price_type="LIMIT", price=upper)["orderid"]
sell_oid = client.placeorder(action="SELL", price_type="LIMIT", price=lower)["orderid"]

# Poll for fill on either side, cancel the other
while True:
    for side, oid, opp in [("BUY", buy_oid, sell_oid),
                           ("SELL", sell_oid, buy_oid)]:
        status = client.orderstatus(order_id=oid, strategy=...)
        if status["data"]["order_status"] == "complete":
            client.cancelorder(order_id=opp, strategy=...)
            return side, fill_price
    time.sleep(1)
```

This is what `opening_range/strategy.py` and `atr_breakout/strategy.py` do internally.

## Fill polling

After placing an order, poll `orderstatus` until `complete`:

```python
def wait_fill(client, order_id, strategy, fallback_price, retries=10, sleep_s=0.5):
    for _ in range(retries):
        try:
            r = client.orderstatus(order_id=order_id, strategy=strategy)
            d = r.get("data", {}) if isinstance(r, dict) else {}
            if d.get("order_status") == "complete":
                avg = d.get("average_price") or d.get("price")
                if avg:
                    return float(avg)
        except Exception:
            log.exception("orderstatus failed")
        time.sleep(sleep_s)
    return fallback_price
```

Returns the actual fill price (for slippage tracking) or falls back to the decision price if the broker is sluggish.

## End-of-day cleanup

```python
# At square-off time:
client.cancelallorder(strategy=STRATEGY_NAME)   # drop pending SL / LIMIT orders
client.closeposition(strategy=STRATEGY_NAME)    # flatten everything
```

Always called in MIS strategies before 15:15 IST. NRML strategies that hold overnight skip this.

## Telegram alerts on key events

```python
client.telegram(
    username=os.getenv("TELEGRAM_USERNAME", ""),
    message=f"ENTRY {SYMBOL} @ {fill_price:.2f}",
)
```

Use sparingly - too many messages cause noise. Common: entry/exit, SL hit, portfolio cap breach.

## Rate limits

OpenAlgo proxies to brokers - each broker has its own rate limit (typically 10-200 requests/sec). For high-frequency strategies:
- Don't poll `orderstatus` faster than 1Hz per order
- Don't place orders faster than 5/sec
- Use WS callbacks for tick data, not polling
- Consider one strategy per process (so each gets its own httpx connection pool)

## Order tagging

Always pass `strategy` parameter. It tags orders in the orderbook so you can:
- Filter `cancelallorder(strategy=NAME)` to your strategy's orders only
- Group P&L per strategy when querying tradebook
- Audit which trade came from which logic

The strategy name flows through env (`STRATEGY_NAME`) so OpenAlgo's `/python` host injects it automatically.
