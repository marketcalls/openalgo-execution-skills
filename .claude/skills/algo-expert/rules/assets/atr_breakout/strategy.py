"""
ATR Breakout - dual-mode strategy with REAL-TIME LIMIT execution.

Computes a volatility band: prior close +/- ATR_MULTIPLIER * ATR(N).
Pre-places a LIMIT BUY at upper band and LIMIT SELL at lower band.
Modifies the LIMIT prices on each tick as ATR/close drifts.

If the LIMIT isn't filled within LIMIT_TIMEOUT_SEC after a band touch,
falls back to MARKET to guarantee execution.

Backtest: applies COSTS.slippage to model the LIMIT/MARKET fallback ratio.
"""
import argparse, logging, os, signal, sys, threading, time
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
from core.indicator_adapter import get_indicators  # noqa: E402
from core.data_router import fetch_backtest_data, warmup_live_data  # noqa: E402
from core.risk_manager import RiskManager, RiskConfig, Position  # noqa: E402
from core.sizing import fixed_fractional_size, compute_live_qty  # noqa: E402
from core.preflight import run_preflight, find_existing_open_position  # noqa: E402
from core.state import StrategyState  # noqa: E402

# === Config ===
SYMBOL          = "RELIANCE"
EXCHANGE        = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
INTERVAL        = "5m"
PRODUCT         = "MIS"
LOT_SIZE        = 1
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "atr_breakout")
DATA_SOURCE     = os.getenv("DATA_SOURCE", "api")
RISK_PER_TRADE  = 0.005
MAX_SIZE_PCT    = 0.50
ATR_PERIOD      = 14
ATR_MULTIPLIER  = 1.5
INDICATOR_LIB   = "openalgo"
EXECUTION_TYPE  = "limit"

LIMIT_OFFSET_PCT      = 0.0005    # peg LIMIT this far from band
LIMIT_TIMEOUT_SEC     = 3         # fall back to MARKET if not filled
LIMIT_MODIFY_THROTTLE = 1.5       # min seconds between modifies
SQUARE_OFF_TIME_HOUR  = 15
SQUARE_OFF_TIME_MIN   = 15

RISK = RiskConfig(sl_pct=0.012, tp_pct=0.025, trail_pct=0.01, time_exit_min=240)
INIT_CASH       = 1_000_000
LOOKBACK_DAYS   = 365 * 2

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
    """Backtest entries on band breach. Bars where high > upper band -> long."""
    ind = get_indicators(INDICATOR_LIB)
    atr = ind.atr(df["high"], df["low"], df["close"], ATR_PERIOD)
    upper = df["close"].shift(1) + ATR_MULTIPLIER * atr.shift(1)
    lower = df["close"].shift(1) - ATR_MULTIPLIER * atr.shift(1)

    long_entry  = (df["high"] > upper).fillna(False).astype(bool)
    short_entry = (df["low"]  < lower).fillna(False).astype(bool)
    # Time-based exit: end of session
    is_eod = df.index.time >= pd.Timestamp("15:15").time()
    exits = pd.Series(is_eod, index=df.index)
    return long_entry, short_entry, exits


def run_backtest():
    import vectorbt as vbt
    log.info("BACKTEST: %s %s %s @ %s", STRATEGY_NAME, SYMBOL, EXCHANGE, INTERVAL)
    log.info("\n%s", format_cost_report(COSTS, INIT_CASH))
    client = api(api_key=API_KEY, host=API_HOST)
    end = datetime.now().date(); start = end - timedelta(days=LOOKBACK_DAYS)
    df = fetch_backtest_data(client, SYMBOL, EXCHANGE, INTERVAL,
                             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                             source=DATA_SOURCE)
    if df is None or len(df) < ATR_PERIOD * 3:
        log.error("Insufficient bars"); return
    long_e, short_e, exits = signals_backtest(df)
    size_pct = fixed_fractional_size(RISK_PER_TRADE, RISK.sl_pct, MAX_SIZE_PCT)
    log.info("Sizing: %.2f%% per trade", size_pct*100)
    pf = vbt.Portfolio.from_signals(
        df["close"],
        entries=long_e, short_entries=short_e,
        exits=exits, short_exits=exits,
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


# ============================================================================
# LIVE: tick-driven LIMIT placement + modification
# ============================================================================

def run_live():
    log.info("LIVE: %s %s @ %s (real-time LIMIT)", STRATEGY_NAME, SYMBOL, INTERVAL)
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

    # Compute initial bands from warmup history
    ind = get_indicators(INDICATOR_LIB)
    df = warmup_live_data(client, SYMBOL, EXCHANGE, INTERVAL, lookback_bars=300,
                          source=DATA_SOURCE)
    atr_series = ind.atr(df["high"], df["low"], df["close"], ATR_PERIOD)
    last_close = float(df["close"].iloc[-1])
    last_atr   = float(atr_series.iloc[-1])
    upper = last_close + ATR_MULTIPLIER * last_atr
    lower = last_close - ATR_MULTIPLIER * last_atr
    log.info("Initial bands: upper=%.2f lower=%.2f (close=%.2f atr=%.2f)",
             upper, lower, last_close, last_atr)

    state = {
        "buy_oid": None, "sell_oid": None,
        "buy_price": None, "sell_price": None,
        "last_modify": 0.0,
        "position": None,
    }

    risk_mgr = RiskManager(client, STRATEGY_NAME, RISK,
                           on_exit_callback=lambda *a: state.update({"position": None}),
                           slippage_tracker=slip, state=state_db)
    state["last_band_update"] = time.time()      # for MARKET fallback decision

    def place_limit(action, price):
        try:
            r = client.placeorder(
                strategy=STRATEGY_NAME, symbol=SYMBOL, exchange=EXCHANGE,
                action=action, price_type="LIMIT", product=PRODUCT,
                quantity=qty, price=str(round(price, 2)),
            )
            return r.get("orderid") if isinstance(r, dict) else None
        except Exception:
            log.exception("place_limit failed for %s @ %.2f", action, price); return None

    def modify_limit(oid, action, price):
        if oid is None: return
        try:
            client.modifyorder(
                order_id=oid, strategy=STRATEGY_NAME,
                symbol=SYMBOL, exchange=EXCHANGE,
                action=action, price_type="LIMIT", product=PRODUCT,
                quantity=qty, price=str(round(price, 2)),
            )
        except Exception:
            log.exception("modifyorder failed for %s @ %.2f", action, price)

    # Place initial LIMIT orders pegged at bands +/- offset
    state["buy_price"]  = round(upper * (1.0 + LIMIT_OFFSET_PCT), 2)
    state["sell_price"] = round(lower * (1.0 - LIMIT_OFFSET_PCT), 2)
    state["buy_oid"]  = place_limit("BUY",  state["buy_price"])
    state["sell_oid"] = place_limit("SELL", state["sell_price"])
    log.info("LIMIT BUY @ %.2f oid=%s | LIMIT SELL @ %.2f oid=%s",
             state["buy_price"], state["buy_oid"], state["sell_price"], state["sell_oid"])

    def on_tick(data):
        if state["position"] is not None:
            return  # don't modify after fill
        try:
            ltp = float(data.get("data", {}).get("ltp", 0))
        except (TypeError, ValueError):
            return
        if ltp <= 0: return

        # Throttle modifications
        if time.time() - state["last_modify"] < LIMIT_MODIFY_THROTTLE:
            return

        # Recompute bands using current LTP as proxy for live close
        new_upper = ltp + ATR_MULTIPLIER * last_atr
        new_lower = ltp - ATR_MULTIPLIER * last_atr
        new_buy  = round(new_upper * (1.0 + LIMIT_OFFSET_PCT), 2)
        new_sell = round(new_lower * (1.0 - LIMIT_OFFSET_PCT), 2)

        # Modify if drift is more than 1 tick
        if state["buy_oid"] and abs(new_buy - state["buy_price"]) >= 0.05:
            log.debug("Modify BUY %.2f -> %.2f", state["buy_price"], new_buy)
            modify_limit(state["buy_oid"], "BUY", new_buy)
            state["buy_price"] = new_buy
        if state["sell_oid"] and abs(new_sell - state["sell_price"]) >= 0.05:
            log.debug("Modify SELL %.2f -> %.2f", state["sell_price"], new_sell)
            modify_limit(state["sell_oid"], "SELL", new_sell)
            state["sell_price"] = new_sell
        state["last_modify"] = time.time()

    instruments = [{"exchange": EXCHANGE, "symbol": SYMBOL}]
    client.subscribe_ltp(instruments, on_data_received=on_tick)

    # OCO + risk handoff polling
    while not stop_event.is_set():
        now = datetime.now()
        if now.hour > SQUARE_OFF_TIME_HOUR or (
            now.hour == SQUARE_OFF_TIME_HOUR and now.minute >= SQUARE_OFF_TIME_MIN):
            log.info("Square-off")
            try: client.cancelallorder(strategy=STRATEGY_NAME)
            except Exception: log.exception("cancelallorder failed")
            try: client.closeposition(strategy=STRATEGY_NAME)
            except Exception: log.exception("closeposition failed")
            break

        # Check OCO fill
        if state["position"] is None:
            for side, oid_key, price_key in [("BUY","buy_oid","buy_price"),
                                              ("SELL","sell_oid","sell_price")]:
                oid = state.get(oid_key)
                if not oid: continue
                try:
                    r = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
                    d = r.get("data", {}) if isinstance(r, dict) else {}
                    if d.get("order_status") == "complete":
                        fill = float(d.get("average_price") or d.get("price") or 0)
                        slip.record(state[price_key], fill, qty, side)
                        log.info("%s LIMIT filled @ %.2f", side, fill)
                        other = "sell_oid" if oid_key == "buy_oid" else "buy_oid"
                        if state[other]:
                            try:
                                client.cancelorder(order_id=state[other], strategy=STRATEGY_NAME)
                                log.info("Cancelled OCO leg")
                            except Exception:
                                log.exception("OCO cancel failed")
                        pos = Position(SYMBOL, EXCHANGE, side, qty, fill,
                                       time.time(), PRODUCT, STRATEGY_NAME)
                        state["position"] = pos
                        risk_mgr.set_position(pos)
                        break
                except Exception:
                    log.exception("orderstatus poll failed")

        # LIMIT timeout MARKET fallback: if we're past LIMIT_TIMEOUT_SEC and
        # the price has moved through one of the bands without filling, cancel
        # the LIMIT and place MARKET to guarantee execution. The "price moved
        # through" check uses get_ltp() against the band level.
        if state["position"] is None and LIMIT_TIMEOUT_SEC > 0:
            try:
                snapshot = client.get_ltp() or {}
                ltp_now = float(
                    snapshot.get(EXCHANGE, {}).get(SYMBOL, {}).get("ltp", 0) or 0
                )
            except Exception:
                ltp_now = 0
            if ltp_now > 0 and (time.time() - state["last_band_update"]) > LIMIT_TIMEOUT_SEC:
                fallback_side = None
                if state["buy_price"] and ltp_now >= state["buy_price"]:
                    fallback_side = "BUY"
                elif state["sell_price"] and ltp_now <= state["sell_price"]:
                    fallback_side = "SELL"
                if fallback_side is not None:
                    log.warning("LIMIT timeout: price %.2f past %s band - falling back to MARKET",
                                ltp_now, fallback_side)
                    # Cancel both LIMIT orders
                    for o in (state["buy_oid"], state["sell_oid"]):
                        if o:
                            try: client.cancelorder(order_id=o, strategy=STRATEGY_NAME)
                            except Exception: log.exception("cancel before MARKET failed")
                    # MARKET entry
                    try:
                        r = client.placeorder(
                            strategy=STRATEGY_NAME, symbol=SYMBOL, exchange=EXCHANGE,
                            action=fallback_side, price_type="MARKET",
                            product=PRODUCT, quantity=qty,
                        )
                        oid = r.get("orderid") if isinstance(r, dict) else None
                        if oid:
                            d = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
                            fill = float(d.get("data", {}).get("average_price")
                                         or d.get("data", {}).get("price") or ltp_now)
                            decided = state["buy_price"] if fallback_side == "BUY" else state["sell_price"]
                            slip.record(decided, fill, qty, fallback_side)
                            pos = Position(SYMBOL, EXCHANGE, fallback_side, qty, fill,
                                           time.time(), PRODUCT, STRATEGY_NAME)
                            state["position"] = pos
                            risk_mgr.set_position(pos)
                            log.info("MARKET fallback filled %s @ %.2f", fallback_side, fill)
                    except Exception:
                        log.exception("MARKET fallback failed")

        if state["position"] is not None and state["position"].closed:
            log.info("Position closed by risk manager - end of run")
            break

        stop_event.wait(1)

    try: client.unsubscribe_ltp(instruments)
    except Exception: pass
    risk_mgr.stop()
    try: client.disconnect()
    except Exception: pass
    state_db.close()
    log.info("\n%s", slip.report())


stop_event = threading.Event()
def _sh(s, f): log.info("signal %d - shutting down", s); stop_event.set()
signal.signal(signal.SIGTERM, _sh); signal.signal(signal.SIGINT, _sh)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backtest","live"], default=os.getenv("MODE","live"))
    a = p.parse_args()
    run_backtest() if a.mode == "backtest" else run_live()

if __name__ == "__main__": main()
