"""
ML Logistic Regression - dual-mode strategy.

Loads a pre-trained sklearn pipeline (see train.py) and uses its prediction
to generate entry signals. Same prediction function feeds both backtest
(VectorBT) and live (event loop).

Workflow:
    1. python train.py --symbol RELIANCE      # offline fit, saves pickle
    2. python strategy.py --mode backtest      # vectorbt backtest using model
    3. python strategy.py --mode live           # live execution
"""
import argparse, logging, os, pickle, signal, sys, threading, time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import find_dotenv, load_dotenv

_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent)); break

from openalgo import api  # noqa: E402
from core.cost_model import lookup as cost_lookup, format_cost_report, SlippageTracker  # noqa: E402
from core.data_router import fetch_backtest_data, warmup_live_data, BarCloseWatcher  # noqa: E402
from core.risk_manager import RiskManager, RiskConfig, Position  # noqa: E402
from core.sizing import fixed_fractional_size, compute_live_qty  # noqa: E402
from core.preflight import run_preflight, find_existing_open_position  # noqa: E402
from core.state import StrategyState, reconcile_with_broker  # noqa: E402

# Re-use the feature builder from train.py
sys.path.insert(0, str(_HERE))
from train import make_features  # noqa: E402

# === Config ===
SYMBOL          = "RELIANCE"
EXCHANGE        = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
INTERVAL        = "15m"
PRODUCT         = "MIS"
LOT_SIZE        = 1
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "ml_logistic")
DATA_SOURCE     = os.getenv("DATA_SOURCE", "api")
RISK_PER_TRADE  = 0.005
MAX_SIZE_PCT    = 0.50
MODEL_PATH      = _HERE / f"{SYMBOL}_logistic.pkl"
PROB_THRESHOLD  = 0.55                # only enter when P(up) >= this
INDICATOR_LIB   = "openalgo"
EXECUTION_TYPE  = "eoc"
POLL_INTERVAL_SEC = 30
RISK = RiskConfig(sl_pct=0.012, tp_pct=0.025, trail_pct=0.01, time_exit_min=180)
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


def load_model():
    if not MODEL_PATH.exists():
        log.error("Model not found at %s. Run: python train.py first.", MODEL_PATH)
        sys.exit(1)
    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    log.info("Loaded model trained %s, walk-forward acc=%.4f",
             bundle.get("trained_at", "n/a"), bundle.get("walk_forward_acc", 0.0))
    return bundle


def signals(df, model_bundle):
    pipeline = model_bundle["pipeline"]
    feature_cols = model_bundle["features"]
    X, _ = make_features(df, lib=INDICATOR_LIB)
    if len(X) == 0:
        empty = pd.Series(False, index=df.index)
        return empty, empty
    proba = pipeline.predict_proba(X[feature_cols])[:, 1]
    proba_s = pd.Series(proba, index=X.index)
    proba_full = proba_s.reindex(df.index)

    prev = proba_full.shift(1)
    entries = ((proba_full >= PROB_THRESHOLD) & (prev < PROB_THRESHOLD)).fillna(False).astype(bool)
    exits   = ((proba_full < (1 - PROB_THRESHOLD)) & (prev >= (1 - PROB_THRESHOLD))).fillna(False).astype(bool)
    return entries, exits


def run_backtest():
    import vectorbt as vbt
    log.info("BACKTEST: %s %s %s @ %s", STRATEGY_NAME, SYMBOL, EXCHANGE, INTERVAL)
    log.info("\n%s", format_cost_report(COSTS, INIT_CASH))
    bundle = load_model()
    client = api(api_key=API_KEY, host=API_HOST)
    end = datetime.now().date(); start = end - timedelta(days=LOOKBACK_DAYS)
    df = fetch_backtest_data(client, SYMBOL, EXCHANGE, INTERVAL,
                             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                             source=DATA_SOURCE)
    if df is None or len(df) < 200:
        log.error("Insufficient bars"); return
    entries, exits = signals(df, bundle)
    size_pct = fixed_fractional_size(RISK_PER_TRADE, RISK.sl_pct, MAX_SIZE_PCT)
    log.info("Sizing: %.2f%% per trade", size_pct*100)
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
    pf.trades.records_readable.to_csv(out / f"{SYMBOL}_trades.csv", index=False)


def _freq(i):
    return {"1m":"1min","3m":"3min","5m":"5min","10m":"10min","15m":"15min",
            "30m":"30min","1h":"1H","D":"1D"}.get(i, "5min")


def run_live():
    log.info("LIVE: %s %s @ %s", STRATEGY_NAME, SYMBOL, INTERVAL)
    bundle = load_model()
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
    state = {"position": None}
    risk_mgr = RiskManager(client, STRATEGY_NAME, RISK,
                           on_exit_callback=lambda *a: state.update({"position": None}),
                           slippage_tracker=slip, state=state_db)
    resumed = reconcile_with_broker(state_db, client, SYMBOL, EXCHANGE)
    if resumed is not None:
        pos = Position(resumed.symbol, resumed.exchange, resumed.side, resumed.qty,
                       resumed.entry_price, resumed.entry_time, resumed.product, STRATEGY_NAME)
        state["position"] = pos
        risk_mgr.set_position(pos, restore_watermark=resumed.watermark)
    warmup_live_data(client, SYMBOL, EXCHANGE, INTERVAL, lookback_bars=300, source=DATA_SOURCE)

    def on_bar_close(df):
        entries, exits = signals(df, bundle)
        if len(df) < 3: return
        ltp = float(df["close"].iloc[-2])
        bar_ts = str(df.index[-2])
        if state_db.signal_already_acted(STRATEGY_NAME, bar_ts): return
        if entries.iloc[-2] and state["position"] is None:
            if find_existing_open_position(client, SYMBOL, EXCHANGE) is not None:
                log.warning("Broker has open pos - skip ENTRY")
                state_db.mark_signal_acted(STRATEGY_NAME, bar_ts); return
            log.info("ML ENTRY %s @ %.2f", df.index[-2], ltp)
            r = client.placeorder(strategy=STRATEGY_NAME, symbol=SYMBOL, exchange=EXCHANGE,
                                  action="BUY", price_type="MARKET", product=PRODUCT, quantity=qty)
            oid = r.get("orderid") if isinstance(r, dict) else None
            fill = _wait_fill(client, oid, ltp) if oid else ltp
            slip.record(ltp, fill, qty, "BUY")
            pos = Position(SYMBOL, EXCHANGE, "BUY", qty, fill, time.time(), PRODUCT, STRATEGY_NAME)
            state["position"] = pos; risk_mgr.set_position(pos)
            state_db.mark_signal_acted(STRATEGY_NAME, bar_ts)
        elif exits.iloc[-2] and state["position"] is not None:
            log.info("ML EXIT %s @ %.2f", df.index[-2], ltp)
            try:
                client.placesmartorder(strategy=STRATEGY_NAME, symbol=SYMBOL, exchange=EXCHANGE,
                                       action="SELL", price_type="MARKET", product=PRODUCT,
                                       quantity=state["position"].qty, position_size=0)
            except Exception: log.exception("exit failed")
            risk_mgr.clear_position(); state["position"] = None
            state_db.mark_signal_acted(STRATEGY_NAME, bar_ts)

    watcher = BarCloseWatcher(client, SYMBOL, EXCHANGE, INTERVAL, on_bar_close,
                              poll_interval_sec=POLL_INTERVAL_SEC, stop_event=stop_event)
    try: watcher.run()
    finally:
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
