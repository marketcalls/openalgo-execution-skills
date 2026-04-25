---
name: sdk-reference
description: Complete OpenAlgo Python SDK reference - all client methods with request/response shapes
---

# OpenAlgo Python SDK Reference

```python
from openalgo import api, ta

client = api(
    api_key="...",
    host="http://127.0.0.1:5000",          # REST endpoint
    ws_url="ws://127.0.0.1:8765",          # WebSocket (only needed for streaming)
    verbose=False,                         # 0=silent, 1=basic logs, 2=full debug
)
```

## Order management

### placeorder
```python
client.placeorder(
    strategy="my_strategy",
    symbol="SBIN", exchange="NSE",
    action="BUY",                          # BUY | SELL
    price_type="MARKET",                   # MARKET | LIMIT | SL | SL-M
    product="MIS",                         # MIS | CNC | NRML
    quantity=1,
    price=None,                            # required for LIMIT
    trigger_price=None,                    # required for SL / SL-M
    disclosed_quantity=0,
)
# -> {"orderid": "250408000989443", "status": "success"}
```

### placesmartorder
Considers current position size; useful for safe scaling and clean flat-out.
```python
client.placesmartorder(
    strategy="...", symbol="...", exchange="...",
    action="SELL", price_type="MARKET", product="MIS",
    quantity=1,
    position_size=0,                       # 0 = flatten regardless of current size
)
```

### modifyorder / cancelorder / cancelallorder / closeposition
```python
client.modifyorder(order_id="...", strategy="...", symbol="...", exchange="...",
                   action="...", price_type="...", product="...",
                   quantity=..., price=...)

client.cancelorder(order_id="...", strategy="...")

client.cancelallorder(strategy="...")
# -> {"status":"success", "canceled_orders":[...], "failed_cancellations":[]}

client.closeposition(strategy="...")
# -> {"message":"All Open Positions Squared Off", "status":"success"}
```

### Multi-order operations
```python
# Basket (mixed symbols)
client.basketorder(orders=[{...}, {...}])

# Split (large qty -> chunks)
client.splitorder(symbol="...", exchange="...", action="...", quantity=105,
                  splitsize=20, price_type="MARKET", product="MIS")
```

## Options orders

### optionsorder (single leg)
```python
client.optionsorder(
    strategy="...",
    underlying="NIFTY", exchange="NSE_INDEX",
    expiry_date="30DEC25",                 # DDMMMYY, no hyphens
    offset="ATM",                          # ATM | ITM1..50 | OTM1..50
    option_type="CE",                      # CE | PE
    action="BUY",
    quantity=75, pricetype="MARKET", product="NRML",
    splitsize=0,
)
# -> {orderid, symbol, underlying, underlying_ltp, status}
```

### optionsmultiorder (multi-leg, atomic)
```python
client.optionsmultiorder(
    strategy="...",
    underlying="NIFTY", exchange="NSE_INDEX",
    expiry_date="25NOV25",                 # default for all legs (override per-leg)
    legs=[
        {"offset":"OTM6","option_type":"CE","action":"BUY","quantity":75},
        {"offset":"OTM6","option_type":"PE","action":"BUY","quantity":75},
        {"offset":"OTM4","option_type":"CE","action":"SELL","quantity":75},
        {"offset":"OTM4","option_type":"PE","action":"SELL","quantity":75},
        # leg-specific override:
        # {"offset":"ITM2","option_type":"CE","action":"BUY","quantity":75,"expiry_date":"30DEC25"},
    ],
)
# -> {status, results:[{leg, orderid, symbol, action, ...}, ...]}
# BUY legs always execute before SELL legs (margin efficiency).
```

## Order info

```python
client.orderstatus(order_id="...", strategy="...")
# -> {data: {order_status, action, average_price, exchange, price, quantity, symbol, ...}}

client.openposition(strategy="...", symbol="...", exchange="...", product="MIS")
# -> {quantity: "-10", status: "success"}
```

## Market data

```python
client.quotes(symbol="RELIANCE", exchange="NSE")
# -> {data: {open, high, low, ltp, ask, bid, prev_close, volume}}

client.multiquotes(symbols=[{"symbol":"RELIANCE","exchange":"NSE"}, ...])

client.depth(symbol="SBIN", exchange="NSE")
# -> {data: {open, high, low, ltp, bids:[5x{price,qty}], asks:[5x{price,qty}], ...}}

client.history(symbol="SBIN", exchange="NSE", interval="5m",
               start_date="2025-04-01", end_date="2025-04-08",
               source="api"                # api | db (Historify DuckDB)
               )
# -> pd.DataFrame with timestamp index, columns: open, high, low, close, volume

client.intervals()
# -> {data: {months,weeks,days,hours,minutes,seconds: [...]}}
```

## Symbols & options

```python
client.symbol(symbol="NIFTY30DEC25FUT", exchange="NFO")
client.search(query="NIFTY 26000 DEC CE", exchange="NFO")
client.optionsymbol(underlying="NIFTY", exchange="NSE_INDEX",
                    expiry_date="30DEC25", offset="ATM", option_type="CE")
client.optionchain(underlying="NIFTY", exchange="NSE_INDEX",
                   expiry_date="30DEC25", strike_count=10)
client.expiry(symbol="NIFTY", exchange="NFO", instrumenttype="options")
client.syntheticfuture(underlying="NIFTY", exchange="NSE_INDEX", expiry_date="25NOV25")
client.optiongreeks(symbol="NIFTY25NOV2526000CE", exchange="NFO",
                    interest_rate=0.0,
                    underlying_symbol="NIFTY", underlying_exchange="NSE_INDEX")
client.instruments(exchange="NSE")
```

## Account

```python
client.funds()
# -> {data: {availablecash, collateral, m2mrealized, m2munrealized, utiliseddebits}}

client.margin(positions=[{symbol, exchange, action, product, pricetype, quantity}])
# -> {data: {total_margin_required, span_margin, exposure_margin}}

client.orderbook()      # all orders
client.tradebook()      # filled trades
client.positionbook()   # current positions with pnl
client.holdings()       # CNC holdings
```

## Calendar

```python
client.holidays(year=2026)
client.timings(date="2025-12-19")
```

## Analyzer (sandbox toggle)

```python
client.analyzerstatus()
# -> {data: {analyze_mode, mode, total_logs}}

client.analyzertoggle(mode=True)   # True = sandbox, False = real broker
# Note: prefer the OpenAlgo UI for this in production - the strategy
# itself shouldn't toggle modes silently.
```

## WebSocket streaming

```python
instruments = [{"exchange":"NSE","symbol":"RELIANCE"}, ...]

def on_data(data):
    # data shape varies by mode (LTP / Quote / Depth)
    print(data["data"]["ltp"])

client.connect()
client.subscribe_ltp(instruments,   on_data_received=on_data)
client.subscribe_quote(instruments, on_data_received=on_data)
client.subscribe_depth(instruments, on_data_received=on_data)

# Polling rolling buffer (alternative to callbacks):
client.get_ltp()                   # all subscribed LTPs
client.get_quotes()                # all subscribed quotes

client.unsubscribe_ltp(instruments)
client.unsubscribe_quote(instruments)
client.unsubscribe_depth(instruments)
client.disconnect()
```

WS payload shapes (callback receives a dict with `type="market_data"` and `data` nested):

```python
# LTP
{"type":"market_data","mode":1,"topic":"RELIANCE.NSE",
 "data":{"symbol":"RELIANCE","exchange":"NSE","ltp":1424.0,"timestamp":"..."}}

# Quote
{"type":"market_data","mode":2,"topic":"RELIANCE.NSE",
 "data":{"symbol","exchange","ltp","change","change_percent","volume",
         "open","high","low","close","last_trade_quantity","avg_trade_price","timestamp"}}

# Depth
{"type":"market_data","mode":3,"depth_level":5,"topic":"RELIANCE.NSE",
 "data":{"symbol","exchange","ltp","depth":{"buy":[{price,quantity,orders}*5],
                                              "sell":[{price,quantity,orders}*5]},
         "timestamp","broker_supported"}}
```

## Telegram alerts (optional)

```python
client.telegram(username="<openalgo_loginid>",
                message="NIFTY crossed 26000!")
```
