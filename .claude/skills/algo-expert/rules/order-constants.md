---
name: order-constants
description: OpenAlgo order constants - exchanges, products, price types, actions
---

# Order Constants

## Exchange codes
| Code | Description |
|---|---|
| `NSE` | NSE Equity |
| `BSE` | BSE Equity |
| `NFO` | NSE Futures & Options |
| `BFO` | BSE Futures & Options |
| `CDS` | NSE Currency Derivatives |
| `BCD` | BSE Currency Derivatives |
| `MCX` | Multi Commodity Exchange |
| `NCDEX` | National Commodity & Derivatives Exchange |
| `NSE_INDEX` | NSE indices (e.g. NIFTY, BANKNIFTY) |
| `BSE_INDEX` | BSE indices (e.g. SENSEX, BANKEX) |

## Product type
| Code | Use for |
|---|---|
| `CNC` | Cash and Carry (delivery equity, hold overnight) |
| `MIS` | Margin Intraday Square-off (auto-flat at 15:15 IST equity / 23:25 MCX) |
| `NRML` | Normal carry (futures, options) |

## Price type
| Code | Behaviour |
|---|---|
| `MARKET` | Execute at best available price immediately |
| `LIMIT` | Execute only at `price` or better. Pass `price` parameter |
| `SL` | Stop-loss limit. Pass `trigger_price` and `price`. When LTP hits trigger, a LIMIT order at `price` is placed |
| `SL-M` | Stop-loss market. Pass `trigger_price`. When LTP hits trigger, a MARKET order is placed |

## Action
| Code | |
|---|---|
| `BUY` | Long entry / short exit |
| `SELL` | Short entry / long exit |

## Common pairings

| Strategy | Exchange | Product | Price type | Notes |
|---|---|---|---|---|
| Intraday equity | NSE/BSE | MIS | MARKET / LIMIT | auto-flat 15:15 IST |
| Delivery equity | NSE/BSE | CNC | MARKET / LIMIT | settles T+1 |
| Index futures | NFO | NRML / MIS | MARKET / LIMIT | NRML to hold; MIS for intraday only |
| Index options | NFO | NRML | MARKET / LIMIT | rarely MIS |
| ORB / breakout entry | NSE | MIS | SL-M | broker-side trigger |
| Per-leg options SL | NFO | NRML | SL | trigger_price + price (limit buffer) |
