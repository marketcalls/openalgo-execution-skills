---
name: mode-toggle
description: How --mode backtest|live dispatches in a single-file strategy; env vs CLI precedence
---

# Mode Toggle: backtest vs live

Every generated strategy supports two modes, dispatched on a single `--mode` arg or `MODE` env var.

## Precedence

```python
parser.add_argument("--mode", choices=["backtest","live"],
                    default=os.getenv("MODE", "live"))
```

CLI arg wins. If absent, falls back to `MODE` env. If neither set, defaults to `live`.

This dual mechanism is required because:
- **Local development**: user types `python strategy.py --mode backtest` - the CLI flag works
- **OpenAlgo /python self-hosted**: the host launches the file via subprocess with no CLI args, only env vars. The user sets `MODE=backtest` in the upload form's parameters section to dry-run before going live

## Backtest mode

- Calls `run_backtest()`
- Uses `client.history()` for one-shot data fetch (no WS connection)
- Pipes `signals(df)` into `vbt.Portfolio.from_signals(...)` with `fees`, `fixed_fees`, `slippage` from the cost model
- Honors `RISK` config via VectorBT's `sl_stop`, `tp_stop`, `sl_trail` params
- Writes trades CSV and (if `quantstats` available) HTML tearsheet
- No order is ever sent to the broker

## Live mode

- Calls `run_live()`
- Connects WS, subscribes LTP/Quote/Depth as needed
- Uses `BarCloseWatcher` (eoc), tick-driven LIMIT placement (limit), or broker-side SL/SL-M (stop) - see `execution-types.md`
- Real orders go through `client.placeorder()`, `placesmartorder()`, `optionsmultiorder()`
- Whether those orders hit the real broker or OpenAlgo's sandbox engine is decided by the **OpenAlgo UI analyzer toggle** - the strategy code is unaware

## Why no separate "paper" mode

OpenAlgo platform handles live/sandbox toggling at the platform level via `/analyzer` page. The strategy code doesn't need a paper mode - just toggle in the UI before running `--mode live`. This keeps the strategy file simple and avoids drift between paper and live code paths.

## Same code, same behaviour

The same `signals(df)` function runs in both modes. Same indicator parameters, same RISK thresholds, same cost assumptions. The only difference is **how the signal is acted on** - VectorBT simulates the trade, the live runner places a real order.

This means:
- A signal that fires in backtest fires identically in live
- A SL hit in backtest mirrors a SL hit in live (modulo broker latency, which `slippage` accounts for)
- If your backtest stats look good, the live run should look statistically similar (with slippage drift reported - see `slippage-handling.md`)

## What about partial bars?

In live, `client.history()` returns the forming bar at `iloc[-1]`. Always evaluate signals on `iloc[-2]` (the just-closed bar) to avoid repaint.

In backtest, all bars are closed by definition - VectorBT operates on the full series with no forming-bar issue.

## Switching during a run

You cannot switch mid-process. To change mode, stop the strategy and restart with the new flag/env. State persists via SQLite (see `state-persistence.md`) so a stop-restart is generally safe.
