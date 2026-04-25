---
name: algo-options
description: Generate options-only execution strategies (short straddle, iron condor). Backtest mode is intentionally disabled. Live mode uses optionsmultiorder + per-leg SL.
argument-hint: "[template] [underlying] [expiry-date]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Create an options execution strategy.

## Arguments

Parse `$ARGUMENTS` as: template underlying expiry-date

- `$0` = template (`short-straddle`, `iron-condor`)
- `$1` = underlying (e.g. NIFTY, BANKNIFTY). Default: NIFTY
- `$2` = expiry date in DDMMMYY format (e.g. `30DEC25`). REQUIRED - no auto-roll. Ask if not given.

If no arguments, ask the user which template.

## Instructions

1. Read `algo-expert/rules/options-execution.md`, `unified-strategy-pattern.md`, and `self-hosted-strategies.md`.
2. Confirm with the user that **backtest mode is disabled** for options strategies (the file will exit with a clear message if `--mode backtest` is passed). They use OpenAlgo's UI analyzer toggle for sandbox testing instead.
3. Ask the user:
   - **Lots**: how many lots (default 1)
   - **OTM offsets** (iron-condor only): OTM_NEAR for short body (default 4), OTM_FAR for long wings (default 8)
   - **Entry time** (IST, e.g. `09:20`)
   - **Exit time** (IST, e.g. `15:15`)
   - **SL multiplier** (short_straddle, e.g. `1.30` for 30% premium SL)
4. Read the matching template at `algo-expert/rules/assets/<template>/strategy.py`.
5. Create `strategies/<template>_<underlying>/` and copy + customize.
6. Set:
   - `UNDERLYING`, `UNDERLYING_EXCH`, `EXPIRY_DATE`
   - `LOTS`, `LOT_SIZE` (NIFTY=65, BANKNIFTY=30, FINNIFTY=60 per Apr 2026 SEBI - see `lot-sizes.md`)
   - `ENTRY_TIME`, `EXIT_TIME` as `dtime(H, M)`
   - `SL_MULTIPLIER` for short straddle
   - `OTM_NEAR`, `OTM_FAR` for iron condor
7. Tell the user:
   - To upload to `/python`: requires `apscheduler` and `pytz` packages installed (covered by setup)
   - Always re-confirm `EXPIRY_DATE` before each cycle - the file does NOT auto-roll
   - Set `MODE=live` in upload form parameters; OpenAlgo UI's analyzer toggle decides sandbox vs real
   - Strategy holds NRML overnight if not flat at EXIT_TIME - cancelallorder + closeposition is called automatically

## Templates

| Template | Description |
|---|---|
| `short-straddle` | Sell ATM CE + ATM PE at scheduled time, per-leg broker SL at SL_MULTIPLIER * premium, flat at EXIT_TIME |
| `iron-condor` | OTM_NEAR short CE/PE + OTM_FAR long CE/PE wings, monitor and flat at EXIT_TIME |

## Backtest mode is off

The generated file checks for `--mode backtest` and exits:
```
Options backtesting is not supported in this skill pack.
Options pricing depends on volatility surfaces, time decay, and OI dynamics
that intraday OHLCV backtests don't capture well.
Use --mode live (with OpenAlgo's UI analyzer toggle for sandbox).
```

If the user wants to dry-run, they should:
1. Flip OpenAlgo's analyzer toggle ON (in `/analyzer` UI)
2. Run with `--mode live` - orders go to the sandbox, not the broker

## Avoid

- Do not use icons/emojis
- Do not auto-roll expiry date (`EXPIRY_DATE = "30DEC25"` is hardcoded; user must update weekly)
- Do not implement options backtesting - explicitly out of scope
- Do not skip the SL placement - per-leg SL is critical for short premium strategies
