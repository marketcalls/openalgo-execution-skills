---
name: algo-strategy
description: Generate a single-file dual-mode trading strategy. Asks for indicator library and execution type. The same file runs `--mode backtest` (VectorBT) and `--mode live` (OpenAlgo). Upload-ready for OpenAlgo /python self-hosted.
argument-hint: "[template] [symbol] [exchange] [interval]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Create a complete dual-mode strategy file from a template.

## Arguments

Parse `$ARGUMENTS` as: template symbol exchange interval

- `$0` = template (e.g. `ema-crossover`, `rsi`, `supertrend`, `donchian`, `macd`, `opening-range`, `atr-breakout`, `bb-squeeze`, `ml-logistic`, `ml-xgb`, `pairs-trading`, `regime-trend`, `event-driven`)
- `$1` = symbol (e.g. SBIN, RELIANCE, NIFTY). Default: template's catalog default
- `$2` = exchange (e.g. NSE, NSE_INDEX, NFO). Default: template's catalog default
- `$3` = interval (e.g. 1m, 5m, 15m, D). Default: template's default

If no arguments, ask the user which template they want. See `algo-expert/rules/strategy-catalog.md`.

## Required prompts before generation

After parsing arguments, ASK the user (do not assume defaults silently):

1. **Indicator library**: `(1) openalgo [default]  (2) talib`
   - Default openalgo. talib's user must confirm explicitly.
   - Specialty indicators (Supertrend, Donchian, Ichimoku, HMA, KAMA) always use openalgo regardless.

2. **Execution type** - default depends on template (see strategy-catalog.md):
   - `(1) end-of-candle (eoc)` - signal at bar close, MARKET on next bar (default for most)
   - `(2) real-time limit (limit)` - tick-driven LIMIT placement (default for atr-breakout)
   - `(3) stop-trigger (stop)` - broker-side SL/SL-M (default for opening-range)

   Tell the user the recommended default for their template and let them override.

## Instructions

1. Read `algo-expert/rules/unified-strategy-pattern.md`, `mode-toggle.md`, and `execution-types.md`.
2. Read the matching template at `algo-expert/rules/assets/<template>/strategy.py` as the starting point.
3. Create `strategies/<template>_<symbol>/` directory if not present.
4. Copy the template into `strategies/<template>_<symbol>/strategy.py` and:
   - Replace `SYMBOL`, `EXCHANGE`, `INTERVAL` constants with user values
   - Set `INDICATOR_LIB` to user's choice (`"openalgo"` or `"talib"`)
   - Set `EXECUTION_TYPE` to user's choice (`"eoc"`, `"limit"`, `"stop"`)
   - Update `STRATEGY_NAME` to `<template>_<symbol>` (default; can be overridden by env)
   - Adjust `PRODUCT` to match the asset class (NSE equity → MIS or CNC; NFO → NRML)
   - Adjust `QUANTITY` to a reasonable starting value (1 for equity, lot size for futures)
5. For ML templates (`ml-logistic`, `ml-xgb`), also copy `train.py` and remind the user to run it BEFORE running the strategy:
   ```
   python strategies/ml_logistic_RELIANCE/train.py
   ```
6. The file must be host-compatible per `self-hosted-strategies.md`:
   - Reads HOST_SERVER first, then OPENALGO_HOST fallback
   - Reads OPENALGO_STRATEGY_EXCHANGE for exchange
   - SIGTERM/SIGINT handlers installed
   - stdout-only logging
   - dispatcher reads env `MODE` if no `--mode` CLI arg
7. Print:
   - File location
   - Next steps:
     - Backtest: `python strategies/<name>/strategy.py --mode backtest`
     - Live: `python strategies/<name>/strategy.py --mode live`
     - Upload to /python: `/algo-host <name>` to validate and generate upload guide

## Available templates

| Template | Default symbol | Default exchange | Default interval | Default execution |
|---|---|---|---|---|
| `ema-crossover` | SBIN | NSE | 5m | eoc |
| `rsi` | RELIANCE | NSE | 15m | eoc |
| `supertrend` | NIFTY | NSE_INDEX | 5m | eoc |
| `donchian` | NIFTY | NSE_INDEX | D | eoc |
| `macd` | INFY | NSE | D | eoc |
| `opening-range` | SBIN | NSE | 5m | stop |
| `atr-breakout` | RELIANCE | NSE | 5m | limit |
| `bb-squeeze` | TCS | NSE | 15m | eoc |
| `ml-logistic` | RELIANCE | NSE | 15m | eoc |
| `ml-xgb` | RELIANCE | NSE | 15m | eoc |
| `pairs-trading` | SBIN/PNB | NSE | D | eoc (two-leg) |
| `regime-trend` | RELIANCE | NSE | D | eoc (ADX+VIX+volume gates) |
| `event-driven` | RELIANCE | NSE | D | scheduled-time |

For options templates (`short-straddle`, `iron-condor`), use `/algo-options` instead.

## Costs

The file's `COSTS = cost_lookup(PRODUCT, EXCHANGE)` auto-resolves:
- MIS + NSE → 0.0225% + Rs 20 + 5 bps slippage
- CNC + NSE → 0.111% + Rs 20 + 3 bps slippage
- NRML + NFO → 0.018% + Rs 20 + 2 bps slippage (futures)
- NRML + NFO with options → 0.098% + Rs 20 + 10 bps slippage

If the user wants different broker rates, point them to `algo-expert/rules/transaction-costs.md` to override the constants.

## Risk defaults

The template comes with reasonable RISK values (see `strategy-catalog.md`). Tell the user these are starting points, not optimal - tune via backtest before live.

## Avoid

- Do not use icons/emojis in code, logger output, or skill text
- Do not generate files in arbitrary paths - always under `strategies/<name>/`
- Do not auto-flip `MODE=live` without the user explicitly running it
