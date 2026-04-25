---
name: algo-risk-test
description: Verify a strategy's SL / TP / trailing stop / portfolio caps fire correctly. Uses OpenAlgo sandbox + synthetic price moves. Run this before going live.
argument-hint: "[strategy-file]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Smoke-test the risk manager and portfolio caps without risking real capital.

## Arguments

`$0` = path to a strategy file (e.g. `strategies/ema_crossover_SBIN/strategy.py`).

If not given, list available strategies and ask.

## Pre-flight

Tell the user:
1. **Flip OpenAlgo's analyzer toggle ON** in the UI: visit `http://127.0.0.1:5000/analyzer` and click "Sandbox Mode". This routes all orders to the sandbox engine, no broker.
2. Confirm `client.analyzerstatus()` returns `analyze_mode: True`:
   ```python
   from openalgo import api
   c = api(api_key="...", host="http://127.0.0.1:5000")
   print(c.analyzerstatus())
   # -> {data: {analyze_mode: True, mode: "analyze", ...}}
   ```
3. The strategy will run in `--mode live` but orders are simulated.

## Instructions

1. Read the strategy file to extract:
   - `SYMBOL`, `EXCHANGE`
   - `RISK` (sl_pct, tp_pct, trail_pct, time_exit_min)
   - Indicator parameters

2. Generate a sibling `risk_test.py` in the same folder. Pattern:

```python
"""
Risk verification harness for <strategy_name>.

Runs the strategy in --mode live (with OpenAlgo sandbox toggled ON).
Once a position opens, this script injects synthetic price moves via
client.placeorder() (sandbox absorbs them) to push LTP through SL, TP,
and trail thresholds, verifying the risk manager fires.

Usage:
    # In one terminal:
    python strategy.py --mode live
    # In another:
    python risk_test.py
"""
import logging, os, sys, time
from pathlib import Path
from dotenv import find_dotenv, load_dotenv

_HERE = Path(__file__).resolve().parent
for p in [_HERE, *_HERE.parents]:
    c = p / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if c.exists():
        sys.path.insert(0, str(c.parent)); break

from openalgo import api  # noqa: E402

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("risk_test")
load_dotenv(find_dotenv(usecwd=True))

# --- Read these from strategy.py ---
SYMBOL = "SBIN"
EXCHANGE = "NSE"
SL_PCT = 0.01
TP_PCT = 0.02
TRAIL_PCT = 0.008

API_KEY = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
client = api(api_key=API_KEY, host=API_HOST)


def assert_sandbox():
    s = client.analyzerstatus()
    if not s.get("data", {}).get("analyze_mode"):
        log.error("OpenAlgo is NOT in sandbox mode. Toggle it on at /analyzer first.")
        sys.exit(1)


def get_open_position():
    pb = client.positionbook()
    for r in pb.get("data", []):
        if r.get("symbol") == SYMBOL and int(float(r.get("quantity", 0) or 0)) != 0:
            return r
    return None


def main():
    assert_sandbox()
    log.info("Sandbox confirmed. Waiting for strategy to open a position...")
    while True:
        pos = get_open_position()
        if pos:
            log.info("Position detected: %s", pos)
            break
        time.sleep(5)

    entry = float(pos["average_price"])
    qty = abs(int(float(pos["quantity"])))
    log.info("Entry %.2f qty=%d", entry, qty)

    # --- Test 1: trigger the SL by placing a counter-order at SL price ---
    sl_target = entry * (1 - SL_PCT) * 0.99       # push 1% past trigger
    log.info("Test SL: pushing LTP to %.2f", sl_target)
    # In sandbox, the trade above is simulated. The risk manager should detect
    # the LTP move (via WS) and fire placesmartorder(position_size=0) to flatten.
    # Real ticks come from broker, but in sandbox they come from your placeorder
    # calls and OpenAlgo's simulator. Verify by polling positionbook.

    time.sleep(10)
    pos_after = get_open_position()
    if pos_after is None:
        log.info("PASS: SL triggered, position flattened.")
    else:
        log.error("FAIL: position still open after SL move.")

    # --- Test 2: similarly for TP and TRAIL ---
    # ... (extend as needed)


if __name__ == "__main__":
    main()
```

3. Tell the user how to run:
   ```
   # Terminal 1: run the strategy in live mode (OpenAlgo sandbox active)
   python strategies/ema_crossover_SBIN/strategy.py --mode live

   # Terminal 2: run the risk verifier
   python strategies/ema_crossover_SBIN/risk_test.py
   ```

4. Note that OpenAlgo's sandbox doesn't accept arbitrary "set LTP" commands. Real verification of the risk manager often needs:
   - Letting the strategy run during market hours (sandbox uses real LTP)
   - Watching for the signal to fire and then timing the test to coincide with a real adverse price move

   Or: temporarily lower SL_PCT to 0.0001 (1 bp) so any tick triggers the SL - confirms the WS feed and exit path work.

## Manual checks

If automated synthetic injection isn't possible (sandbox limitations), the user should:
- Run the strategy in sandbox during market hours
- Confirm WS connects and ticks flow (set `verbose=True` temporarily)
- Confirm a real adverse move fires `placesmartorder(position_size=0)` (check `tradebook()` for the closing trade)
- Confirm `core/risk_manager.py`'s `_check_exits()` log line appears with `reason=SL_PCT`/`TP_PCT`/`TRAIL`

## What to verify

| Check | How |
|---|---|
| WS connects, LTP arrives | log shows `subscribe_ltp` success and tick callbacks fire |
| SL triggers exit | log: `EXIT trigger ... reason=SL_PCT (...)` |
| TP triggers exit | log: `EXIT trigger ... reason=TP_PCT (...)` |
| Trailing stop locks profit | log: `EXIT trigger ... reason=TRAIL (...)` |
| Time exit fires | log: `EXIT trigger ... reason=TIME_EXIT (...)` |
| Exit places `placesmartorder(position_size=0)` | log: `Exit order placed: ...` |
| Exit fill price recorded for slippage | end-of-run report shows non-zero fills |
| Portfolio cap fires | run `/algo-portfolio`, force a strategy to fake P&L below cap, verify SIGTERM propagation |

## Avoid

- Do not run risk tests with `analyze_mode: False` - real broker orders WILL fire
- Do not use icons/emojis
