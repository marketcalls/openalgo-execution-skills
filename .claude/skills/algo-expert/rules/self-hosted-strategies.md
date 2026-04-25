---
name: self-hosted-strategies
description: How to make strategies upload-ready for OpenAlgo's /python self-hosted strategy host - env vars, exchange-aware calendar, SIGTERM safety
---

# Self-Hosted Strategies (`/python`)

OpenAlgo has a built-in `/python` page (`http://localhost:5000/python`) that hosts and schedules user-uploaded Python strategies as managed subprocesses. Every strategy template in this pack is generated **upload-ready**.

Reference: `D:/openalgo-python/openalgo/strategies/README.md`.

## What the host does

1. **Upload UI** - drag-and-drop a `.py` file, set parameters, pick exchange, set schedule
2. **Process isolation** - each strategy runs in its own subprocess with `os.environ.copy()` plus injected env vars
3. **Exchange-aware calendar** - strategies are gated by their assigned exchange's holiday calendar
4. **Schedule** - cron-based daily start/stop in IST, can be overridden per day
5. **SIGTERM lifecycle** - host signals SIGTERM to stop a strategy gracefully
6. **Logs** - strategy stdout/stderr goes to `logs/strategies/<name>_<timestamp>.log`

## Env vars injected by the host

The platform sets these on each subprocess:

| Var | Description |
|---|---|
| `STRATEGY_ID` | Unique ID assigned at upload |
| `STRATEGY_NAME` | Name from upload form |
| `OPENALGO_STRATEGY_EXCHANGE` | Exchange picked at upload (NSE/BSE/NFO/BFO/MCX/BCD/CDS/CRYPTO) |
| `OPENALGO_API_KEY` | Decrypted user API key |
| `OPENALGO_HOST` | setdefault to `http://127.0.0.1:5000` (only if not in `.env`) |

## Env vars inherited from OpenAlgo's `.env`

Strategies launch via `os.environ.copy()`, so they see everything in OpenAlgo's `.env`:

| Var | Description | Recommended |
|---|---|---|
| `HOST_SERVER` | REST host (canonical name in OpenAlgo `.env`) | **prefer this** |
| `WEBSOCKET_URL` | Full WS URL | **prefer this** |
| `WEBSOCKET_HOST` | Just the host | fallback |
| `WEBSOCKET_PORT` | Just the port | fallback |
| `FLASK_HOST_IP`, `FLASK_PORT` | also exposed | rarely needed |

## Canonical env resolution (every template uses this)

```python
import os
from dotenv import find_dotenv, load_dotenv

# load .env from project root (when running locally)
load_dotenv(find_dotenv(usecwd=True))

API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}"
)
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
```

Priority: HOST_SERVER (canonical) > OPENALGO_HOST (host-injected fallback) > literal default.

For the exchange: OPENALGO_STRATEGY_EXCHANGE wins when uploaded; otherwise local EXCHANGE env; otherwise NSE.

> **Why the exchange matters**: the host gates the strategy on the exchange's calendar. If your script hardcodes `exchange="NSE"` but the host gates on MCX, the broker will reject the order ("market closed"). Reading `OPENALGO_STRATEGY_EXCHANGE` keeps the host calendar and your orders aligned.

## SIGTERM handling

The host sends SIGTERM when stopping a strategy. The script must trap it and exit cleanly:

```python
import signal, threading

stop_event = threading.Event()

def _shutdown(signum, frame):
    log.info("Signal %d received - shutting down gracefully", signum)
    stop_event.set()

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

# In your loops:
while not stop_event.is_set():
    ...
    stop_event.wait(POLL_INTERVAL_SEC)   # interruptible sleep
```

In the `finally` block:
```python
finally:
    risk_mgr.stop()              # unsubscribe WS
    try: client.disconnect()     # close WS connection
    except Exception: pass
    log.info("Shutdown complete")
```

If the script doesn't exit within 15 seconds of SIGTERM, the host escalates to SIGKILL - state may be lost.

## stdout-only logging

The host captures stdout/stderr per process. Log to stdout, never to a file (would conflict with the host's log rotation):

```python
import logging, sys
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,                    # critical
)
```

## Exchange-aware scheduling

When you upload, you pick:
- **Exchange** (drives calendar)
- **Start time** (e.g. 09:15 IST)
- **Stop time** (optional, e.g. 15:30 IST)
- **Days** (Mon-Sun checkboxes)

The host then computes the **intersection** of:
1. Your `start..stop` schedule
2. The exchange's session for that date (from market calendar DB)

So an MCX strategy on a partial-holiday like 14-Apr-2026 (NSE/BSE closed, MCX open 17:00-23:55) only fires 17:00-23:55 even if your schedule was 09:15-23:55.

| Exchange | Calendar 14-Apr-2026 | Strategy fires |
|---|---|---|
| NSE / BSE / NFO / BFO | closed all day | not fired |
| MCX | open 17:00-23:55 | 17:00-23:55 only |
| CRYPTO | 24/7 | 09:15-23:55 (your schedule) |

For 8-Nov-2026 (Sunday Diwali Muhurat with SPECIAL_SESSION 18:00-19:15):

| Exchange | Calendar | Strategy fires |
|---|---|---|
| NSE / BSE / NFO / BFO | SPECIAL_SESSION 18:00-19:15 | 18:00-19:15 (overrides Sunday weekend reject) |
| MCX | SPECIAL_SESSION 18:00-00:15 | full window |
| CRYPTO | 24/7 | unaffected |

## Per-strategy parameters

The upload form has a "parameters" section. Each row becomes an environment variable on the strategy subprocess:

| Form key | Form value | Available as |
|---|---|---|
| `SYMBOL` | `RELIANCE` | `os.getenv("SYMBOL")` |
| `INTERVAL` | `5m` | `os.getenv("INTERVAL")` |
| `MODE` | `live` (or `backtest` for dry-run) | `os.getenv("MODE")` |
| `LOG_LEVEL` | `DEBUG` | `os.getenv("LOG_LEVEL")` |

Make config-block constants in the strategy override-able:

```python
SYMBOL = os.getenv("SYMBOL", "SBIN")          # default if not in form
INTERVAL = os.getenv("INTERVAL", "5m")
```

## Backtest dry-run on the host

Set `MODE=backtest` in the parameters when uploading. The script runs once (backtest is finite), exits, and the host marks it stopped. Useful for one-off validation before flipping `MODE=live` and scheduling the run.

## What `/algo-host` checks (validation skill)

Run `/algo-host <strategy-name>` to validate before upload:
- SIGTERM and SIGINT handlers installed
- Reads `HOST_SERVER` first, then `OPENALGO_HOST`
- Reads `OPENALGO_STRATEGY_EXCHANGE` for exchange (not hardcoded)
- Logs to stdout (no file handlers)
- Has `if __name__ == "__main__"` entry
- No hardcoded local paths (e.g. `/home/user/...` would break on host)
- Has graceful WS unsubscribe / disconnect in `finally`

Generates `strategies/<name>/HOST_UPLOAD.md` with the exact form values to enter.

## Migration of existing strategies

If you have older strategies that don't read `OPENALGO_STRATEGY_EXCHANGE`:
1. Edit the script: `EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", "NSE")`
2. Re-upload (or use the host's edit feature, which keeps STRATEGY_ID stable)
3. Optionally re-pick exchange in the schedule UI for legacy strategies

The host auto-defaults legacy strategies to `exchange="NSE"` so nothing breaks immediately - but MCX/CRYPTO strategies need a one-time edit.
