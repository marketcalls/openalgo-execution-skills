"""
Opening Range Breakout - dual-mode strategy with STOP-TRIGGER execution.

After the opening range closes (default 9:15-9:30 IST = first 3x 5m bars),
calculate the high and low. Place broker-side SL-M orders:
  - BUY SL-M with trigger at OR_HIGH
  - SELL SL-M with trigger at OR_LOW

When one fires, the other is cancelled (OCO via tick polling on orderstatus).

Backtest: simulates by entering at next-bar open after OR_HIGH/OR_LOW is breached
within trading hours. Honors slippage assumption (broker latency past trigger).
"""
import argparse, logging, os, signal, sys, threading, time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv

_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent)); break

from openalgo import api  # noqa: E402
from core.cost_model import lookup as cost_lookup, format_cost_report, SlippageTracker  # noqa: E402
from core.data_router import fetch_backtest_data, warmup_live_data  # noqa: E402
from core.risk_manager import RiskManager, RiskConfig, Position  # noqa: E402
from core.sizing import fixed_fractional_size, compute_live_qty  # noqa: E402
from core.preflight import run_preflight  # noqa: E402
from core.state import StrategyState  # noqa: E402

# === Config ===
SYMBOL          = "SBIN"
EXCHANGE        = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
INTERVAL        = "5m"
PRODUCT         = "MIS"
LOT_SIZE        = 1
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "opening_range")
DATA_SOURCE     = os.getenv("DATA_SOURCE", "api")
RISK_PER_TRADE  = 0.005
MAX_SIZE_PCT    = 0.50

# Opening range: first 3x 5m bars = 9:15-9:30 IST
OR_START_TIME   = dtime(9, 15)    # IST market open
OR_END_TIME     = dtime(9, 30)    # 15-min OR
SQUARE_OFF_TIME = dtime(15, 15)   # MIS auto-squareoff

EXECUTION_TYPE  = "stop"
STOP_TRIGGER_BUFFER = 0.0005      # 5 bps above/below OR for trigger price (broker slip)

INDICATOR_LIB   = "openalgo"      # not heavily used here
RISK = RiskConfig(sl_pct=0.005, tp_pct=None, trail_pct=0.005, time_exit_min=None)
INIT_CASH       = 1_000_000
LOOKBACK_DAYS   = 365

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)
load_dotenv(find_dotenv(usecwd=True))
API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST','127.0.0.1')}:{os.getenv('WEBSOCKET_PORT','8765')}")
COSTS = cost_lookup(PRODUCT, EXCHANGE)


# ============================================================================
# BACKTEST: simulate stop-trigger entries on 5m bars
# ============================================================================

def signals_backtest(df):
    """
    Per-day OR breakout. For each trading day:
      OR_HIGH = max(high) over OR_START..OR_END
      OR_LOW  = min(low)  over OR_START..OR_END
    Entry on first bar after OR_END where high > OR_HIGH (long) or low < OR_LOW (short).
    Exit at SQUARE_OFF_TIME or next day's session open.
    """
    df = df.copy()
    df["date"] = df.index.date
    df["time"] = df.index.time

    or_mask = (df["time"] >= OR_START_TIME) & (df["time"] <= OR_END_TIME)
    or_df = df[or_mask].groupby("date").agg(or_high=("high", "max"), or_low=("low", "min"))

    entries_long  = pd.Series(False, index=df.index)
    entries_short = pd.Series(False, index=df.index)
    exits         = pd.Series(False, index=df.index)

    for date, group in df.groupby("date"):
        if date not in or_df.index:
            continue
        oh = or_df.loc[date, "or_high"]
        ol = or_df.loc[date, "or_low"]

        post_or = group[group["time"] > OR_END_TIME].copy()
        if post_or.empty:
            continue

        long_hits = post_or[post_or["high"] > oh].index
        short_hits = post_or[post_or["low"] < ol].index
        first_long  = long_hits[0] if len(long_hits) else None
        first_short = short_hits[0] if len(short_hits) else None

        # Whichever side triggers first
        if first_long is not None and (first_short is None or first_long <= first_short):
            entries_long.loc[first_long] = True
        elif first_short is not None:
            entries_short.loc[first_short] = True

        # Square-off at end of day
        eod = group[group["time"] >= SQUARE_OFF_TIME]
        if len(eod):
            exits.loc[eod.index[0]] = True
        else:
            exits.loc[group.index[-1]] = True

    return entries_long, entries_short, exits


def run_backtest():
    import vectorbt as vbt
    log.info("BACKTEST: %s %s %s @ %s", STRATEGY_NAME, SYMBOL, EXCHANGE, INTERVAL)
    log.info("\n%s", format_cost_report(COSTS, INIT_CASH))
    client = api(api_key=API_KEY, host=API_HOST)
    end = datetime.now().date(); start = end - timedelta(days=LOOKBACK_DAYS)
    df = fetch_backtest_data(client, SYMBOL, EXCHANGE, INTERVAL,
                             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                             source=DATA_SOURCE)
    if df is None or len(df) < 50:
        log.error("Insufficient bars"); return

    entries_long, entries_short, exits = signals_backtest(df)
    # Add stop-trigger slippage (broker-side latency past trigger)
    effective_slippage = COSTS.slippage + STOP_TRIGGER_BUFFER
    size_pct = fixed_fractional_size(RISK_PER_TRADE, RISK.sl_pct, MAX_SIZE_PCT)
    log.info("Sizing: %.2f%% per trade", size_pct*100)

    pf = vbt.Portfolio.from_signals(
        df["close"],
        entries=entries_long, short_entries=entries_short,
        exits=exits, short_exits=exits,
        price=df["open"].shift(-1),
        init_cash=INIT_CASH, fees=COSTS.fees, fixed_fees=COSTS.fixed_fees,
        slippage=effective_slippage, size=size_pct, size_type="percent",
        sl_stop=RISK.sl_pct, freq=_freq(INTERVAL),
        min_size=LOT_SIZE, size_granularity=LOT_SIZE,
    )
    log.info("\n=== Stats ===\n%s", pf.stats())
    out = Path("backtests") / STRATEGY_NAME; out.mkdir(parents=True, exist_ok=True)
    pf.trades.records_readable.to_csv(out / f"{SYMBOL}_trades.csv", index=False)


def _freq(i):
    return {"1m":"1min","3m":"3min","5m":"5min","10m":"10min","15m":"15min",
            "30m":"30min","1h":"1H","D":"1D"}.get(i, "5min")


# ============================================================================
# LIVE: stop-trigger execution with broker-side SL-M orders + OCO
# ============================================================================

def run_live():
    log.info("LIVE: %s %s @ %s (stop-trigger ORB)", STRATEGY_NAME, SYMBOL, INTERVAL)
    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
    try:
        run_preflight(client, symbol=SYMBOL, exchange=EXCHANGE, expected_exchange_env=EXCHANGE)
    except Exception as e:
        log.error("Preflight failed: %s - aborting", e); return

    state_db = StrategyState(_HERE / "state.db")
    qty = compute_live_qty(client, SYMBOL, EXCHANGE, sl_pct=RISK.sl_pct,
                           risk_per_trade=RISK_PER_TRADE,
                           lot_size=LOT_SIZE, min_qty=LOT_SIZE,
                           max_capital_pct=MAX_SIZE_PCT)
    if qty <= 0:
        log.error("qty=0 - aborting"); state_db.close(); return
    log.info("Live qty: %d", qty)

    client.connect()
    slip = SlippageTracker(assumed_pct=COSTS.slippage + STOP_TRIGGER_BUFFER)

    state = {"or_high": None, "or_low": None, "or_done": False,
             "buy_oid": None, "sell_oid": None,
             "filled_side": None, "position": None}

    risk_mgr = RiskManager(client, STRATEGY_NAME, RISK,
                           on_exit_callback=lambda *a: state.update({"position": None}),
                           slippage_tracker=slip, state=state_db)

    # --- 1. Wait for OR window to close, compute OR levels ---
    log.info("Waiting for opening range %s..%s to complete", OR_START_TIME, OR_END_TIME)
    while not stop_event.is_set():
        now = datetime.now().time()
        if now < OR_END_TIME:
            stop_event.wait(15); continue
        # OR window closed - fetch today's bars and compute OR
        df = warmup_live_data(client, SYMBOL, EXCHANGE, INTERVAL, lookback_bars=200,
                              source=DATA_SOURCE)
        today = datetime.now().date()
        today_df = df[df.index.date == today]
        or_df = today_df[(today_df.index.time >= OR_START_TIME)
                         & (today_df.index.time <= OR_END_TIME)]
        if len(or_df) >= 1:
            state["or_high"] = float(or_df["high"].max())
            state["or_low"]  = float(or_df["low"].min())
            state["or_done"] = True
            log.info("OR established: high=%.2f low=%.2f",
                     state["or_high"], state["or_low"])
            break
        stop_event.wait(15)

    if not state["or_done"]:
        log.warning("Exiting before OR was established"); return

    # --- 2. Place broker-side SL-M trigger orders ---
    buy_trigger  = round(state["or_high"] * (1.0 + STOP_TRIGGER_BUFFER), 2)
    sell_trigger = round(state["or_low"]  * (1.0 - STOP_TRIGGER_BUFFER), 2)

    try:
        r = client.placeorder(
            strategy=STRATEGY_NAME, symbol=SYMBOL, exchange=EXCHANGE,
            action="BUY", price_type="SL-M", product=PRODUCT,
            quantity=qty, trigger_price=str(buy_trigger),
        )
        state["buy_oid"] = r.get("orderid") if isinstance(r, dict) else None
        log.info("BUY SL-M placed @ trigger %.2f -> oid=%s", buy_trigger, state["buy_oid"])
    except Exception:
        log.exception("BUY trigger placement failed")

    try:
        r = client.placeorder(
            strategy=STRATEGY_NAME, symbol=SYMBOL, exchange=EXCHANGE,
            action="SELL", price_type="SL-M", product=PRODUCT,
            quantity=qty, trigger_price=str(sell_trigger),
        )
        state["sell_oid"] = r.get("orderid") if isinstance(r, dict) else None
        log.info("SELL SL-M placed @ trigger %.2f -> oid=%s", sell_trigger, state["sell_oid"])
    except Exception:
        log.exception("SELL trigger placement failed")

    # --- 3. Poll orderstatus for OCO fill, hand off to risk manager ---
    while not stop_event.is_set():
        now = datetime.now().time()
        if now >= SQUARE_OFF_TIME:
            log.info("Square-off time - closing")
            try: client.cancelallorder(strategy=STRATEGY_NAME)
            except Exception: log.exception("cancelallorder failed")
            try: client.closeposition(strategy=STRATEGY_NAME)
            except Exception: log.exception("closeposition failed")
            break

        # Check both orders. To avoid OCO race (both legs fill in the same
        # poll window on a sharp pin-bar), the moment ANY of the two orders
        # is observed complete we immediately cancelallorder. This is more
        # aggressive than a per-leg cancelorder but eliminates the window
        # where both can hit before we cancel.
        if state["filled_side"] is None:
            for side, oid_key in [("BUY", "buy_oid"), ("SELL", "sell_oid")]:
                oid = state.get(oid_key)
                if not oid: continue
                try:
                    s = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
                    d = s.get("data", {}) if isinstance(s, dict) else {}
                    if d.get("order_status") == "complete":
                        state["filled_side"] = side
                        fill = float(d.get("average_price") or d.get("price") or 0)
                        decision = buy_trigger if side == "BUY" else sell_trigger
                        slip.record(decision, fill, qty, side)
                        log.info("%s leg filled @ %.2f - cancelling all opposing orders", side, fill)
                        # Race fix: cancelallorder drops every pending order tagged with this
                        # strategy in one call - safer than racing per-leg cancel.
                        try:
                            client.cancelallorder(strategy=STRATEGY_NAME)
                        except Exception:
                            log.exception("OCO cancelallorder failed")
                        # Arm risk manager
                        pos = Position(SYMBOL, EXCHANGE, side, qty, fill, time.time(),
                                       PRODUCT, STRATEGY_NAME)
                        state["position"] = pos
                        risk_mgr.set_position(pos)
                        break
                except Exception:
                    log.exception("orderstatus poll failed")

        # If position is closed by risk manager, end the day's run
        if state["filled_side"] and state["position"] is None:
            log.info("Position closed - end of run")
            break

        stop_event.wait(2)

    risk_mgr.stop()
    try: client.disconnect()
    except Exception: pass
    state_db.close()
    log.info("\n%s", slip.report())


# ============================================================================
# Lifecycle
# ============================================================================

stop_event = threading.Event()
def _sh(s, f): log.info("signal %d - shutting down", s); stop_event.set()
signal.signal(signal.SIGTERM, _sh); signal.signal(signal.SIGINT, _sh)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backtest","live"], default=os.getenv("MODE","live"))
    a = p.parse_args()
    run_backtest() if a.mode == "backtest" else run_live()

if __name__ == "__main__": main()
