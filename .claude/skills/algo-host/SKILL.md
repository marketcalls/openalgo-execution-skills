---
name: algo-host
description: Validate a generated strategy and produce an upload guide for OpenAlgo's /python self-hosted strategy page. Confirms env-var reads, SIGTERM, stdout-only logging.
argument-hint: "[strategy-name|strategy-file]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Prepare a strategy for upload to OpenAlgo's `/python` strategy host.

## Arguments

- `$0` = strategy name (e.g. `ema_crossover_SBIN`) OR full path to a `.py` file
- Special: `--list` lists deployable strategies in `strategies/` and their validation status

If `$0` is a folder name, the file is `strategies/<name>/strategy.py`. If it's already a path, use as-is.

## Instructions

1. Read `algo-expert/rules/self-hosted-strategies.md`.
2. Read the strategy file.
3. **Validate** these criteria (each can be inferred via grep):

| Check | What to look for |
|---|---|
| SIGTERM handler | `signal.signal(signal.SIGTERM, ...)` present |
| SIGINT handler | `signal.signal(signal.SIGINT, ...)` present |
| stop_event used | `threading.Event()` and `stop_event.set()` |
| HOST_SERVER priority | `os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST",` pattern |
| Reads STRATEGY_EXCHANGE | `os.getenv("OPENALGO_STRATEGY_EXCHANGE",` pattern |
| stdout logging | `stream=sys.stdout` in `basicConfig`; no `FileHandler` calls |
| Strategy entry guard | `if __name__ == "__main__":` |
| Mode dispatcher | `--mode` argparse with `default=os.getenv("MODE", "live")` |
| No hardcoded local paths | `/home/`, `C:\\Users`, `/Users/` not present |
| Env tag for STRATEGY_NAME | `os.getenv("STRATEGY_NAME",` pattern |

4. Print a validation report:
   ```
   Validation: strategies/ema_crossover_SBIN/strategy.py
     [PASS]  SIGTERM handler installed (line 327)
     [PASS]  HOST_SERVER priority correct
     [PASS]  Reads OPENALGO_STRATEGY_EXCHANGE
     [PASS]  stdout-only logging
     [PASS]  Mode dispatcher honors env MODE
     [WARN]  No state.db persistence detected (optional)
     [PASS]  No hardcoded local paths
     ALL REQUIRED CHECKS PASSED
   ```

5. **Generate `HOST_UPLOAD.md`** in the same folder with the exact form values to enter at `http://localhost:5000/python`:

```markdown
# Upload Guide: <strategy_name>

## On http://localhost:5000/python

### Step 1: Click "Add Strategy"

| Form field | Value |
|---|---|
| Name | <strategy_name> |
| File | <full path to strategy.py> |
| Exchange | <SYMBOL's exchange, e.g. NSE> |

### Step 2: Parameters (key=value, one per row)

| Key | Value | Purpose |
|---|---|---|
| MODE | live | live execution (sandbox/real per UI analyzer toggle) |
| SYMBOL | <SYMBOL> | trading symbol |
| INTERVAL | <INTERVAL> | bar interval |
| LOG_LEVEL | INFO | logging verbosity |

### Step 3: Schedule

| Field | Value |
|---|---|
| Start time | 09:15 IST |
| Stop time | 15:30 IST (or leave empty for indefinite) |
| Days | Mon, Tue, Wed, Thu, Fri (weekdays for NSE) |

For MCX strategies use `EXCHANGE=MCX` and start time `17:00`.
For CRYPTO use `EXCHANGE=CRYPTO` and check all 7 days.

### Step 4: Click Upload, then Start

The host launches the strategy in a subprocess. Logs go to `openalgo/logs/strategies/<id>_<timestamp>.log`.

To stop the strategy gracefully, click Stop in the UI - the host sends SIGTERM and waits up to 15s for clean shutdown.

## Notes

- Live vs sandbox is decided by OpenAlgo UI's analyzer toggle, not by this strategy
- Re-upload anytime; the host preserves STRATEGY_ID
- The exchange you pick drives the calendar gating; intersect with start-stop
```

6. If `--list`, list `strategies/` subfolders, validate each, print a summary table.

## When validation FAILs

Print the specific lines that need fixing:

```
[FAIL] HOST_SERVER priority: line 38 reads OPENALGO_HOST first.
       Change:
           API_HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
       To:
           API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
```

Offer to make the fix automatically (use Edit tool).

## Reset state

If the user asks to reset a strategy's persistent state before re-upload, suggest:
```bash
rm strategies/<name>/state.db
```

Or, if no state.db exists, no action needed.

## Avoid

- Do not auto-upload via API - the `/python` page is a UI form, not an API endpoint
- Do not modify the strategy's logic, only the host-compatibility plumbing
- Do not use icons/emojis
