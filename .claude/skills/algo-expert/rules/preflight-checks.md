---
name: preflight-checks
description: Startup verification - broker auth, funds, holiday, exchange-env consistency. Idempotency via positionbook and orderbook.
---

# Preflight Checks

Every live strategy runs `run_preflight()` before placing any order. Catches three classes of misconfiguration that would otherwise silently fail or error in confusing ways:

1. **Broker session not authenticated** - `funds()` returns auth error
2. **Holiday today** - `holidays()` shows exchange closed
3. **Hardcoded exchange mismatch** - script says `exchange="NSE"` but host gates on MCX

## Usage

```python
from core.preflight import run_preflight, find_existing_open_position, find_pending_orders

try:
    run_preflight(client,
                  symbol=SYMBOL, exchange=EXCHANGE,
                  min_cash=0,                              # Rs minimum
                  expected_exchange_env=EXCHANGE,
                  fail_on_holiday=True)
except Exception as e:
    log.error("Preflight failed: %s - aborting", e)
    return
```

Raises `PreflightError` on hard failure. The strategy's `run_live` aborts cleanly without placing any orders.

## What's checked

| Check | What's verified |
|---|---|
| Broker auth | `funds()` returns `{status:success, data:{availablecash}}` |
| Min cash | `availablecash >= min_cash` (skipped if min_cash=0) |
| Holiday | `holidays(year)` doesn't list today as fully closed for `exchange` |
| Symbol resolves | `symbol(symbol, exchange)` returns valid lot_size and tick_size |
| Env consistency | `OPENALGO_STRATEGY_EXCHANGE` env matches `expected_exchange_env` (warn only) |

Returns a dict with `available_cash`, `lot_size`, `tick_size`, `holiday_check` for the caller to use.

## SPECIAL_SESSION handling

If today has a partial session (Muhurat trading, MCX evening on NSE holiday), preflight logs a notice but doesn't fail:

```
[INFO] Preflight: MCX has SPECIAL_SESSION/partial today: Diwali Muhurat
```

The strategy can still trade in the open window. The OpenAlgo `/python` host's calendar gating handles the time intersection.

## Idempotency (separate from preflight)

`find_existing_open_position()` and `find_pending_orders()` are used inside the bar-close handler to prevent duplicate orders on restart:

```python
if find_existing_open_position(client, SYMBOL, EXCHANGE) is not None:
    log.warning("Broker has open position - skipping ENTRY")
    return
```

Combined with `state_db.signal_already_acted(strategy, bar_ts)` (same-bar idempotency), this prevents:
- Duplicate entries when the strategy restarts mid-bar
- Re-entry when the broker still has a position from a previous run that wasn't reflected in local state

## Failure modes preflight catches

| Failure | What user sees today | What preflight does |
|---|---|---|
| Broker session expired | `placeorder` fails with cryptic error | Aborts at startup with "broker session not authenticated" |
| Trading on a holiday | Orders silently rejected | Aborts with "Exchange NSE closed today (2026-01-26): Republic Day" |
| Insufficient cash | Order rejected at broker | Aborts with explicit cash-required message |
| MCX strategy gated as NSE | "Market closed" rejection on every order | Logs warning at startup; user fixes upload form |
| Symbol typo (SBIN1 vs SBIN) | Order rejected | Warning at startup; strategy still tries |

## When to skip preflight

Backtest mode skips it (no broker connection needed).

For sandbox testing where holidays don't matter, set `fail_on_holiday=False`.

For very capital-flexible strategies that can wait, set `min_cash=0`.

## Order flow verification

After preflight passes and entry signal fires, also check:

```python
# Idempotency check #1: in-memory state
if state_db.signal_already_acted(STRATEGY_NAME, bar_ts):
    return

# Idempotency check #2: broker state
if find_existing_open_position(client, SYMBOL, EXCHANGE) is not None:
    log.warning("Broker has open pos - skip ENTRY")
    state_db.mark_signal_acted(STRATEGY_NAME, bar_ts)
    return
```

Together: even if state.db is corrupted, the broker check stops a duplicate. Even if the broker check is slow, the in-memory check catches it. Both fire on every entry attempt.
