---
name: logging-and-alerts
description: stdlib logging configuration (no icons), Telegram alerts via OpenAlgo, drift report
---

# Logging & Alerts

## Logging

Use stdlib `logging`. No icons, no emojis (per user's global preference).

```python
import logging, os, sys
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(STRATEGY_NAME)
```

`stream=sys.stdout` is required for OpenAlgo `/python` host capture.

### Log levels

| Level | Use for |
|---|---|
| DEBUG | Tick data, internal state at every poll |
| INFO | Strategy lifecycle, signal events, fills, exits |
| WARNING | Recoverable issues - failed orderstatus poll, missing data |
| ERROR | Failed orders, broker disconnects (caught) |
| EXCEPTION | Unhandled exceptions (use `log.exception()` for traceback) |

Default level INFO is right for production. Use DEBUG locally when debugging signals.

### Common log lines

```python
log.info("ENTRY %s @ %.2f", df.index[-2], ltp)
log.info("EXIT signal %s @ %.2f", df.index[-2], ltp)
log.info("Risk manager armed: %s qty=%d entry=%.2f", side, qty, entry)
log.warning("Strategy %s did not stop in 15s, sending SIGKILL", name)
log.error("Could not confirm fills - manual check required")

try:
    client.placeorder(...)
except Exception:
    log.exception("Order placement failed")    # auto-captures traceback
```

`log.exception()` is preferred over `log.error()` + `traceback.print_exc()` - keeps the centralized logging path.

### Log inspection

For `/python`-hosted strategies, logs land in `logs/strategies/`:
```
openalgo/logs/strategies/
├── ema_sbin_2026-04-25_09-15-00.log
├── ema_sbin_2026-04-25_15-30-00.log
└── ...
```

Local strategies log to stdout - tee to a file if needed:
```bash
python strategies/ema_sbin/strategy.py --mode live 2>&1 | tee strategies/ema_sbin/run.log
```

## Telegram alerts

OpenAlgo provides `client.telegram()` which posts to the user's registered Telegram bot. Set `TELEGRAM_USERNAME` in `.env`:

```python
import os
TELEGRAM_USERNAME = os.getenv("TELEGRAM_USERNAME", "")

def alert(msg, level="INFO"):
    log.info(msg) if level == "INFO" else log.warning(msg)
    if TELEGRAM_USERNAME:
        try:
            client.telegram(username=TELEGRAM_USERNAME, message=msg)
        except Exception:
            log.exception("telegram alert failed - continuing")
```

### Don't spam Telegram

Reserve Telegram for high-signal events:

| Use Telegram for | Don't use Telegram for |
|---|---|
| Strategy entry / exit | Heartbeats |
| SL / TP / trailing fired | Each tick |
| Portfolio cap breach | Indicator values |
| Strategy crashed / restarted | Polling status |
| Daily PnL summary | Order placement attempts (too verbose) |

A typical day = 5-15 Telegram messages per strategy. More than 50 = noise that gets ignored.

### Sample message formats

```python
alert(f"ENTRY {SYMBOL} @ Rs {fill:.2f} qty={qty} (signal: {signal_reason})")
alert(f"EXIT {SYMBOL} @ Rs {fill:.2f} reason={reason} pnl={pnl:+.2f} (Rs {pnl_abs:+,.0f})")
alert(f"DAILY: {trades} trades, win_rate={wr:.1%}, total_pnl=Rs {pnl:+,.0f}")
alert(f"PORTFOLIO HALT: {kill_reason}", level="WARNING")
```

## Slippage drift report

`SlippageTracker.report()` (in `core/cost_model.py`) prints at end of session:

```
Slippage Report:
  Fills:               14
  Assumed (per side):  0.0500%
  Measured (avg):      0.0823%
  Ratio measured/assumed: 1.65x  [OK]
  Worst slip: 0.2540% (BUY @ decided 1567.50, filled 1571.48)
```

Logged at the end of `run_live()` in every template:
```python
finally:
    log.info("\n%s", slip.report())
```

If the ratio exceeds 2x, the report flags `[DRIFT - measured >2x assumed]` - re-tune your slippage assumption.

## Audit trail

Every order placed should log:
1. Decision time + price
2. Order ID returned by `placeorder`
3. Fill confirmation (poll `orderstatus` → log average_price)
4. Slippage measurement

Together with OpenAlgo's `tradebook()` and `orderbook()`, this gives a complete audit trail without needing your own DB.

## Don't log secrets

```python
# Don't:
log.info("Connecting with API key %s", API_KEY)

# Do:
log.info("Connecting to OpenAlgo at %s", API_HOST)
```

OpenAlgo's logger has a SensitiveDataFilter for its own logs but your strategy's stdout doesn't - be careful what you print.
