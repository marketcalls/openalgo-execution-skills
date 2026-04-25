---
name: symbol-format
description: OpenAlgo standardized symbol format - equity, futures, options, indices
---

# Symbol Format

OpenAlgo standardizes symbols across 24+ brokers - the strategy code uses the same format everywhere; the platform maps to broker-specific symbols internally.

## Equity
Just the base symbol:
- `INFY`, `SBIN`, `RELIANCE`, `TATAMOTORS`

## Futures
Format: `[BaseSymbol][ExpiryDate]FUT`
- `BANKNIFTY24APR24FUT`
- `NIFTY30DEC25FUT`
- `CRUDEOILM20MAY24FUT` (MCX)
- `USDINR10MAY24FUT` (CDS)

## Options
Format: `[BaseSymbol][ExpiryDate][StrikePrice][CE/PE]`
- `NIFTY28MAR2420800CE`
- `VEDL25APR24292.5CE` (decimal strike OK)
- `USDINR19APR2482CE`

Expiry date in symbol = `DDMMMYY` with no hyphens. When passing to `optionsorder()` / `optionsmultiorder()` / `optionchain()`, the format is identical: `30DEC25` (not `30-DEC-25`).

## Common NSE indices (`exchange="NSE_INDEX"`)
`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `NIFTYNXT50`, `INDIAVIX`,
`NIFTY100`, `NIFTY200`, `NIFTY500`, `NIFTYAUTO`, `NIFTYIT`, `NIFTYBANK`,
`NIFTYPHARMA`, `NIFTYFMCG`, `NIFTYMETAL`, `NIFTYREALTY`, `NIFTYENERGY`,
`NIFTYMIDCAP100`, `NIFTYMIDCAP150`, `NIFTYSMLCAP100`, etc.

## Common BSE indices (`exchange="BSE_INDEX"`)
`SENSEX`, `BANKEX`, `SENSEX50`, `BSE100`, `BSE200`, `BSE500`, `BSEMIDCAP`,
`BSESMALLCAP`, `BSEAUTO`, `BSEIT`, `BSEHEALTHCARE`, etc.

## Tips
- Use `client.search(query="NIFTY 26000 DEC CE", exchange="NFO")` to disambiguate option symbols
- Use `client.symbol(symbol=..., exchange=...)` to fetch metadata (lot size, tick size, expiry)
- Use `client.optionsymbol(...)` if you have underlying + offset (ATM/ITM/OTM) but not the explicit symbol - returns the matching option symbol
- Lot sizes for current month - see `lot-sizes.md`
