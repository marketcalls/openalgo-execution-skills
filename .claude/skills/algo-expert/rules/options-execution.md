---
name: options-execution
description: Options-only execution patterns - optionsorder / optionsmultiorder, ATM/ITM/OTM offsets, per-leg SL, expiry handling. No backtest.
---

# Options Execution

Options strategies in this pack are **execution-only**. Backtest mode exits with a clear message - options pricing depends on volatility surfaces, time decay, and OI dynamics that intraday OHLCV history doesn't capture well.

## Single-leg orders (`optionsorder`)

```python
response = client.optionsorder(
    strategy="my_atm_buy",
    underlying="NIFTY",            # base ticker
    exchange="NSE_INDEX",          # underlying's exchange
    expiry_date="30DEC25",         # DDMMMYY no hyphens
    offset="ATM",                  # ATM | ITM1..50 | OTM1..50
    option_type="CE",              # CE | PE
    action="BUY",
    quantity=75,                   # 1 lot for NIFTY (apr 2026)
    pricetype="MARKET",
    product="NRML",
    splitsize=0,                   # 0 = no split; >0 splits to chunks
)
# response: {orderid, symbol, exchange:"NFO", underlying, underlying_ltp, status}
```

OpenAlgo resolves `offset="ATM"` to the matching option symbol using current spot. `OTM2` = 2 strikes OTM, `ITM3` = 3 strikes ITM, etc.

## Multi-leg orders (`optionsmultiorder`)

Atomic multi-leg placement. BUY legs always execute first (margin efficiency).

```python
response = client.optionsmultiorder(
    strategy="iron_condor",
    underlying="NIFTY",
    exchange="NSE_INDEX",
    expiry_date="25NOV25",         # default expiry for all legs
    legs=[
        {"offset":"OTM6","option_type":"CE","action":"BUY","quantity":75},
        {"offset":"OTM6","option_type":"PE","action":"BUY","quantity":75},
        {"offset":"OTM4","option_type":"CE","action":"SELL","quantity":75},
        {"offset":"OTM4","option_type":"PE","action":"SELL","quantity":75},
    ],
)

# Per-leg expiry override (for diagonals/calendars):
legs=[
    {"offset":"ITM2","option_type":"CE","action":"BUY","quantity":75,"expiry_date":"30DEC25"},
    {"offset":"OTM2","option_type":"CE","action":"SELL","quantity":75,"expiry_date":"25NOV25"},
]
```

Response contains `results: [{leg, orderid, symbol, action, ...}, ...]`. Order in the array matches the order in `legs` (after BUY-first reordering by OpenAlgo).

## Per-leg stop loss

After a multi-leg fill, place broker-side SL orders for each leg:

```python
# Read fill prices for each leg
ce_fill = float(client.orderstatus(order_id=ce_oid, strategy=...)["data"]["average_price"])
pe_fill = float(client.orderstatus(order_id=pe_oid, strategy=...)["data"]["average_price"])

# 30% premium SL on a short leg = trigger when price climbs to 1.30x entry
sl_multiplier = 1.30
ce_sl_trigger = round(ce_fill * sl_multiplier, 2)
pe_sl_trigger = round(pe_fill * sl_multiplier, 2)
ce_sl_price   = round(ce_sl_trigger * 1.005, 2)   # SL = trigger + 0.5% buffer
pe_sl_price   = round(pe_sl_trigger * 1.005, 2)

# For BUY-back exits on short legs:
client.placeorder(
    strategy=..., symbol=ce_symbol, exchange="NFO",
    action="BUY",                     # opposite of the SELL entry
    price_type="SL", product="NRML",
    quantity=qty,
    trigger_price=str(ce_sl_trigger),
    price=str(ce_sl_price),
)
```

Pattern from `D:/openalgo-python/openalgo/examples/python/straddle_with_stops.py`.

## Greeks-aware sizing

```python
greeks = client.optiongreeks(
    symbol="NIFTY25NOV2526000CE",
    exchange="NFO",
    interest_rate=0.0,
    underlying_symbol="NIFTY",
    underlying_exchange="NSE_INDEX",
)
# greeks["greeks"]: {delta, gamma, vega, theta, rho}
# greeks["implied_volatility"]
```

Use delta to size delta-neutral straddles, vega exposure for IV-targeting trades, theta for premium-decay strategies.

## Synthetic future (delta-neutral hedging)

```python
sf = client.syntheticfuture(
    underlying="NIFTY", exchange="NSE_INDEX", expiry_date="25NOV25",
)
# sf["synthetic_future_price"] = call - put + strike   (covered parity)
```

Useful when futures are unavailable or for arbitrage between options and synthetic.

## Option chain (for live decision logic)

```python
chain = client.optionchain(
    underlying="NIFTY", exchange="NSE_INDEX",
    expiry_date="30DEC25",
    strike_count=10,            # +/- 10 strikes around ATM
)
# chain["chain"] = [{strike, ce:{ltp,bid,ask,oi,...}, pe:{...}}]
# chain["atm_strike"] = current ATM
```

Useful for IV/volume filters before entering, e.g. only sell premium when IV percentile is high.

## Why no backtest

Options pricing models (Black-Scholes, Bjerksund-Stensland) need volatility, time-to-expiry, interest rate, and dividends for accurate replay. OpenAlgo's `client.history()` returns OHLCV per option, but:
- Old strikes don't have data (they expired)
- Volume on far-OTM strikes is too thin to model fills
- Greeks change minute-to-minute - hard to backtest a delta-hedged book

This skill pack intentionally skips options backtesting. Use vectorbt or quantlib externally if you need it; this pack focuses on making real options execution reliable.

## Templates

- `short_straddle/strategy.py` - sells ATM CE + PE at scheduled time, per-leg SL at 1.3x premium, flat at 15:15
- `iron_condor/strategy.py` - 4-leg credit spread, OTM4 short body + OTM8 long wings

Both refuse `--mode backtest` with a clear error message.

## Best practices

- **Always fetch spot quote first** (`client.quotes()`) before placing options - debug logs will show you the spot ATM was anchored on
- **Never auto-roll expiry** - hardcode `EXPIRY_DATE` per cycle, change it weekly
- **Use `client.expiry()`** to validate the date is still active before placing orders
- **Square off before market close** - MIS auto-flat applies, but NRML positions held overnight margin much higher
- **Use `client.cancelallorder()`** at the end of session to clean up dangling SL orders
