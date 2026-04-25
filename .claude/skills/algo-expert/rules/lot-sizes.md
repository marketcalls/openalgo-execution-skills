---
name: lot-sizes
description: SEBI revised lot sizes for index and stock F&O (effective Apr/May/Jun 2026)
---

# F&O Lot Sizes (SEBI Revised, Apr-Jun 2026)

Per `D:/openalgo-python/openalgo/docs/prompt/LotSize.md`. These are the lot sizes used for `optionsorder()` and `optionsmultiorder()` quantity arithmetic.

## Index F&O lot sizes
| Index | Lot |
|---|---|
| `NIFTY` | 65 |
| `BANKNIFTY` | 30 |
| `FINNIFTY` | 60 |
| `MIDCPNIFTY` | 120 |
| `NIFTYNXT50` | 25 |

## How to read lot sizes programmatically

The lot size for any F&O symbol is exposed by `client.symbol()`:

```python
info = client.symbol(symbol="NIFTY30DEC25FUT", exchange="NFO")
lot = int(info["data"]["lotsize"])
```

`client.optionchain()` and `client.optionsymbol()` also include `lotsize` per strike.

## Quantity calculation

Always pass quantity as `lots * lot_size`:

```python
LOTS = 1
LOT_SIZE = 65  # NIFTY
quantity = LOTS * LOT_SIZE
```

For stock F&O, fetch the lot size at strategy start so it stays current with monthly expiry rolls (SEBI may revise mid-cycle).

## Stock F&O lot sizes (sample - keep this list short, fetch live for full accuracy)

| Symbol | Lot |
|---|---|
| `RELIANCE` | 500 |
| `SBIN` | 750 |
| `INFY` | 400 |
| `HDFCBANK` | 550 |
| `ICICIBANK` | 700 |
| `TCS` | 175 |
| `BAJFINANCE` | 750 |
| `MARUTI` | 50 |
| `LT` | 175 |
| `BHARTIARTL` | 475 |

For the full table see `D:/openalgo-python/openalgo/docs/prompt/LotSize.md`. Lot sizes can change every contract cycle - prefer `client.symbol()` over hardcoding for production.

## Freeze quantity

Each contract has a `freeze_qty` (max single-order quantity) returned by `client.symbol()`:
- NIFTY = 1800
- FINNIFTY = 1200
- BANKNIFTY = 900

If your order qty exceeds freeze, use `splitsize` parameter on `placeorder()` / `optionsorder()` to chunk into multiple orders.
