"""
Event-Driven Strategy - dual-mode.

A scheduled-time entry strategy keyed off market events. Default flavor:
"earnings results-day drift" - enters at 09:30 IST on configured EVENT_DATES,
holds for HOLDING_DAYS, exits at SQUARE_OFF_TIME.

Other event flavors you can rewire to:
  - Pre-results bullish gap: enter day-before-results at close
  - Dividend ex-date arbitrage: short day-before, cover ex-date
  - Index rebalance front-running: enter night before announcement
  - Budget day vol play: enter pre-budget straddle (use options template)

Backtest mode replays historical event dates against history. Live mode
schedules the entry via APScheduler.
"""
import argparse, logging, os, signal, sys, threading, time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import pytz
from dotenv import find_dotenv, load_dotenv

_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent)); break

from openalgo import api  # noqa: E402
from core.cost_model import lookup as cost_lookup, format_cost_report, SlippageTracker  # noqa: E402
from core.data_router import fetch_backtest_data  # noqa: E402
from core.risk_manager import RiskManager, RiskConfig, Position  # noqa: E402
from core.sizing import fixed_fractional_size, compute_live_qty  # noqa: E402
from core.preflight import run_preflight  # noqa: E402
from core.state import StrategyState  # noqa: E402

# === Config ===
SYMBOL          = "RELIANCE"
EXCHANGE        = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
INTERVAL        = "D"
PRODUCT         = "CNC"
LOT_SIZE        = 1
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "event_driven")
DATA_SOURCE     = os.getenv("DATA_SOURCE", "api")
RISK_PER_TRADE  = 0.01     # event trades are higher conviction; allow 1% risk
MAX_SIZE_PCT    = 0.50

# Event configuration
# Dates are YYYY-MM-DD strings. For results-day drift, set these to historical
# results dates for backtest, or upcoming results for live.
EVENT_DATES     = os.getenv("EVENT_DATES",
                            "2025-07-19,2025-10-18,2026-01-17,2026-04-19").split(",")
HOLDING_DAYS    = 5             # exit after N trading days
ENTRY_DIRECTION = "BUY"         # "BUY" for bullish drift, "SELL" for bearish
ENTRY_TIME_LIVE = dtime(9, 30)
SQUARE_OFF_TIME = dtime(15, 15)

INDICATOR_LIB   = "openalgo"

RISK = RiskConfig(sl_pct=0.04, tp_pct=0.08, trail_pct=0.03, time_exit_min=None)
INIT_CASH       = 1_000_000
LOOKBACK_DAYS   = 365 * 3

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)
load_dotenv(find_dotenv(usecwd=True))
API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST','127.0.0.1')}:{os.getenv('WEBSOCKET_PORT','8765')}")
COSTS = cost_lookup(PRODUCT, EXCHANGE)


def signals_backtest(df):
    """Mark entry on event dates, exit HOLDING_DAYS later."""
    entries = pd.Series(False, index=df.index)
    exits   = pd.Series(False, index=df.index)
    event_set = set(pd.to_datetime(d).date() for d in EVENT_DATES)
    for ts in df.index:
        d = ts.date()
        if d in event_set:
            entries.loc[ts] = True
            # Find exit ts: HOLDING_DAYS later (next trading bar in df)
            future = df.index[df.index > ts]
            if len(future) > HOLDING_DAYS - 1:
                exits.loc[future[HOLDING_DAYS - 1]] = True
            elif len(future):
                exits.loc[future[-1]] = True
    return entries, exits


def run_backtest():
    import vectorbt as vbt
    log.info("BACKTEST: %s on %s/%s with %d event dates",
             STRATEGY_NAME, SYMBOL, EXCHANGE, len(EVENT_DATES))
    log.info("\n%s", format_cost_report(COSTS, INIT_CASH))
    client = api(api_key=API_KEY, host=API_HOST)

    end = datetime.now().date(); start = end - timedelta(days=LOOKBACK_DAYS)
    df = fetch_backtest_data(client, SYMBOL, EXCHANGE, INTERVAL,
                             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                             source=DATA_SOURCE)
    if df is None or len(df) < 50:
        log.error("Insufficient bars"); return
    entries, exits = signals_backtest(df)
    log.info("Event entries triggered: %d", int(entries.sum()))
    size_pct = fixed_fractional_size(RISK_PER_TRADE, RISK.sl_pct, MAX_SIZE_PCT)

    pf = vbt.Portfolio.from_signals(
        df["close"], entries=entries, exits=exits,
        price=df["open"].shift(-1),
        init_cash=INIT_CASH, fees=COSTS.fees, fixed_fees=COSTS.fixed_fees,
        slippage=COSTS.slippage, size=size_pct, size_type="percent",
        sl_stop=RISK.sl_pct, tp_stop=RISK.tp_pct,
        sl_trail=False if RISK.trail_pct is None else RISK.trail_pct,
        freq=_freq(INTERVAL), min_size=LOT_SIZE, size_granularity=LOT_SIZE,
    )
    log.info("\n=== Stats ===\n%s", pf.stats())
    out = Path("backtests") / STRATEGY_NAME; out.mkdir(parents=True, exist_ok=True)
    pf.trades.records_readable.to_csv(out / f"{SYMBOL}_event_trades.csv", index=False)


def _freq(i):
    return {"1m":"1min","3m":"3min","5m":"5min","10m":"10min","15m":"15min",
            "30m":"30min","1h":"1H","D":"1D"}.get(i, "1D")


def run_live():
    """
    Schedules entry via APScheduler at ENTRY_TIME_LIVE on configured event dates.
    Holds for HOLDING_DAYS then flattens. Risk manager runs throughout.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    log.info("LIVE: %s on %s for %d event dates", STRATEGY_NAME, SYMBOL, len(EVENT_DATES))
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
    slip = SlippageTracker(assumed_pct=COSTS.slippage)
    state = {"position": None, "entry_date": None}
    risk_mgr = RiskManager(client, STRATEGY_NAME, RISK,
                           on_exit_callback=lambda *a: state.update({"position": None}),
                           slippage_tracker=slip, state=state_db)

    def place_event_entry():
        today = datetime.now(pytz.timezone("Asia/Kolkata")).date()
        if today.isoformat() not in [d.strip() for d in EVENT_DATES]:
            log.info("Today %s is not an event date - skipping", today)
            return
        if state["position"] is not None:
            log.warning("Already in position - skipping event entry")
            return
        try:
            ltp = float(client.quotes(symbol=SYMBOL, exchange=EXCHANGE)["data"]["ltp"])
        except Exception:
            log.exception("quotes failed"); return
        log.info("EVENT entry %s %s @ %.2f", ENTRY_DIRECTION, SYMBOL, ltp)
        try:
            r = client.placeorder(strategy=STRATEGY_NAME, symbol=SYMBOL, exchange=EXCHANGE,
                                  action=ENTRY_DIRECTION, price_type="MARKET",
                                  product=PRODUCT, quantity=qty)
            oid = r.get("orderid") if isinstance(r, dict) else None
            fill = _wait_fill(client, oid, ltp) if oid else ltp
            slip.record(ltp, fill, qty, ENTRY_DIRECTION)
            pos = Position(SYMBOL, EXCHANGE, ENTRY_DIRECTION, qty, fill,
                           time.time(), PRODUCT, STRATEGY_NAME)
            state["position"] = pos
            state["entry_date"] = today
            risk_mgr.set_position(pos)
        except Exception:
            log.exception("Event entry failed")

    def check_holding_period_exit():
        if state["position"] is None or state["entry_date"] is None:
            return
        today = datetime.now(pytz.timezone("Asia/Kolkata")).date()
        if (today - state["entry_date"]).days >= HOLDING_DAYS:
            log.info("Holding period reached (%d days) - flattening", HOLDING_DAYS)
            try:
                opposite = "SELL" if ENTRY_DIRECTION == "BUY" else "BUY"
                client.placesmartorder(strategy=STRATEGY_NAME, symbol=SYMBOL,
                                       exchange=EXCHANGE, action=opposite,
                                       price_type="MARKET", product=PRODUCT,
                                       quantity=state["position"].qty, position_size=0)
            except Exception:
                log.exception("holding-period exit failed")
            risk_mgr.clear_position()
            state["position"] = None

    ist = pytz.timezone("Asia/Kolkata")
    scheduler = BackgroundScheduler(timezone=ist)
    scheduler.add_job(place_event_entry, trigger="cron",
                      hour=ENTRY_TIME_LIVE.hour, minute=ENTRY_TIME_LIVE.minute,
                      id="event_entry")
    scheduler.add_job(check_holding_period_exit, trigger="cron",
                      hour=SQUARE_OFF_TIME.hour, minute=SQUARE_OFF_TIME.minute,
                      id="event_exit")
    scheduler.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(60)
    finally:
        scheduler.shutdown(wait=False)
        risk_mgr.stop()
        try: client.disconnect()
        except Exception: pass
        state_db.close()
        log.info("\n%s", slip.report())


def _wait_fill(client, oid, fallback, retries=10, sleep_s=0.5):
    for _ in range(retries):
        try:
            r = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
            d = r.get("data", {}) if isinstance(r, dict) else {}
            if d.get("order_status") == "complete":
                avg = d.get("average_price") or d.get("price")
                if avg: return float(avg)
        except Exception: log.exception("orderstatus poll failed")
        time.sleep(sleep_s)
    return fallback


stop_event = threading.Event()
def _sh(s, f): log.info("signal %d - shutting down", s); stop_event.set()
signal.signal(signal.SIGTERM, _sh); signal.signal(signal.SIGINT, _sh)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backtest","live"], default=os.getenv("MODE","live"))
    a = p.parse_args()
    run_backtest() if a.mode == "backtest" else run_live()

if __name__ == "__main__": main()
