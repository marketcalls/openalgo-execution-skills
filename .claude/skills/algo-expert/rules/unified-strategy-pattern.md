---
name: unified-strategy-pattern
description: The canonical single-file dual-mode strategy template - imports, config blocks, dispatcher
---

# Unified Strategy Pattern

Every `/algo-strategy` invocation produces ONE Python file with this structure. The file runs:
- Locally as `python strategy.py --mode backtest|live`
- On OpenAlgo `/python` host with no CLI args (defaults to `MODE=live` env)

## Anatomy (8 sections)

```python
"""Strategy docstring with usage examples."""

# 1. Imports (stdlib + numpy/pandas + openalgo + core/* helpers)
import argparse, logging, os, signal, sys, threading, time
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from dotenv import find_dotenv, load_dotenv

# Add core helpers to import path
_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent)); break

from openalgo import api
from core.cost_model import lookup as cost_lookup, format_cost_report, SlippageTracker
from core.indicator_adapter import get_indicators
from core.data_router import fetch_backtest_data, warmup_live_data, BarCloseWatcher
from core.risk_manager import RiskManager, RiskConfig, Position

# 2. CONFIG (symbol, exchange, interval, product, qty, indicator params)
SYMBOL          = "SBIN"
EXCHANGE        = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
INTERVAL        = "5m"
PRODUCT         = "MIS"
QUANTITY        = 1
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "my_strategy")

# 3. EXECUTION TYPE - "eoc" | "limit" | "stop"  (see execution-types.md)
EXECUTION_TYPE      = "eoc"
POLL_INTERVAL_SEC   = 15
LIMIT_OFFSET_PCT    = 0.0005
LIMIT_TIMEOUT_SEC   = 3
STOP_TRIGGER_BUFFER = 0.0005

# 4. RISK_CONFIG - per-position SL/TP/trail/time (see risk-management.md)
RISK = RiskConfig(
    sl_pct=0.01, tp_pct=0.02, trail_pct=0.008, time_exit_min=240,
)

# 5. BACKTEST CONFIG
INIT_CASH       = 1_000_000
LOOKBACK_DAYS   = 365 * 2

# 6. Logging (stdout only - /python host captures it)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                    stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)

# 7. Env resolution (HOST_SERVER wins over OPENALGO_HOST)
load_dotenv(find_dotenv(usecwd=True))
API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST','127.0.0.1')}:{os.getenv('WEBSOCKET_PORT','8765')}")
COSTS = cost_lookup(PRODUCT, EXCHANGE)


# 8. signals(df) - the only piece of strategy logic
def signals(df):
    """Return (entries, exits) bool Series indexed like df."""
    ind = get_indicators(INDICATOR_LIB)
    # ... compute indicators, derive entries/exits ...
    return entries, exits


# Backtest runner - pipes signals() into vbt.Portfolio.from_signals
def run_backtest():
    import vectorbt as vbt
    log.info("\n%s", format_cost_report(COSTS, INIT_CASH))
    client = api(api_key=API_KEY, host=API_HOST)
    df = fetch_backtest_data(client, SYMBOL, EXCHANGE, INTERVAL, ...)
    entries, exits = signals(df)
    pf = vbt.Portfolio.from_signals(
        df["close"], entries=entries, exits=exits,
        init_cash=INIT_CASH, fees=COSTS.fees,
        fixed_fees=COSTS.fixed_fees, slippage=COSTS.slippage,
        sl_stop=RISK.sl_pct, tp_stop=RISK.tp_pct,
        sl_trail=False if RISK.trail_pct is None else RISK.trail_pct,
        ...
    )
    log.info("\n%s", pf.stats())


# Live runner - pipes signals() into BarCloseWatcher + RiskManager
def run_live():
    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
    client.connect()
    state = {"position": None}
    risk_mgr = RiskManager(client, STRATEGY_NAME, RISK,
                           on_exit_callback=lambda *a: state.update({"position": None}),
                           slippage_tracker=SlippageTracker(COSTS.slippage))
    warmup_live_data(client, SYMBOL, EXCHANGE, INTERVAL)

    def on_bar_close(df):
        entries, exits = signals(df)
        if len(df) < 3: return
        ltp = float(df["close"].iloc[-2])  # iloc[-2] = closed bar; -1 is forming
        if entries.iloc[-2] and state["position"] is None:
            r = client.placeorder(strategy=STRATEGY_NAME, ...)
            # ... fill polling, set position, arm risk_mgr ...
        elif exits.iloc[-2] and state["position"] is not None:
            client.placesmartorder(..., position_size=0)
            risk_mgr.clear_position()
            state["position"] = None

    BarCloseWatcher(client, SYMBOL, EXCHANGE, INTERVAL,
                    on_bar_close=on_bar_close,
                    poll_interval_sec=POLL_INTERVAL_SEC,
                    stop_event=stop_event).run()


# SIGTERM-safe shutdown (required for /python self-hosted)
stop_event = threading.Event()
def _shutdown(signum, frame):
    log.info("Signal %d received - shutting down", signum)
    stop_event.set()
signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


# Dispatcher
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backtest","live"], default=os.getenv("MODE","live"))
    args = p.parse_args()
    run_backtest() if args.mode == "backtest" else run_live()


if __name__ == "__main__":
    main()
```

## What lives in `core/` vs the strategy file

| Lives in `core/` (shared) | Lives in strategy file |
|---|---|
| Cost model lookup, slippage tracker | Cost block selection (PRODUCT/EXCHANGE) |
| Indicator backend (openalgo/talib) | Which indicators to call, parameters |
| Data fetch + warmup + bar-close watcher | Symbol, exchange, interval |
| Risk manager (WS-driven SL/TP/trail) | RiskConfig values |
| Portfolio runner | Whether portfolio mode is used at all |

## Why one file per strategy

- Each strategy is independently uploadable to OpenAlgo's `/python` page (single-file constraint)
- Each strategy can have its own SQLite state, log scope, env params
- Risk configs and product types are strategy-specific - putting them in shared code creates cross-strategy coupling

## Why `iloc[-2]` and not `iloc[-1]`

`client.history()` returns the latest bar partially formed (the current minute is still printing). Using `iloc[-1]` causes:
1. Repaint - the bar values shift between polls
2. Premature signal firing - a 5m signal may fire 4m before the bar actually closes

Always use `iloc[-2]` (the just-closed bar) for signal evaluation. The bar at `iloc[-1]` should only be used for live LTP reading via the WS feed.
