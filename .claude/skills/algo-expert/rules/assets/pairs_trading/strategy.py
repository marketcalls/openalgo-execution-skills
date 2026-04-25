"""
Pairs Trading - dual-mode strategy.

Trades two cointegrated symbols. When the spread (Y - beta*X) deviates from
its rolling mean by >= ENTRY_Z standard deviations, enter long-spread (BUY Y,
SELL X) or short-spread, expecting reversion. Exit at +/- EXIT_Z (default 0).

Common pairs in Indian markets:
  - SBIN vs PNB         (PSU banks)
  - RELIANCE vs ONGC     (energy)
  - HDFCBANK vs ICICIBANK (private banks)
  - TCS vs INFY          (IT services)

Note: cointegration must be verified before live use. Run a separate
Engle-Granger test (scipy.stats / statsmodels) on history to confirm the
pair is cointegrated, not just correlated.
"""
import argparse, logging, os, signal, sys, threading, time
from datetime import datetime, timedelta
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
from core.data_router import fetch_backtest_data, warmup_live_data, BarCloseWatcher  # noqa: E402
from core.risk_manager import RiskManager, RiskConfig, Position  # noqa: E402
from core.sizing import compute_live_qty  # noqa: E402
from core.preflight import run_preflight  # noqa: E402
from core.state import StrategyState  # noqa: E402

# === Config ===
SYMBOL_Y        = "SBIN"      # the dependent (we long it when spread is below mean)
SYMBOL_X        = "PNB"       # the independent
EXCHANGE        = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
INTERVAL        = "D"
PRODUCT         = "MIS"
LOT_SIZE        = 1
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "pairs_sbin_pnb")
DATA_SOURCE     = os.getenv("DATA_SOURCE", "api")

# Cointegration parameters
ZSCORE_LOOKBACK = 60          # bars used to estimate spread mean and std
HEDGE_LOOKBACK  = 252         # bars used to estimate beta (Y = beta * X)
ENTRY_Z         = 2.0         # enter when |z-score| >= this
EXIT_Z          = 0.5         # exit when |z-score| <= this
STOP_Z          = 4.0         # bail-out if z-score exceeds this (cointegration broken?)

POLL_INTERVAL_SEC = 30
INDICATOR_LIB   = "openalgo"  # not heavily used here

# Risk: per-leg stop and time exit. Pairs P&L caps at the strategy level.
RISK = RiskConfig(sl_pct=None, tp_pct=None, trail_pct=None, time_exit_min=None)
INIT_CASH       = 1_000_000
LOOKBACK_DAYS   = 365 * 3
RISK_PER_TRADE  = 0.005
MAX_SIZE_PCT    = 0.40        # split between two legs => total exposure ~80%

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)
load_dotenv(find_dotenv(usecwd=True))
API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST','127.0.0.1')}:{os.getenv('WEBSOCKET_PORT','8765')}")
COSTS = cost_lookup(PRODUCT, EXCHANGE)


def compute_spread_zscore(df_y, df_x):
    """Returns aligned (y, x, beta_series, spread, zscore) frames."""
    common = df_y.index.intersection(df_x.index)
    y = df_y.loc[common, "close"]
    x = df_x.loc[common, "close"]

    # Rolling beta from y = beta * x (OLS without intercept; centered prices)
    beta = y.rolling(HEDGE_LOOKBACK).cov(x) / x.rolling(HEDGE_LOOKBACK).var()
    spread = y - beta * x
    mean = spread.rolling(ZSCORE_LOOKBACK).mean()
    std = spread.rolling(ZSCORE_LOOKBACK).std()
    z = (spread - mean) / std
    return y, x, beta, spread, z


def signals_for_backtest(z):
    """Long-spread when z <= -ENTRY_Z; short-spread when z >= +ENTRY_Z."""
    long_entry  = z <= -ENTRY_Z
    short_entry = z >= +ENTRY_Z
    flat_exit   = (z.abs() <= EXIT_Z) | (z.abs() >= STOP_Z)
    return long_entry.fillna(False), short_entry.fillna(False), flat_exit.fillna(False)


def run_backtest():
    import vectorbt as vbt
    log.info("BACKTEST: %s on %s/%s", STRATEGY_NAME, SYMBOL_Y, SYMBOL_X)
    log.info("\n%s", format_cost_report(COSTS, INIT_CASH))
    client = api(api_key=API_KEY, host=API_HOST)

    end = datetime.now().date(); start = end - timedelta(days=LOOKBACK_DAYS)
    df_y = fetch_backtest_data(client, SYMBOL_Y, EXCHANGE, INTERVAL,
                               start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                               source=DATA_SOURCE)
    df_x = fetch_backtest_data(client, SYMBOL_X, EXCHANGE, INTERVAL,
                               start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                               source=DATA_SOURCE)
    if df_y is None or df_x is None or len(df_y) < HEDGE_LOOKBACK + ZSCORE_LOOKBACK:
        log.error("Insufficient bars (need at least %d)", HEDGE_LOOKBACK + ZSCORE_LOOKBACK)
        return

    y, x, beta, spread, z = compute_spread_zscore(df_y, df_x)
    long_e, short_e, exits = signals_for_backtest(z)

    # Backtest the spread series directly. Trade y - beta*x as the asset;
    # this is an approximation since beta drifts, but useful for stats.
    pf = vbt.Portfolio.from_signals(
        y,
        entries=long_e, short_entries=short_e,
        exits=exits, short_exits=exits,
        price=y.shift(-1),                      # next-bar fill
        init_cash=INIT_CASH,
        fees=COSTS.fees * 2, fixed_fees=COSTS.fixed_fees * 2,    # two legs
        slippage=COSTS.slippage,
        size=MAX_SIZE_PCT, size_type="percent",
        freq=_freq(INTERVAL), min_size=LOT_SIZE, size_granularity=LOT_SIZE,
    )
    log.info("\n=== Stats ===\n%s", pf.stats())
    log.info("Mean beta: %.3f | latest z-score: %.3f", float(beta.mean()), float(z.iloc[-1] or 0))

    out = Path("backtests") / STRATEGY_NAME; out.mkdir(parents=True, exist_ok=True)
    pf.trades.records_readable.to_csv(out / f"{SYMBOL_Y}_{SYMBOL_X}_trades.csv", index=False)


def _freq(i):
    return {"1m":"1min","3m":"3min","5m":"5min","10m":"10min","15m":"15min",
            "30m":"30min","1h":"1H","D":"1D"}.get(i, "1D")


def run_live():
    """
    Live pairs is more involved than single-symbol because each entry places
    two orders (BUY Y + SELL X for long-spread). The risk manager only tracks
    one leg. For a production pairs strategy you typically want a custom
    risk supervisor. This template implements a simple two-leg open/close loop.
    """
    log.info("LIVE: %s pair=%s/%s @ %s", STRATEGY_NAME, SYMBOL_Y, SYMBOL_X, INTERVAL)
    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
    try:
        run_preflight(client, symbol=SYMBOL_Y, exchange=EXCHANGE, expected_exchange_env=EXCHANGE)
        run_preflight(client, symbol=SYMBOL_X, exchange=EXCHANGE)
    except Exception as e:
        log.error("Preflight failed: %s - aborting", e); return

    state_db = StrategyState(_HERE / "state.db")
    qty_y = compute_live_qty(client, SYMBOL_Y, EXCHANGE, sl_pct=0.02,
                             risk_per_trade=RISK_PER_TRADE * 0.5,
                             lot_size=LOT_SIZE, max_capital_pct=MAX_SIZE_PCT)
    qty_x = compute_live_qty(client, SYMBOL_X, EXCHANGE, sl_pct=0.02,
                             risk_per_trade=RISK_PER_TRADE * 0.5,
                             lot_size=LOT_SIZE, max_capital_pct=MAX_SIZE_PCT)
    if qty_y <= 0 or qty_x <= 0:
        log.error("Sizing returned 0 - aborting"); state_db.close(); return
    log.info("Live qty: Y=%d X=%d", qty_y, qty_x)

    client.connect()
    state = {"open_side": None}     # "LONG_SPREAD" | "SHORT_SPREAD" | None
    slip = SlippageTracker(assumed_pct=COSTS.slippage)

    def on_bar_close(df_y_chunk):
        # Re-fetch X bars at the same interval; pair signals need both
        try:
            from_d = (datetime.now() - timedelta(days=HEDGE_LOOKBACK)).strftime("%Y-%m-%d")
            to_d   = datetime.now().strftime("%Y-%m-%d")
            df_x_now = client.history(symbol=SYMBOL_X, exchange=EXCHANGE, interval=INTERVAL,
                                      start_date=from_d, end_date=to_d, source="api")
            from core.data_router import normalize_history
            df_x_now = normalize_history(df_x_now)
        except Exception:
            log.exception("X bars fetch failed - skipping")
            return

        if df_x_now is None or len(df_y_chunk) < HEDGE_LOOKBACK or len(df_x_now) < HEDGE_LOOKBACK:
            return

        _, _, _, _, z = compute_spread_zscore(df_y_chunk, df_x_now)
        if len(z) < 2: return
        cur_z = float(z.iloc[-2])     # closed bar, no repaint
        log.info("z-score: %.3f", cur_z)

        if state["open_side"] is None:
            if cur_z <= -ENTRY_Z:
                _enter_long_spread()
                state["open_side"] = "LONG_SPREAD"
            elif cur_z >= +ENTRY_Z:
                _enter_short_spread()
                state["open_side"] = "SHORT_SPREAD"
        else:
            if abs(cur_z) <= EXIT_Z or abs(cur_z) >= STOP_Z:
                _flatten()
                state["open_side"] = None

    def _enter_long_spread():
        log.info("LONG SPREAD: BUY %s, SELL %s", SYMBOL_Y, SYMBOL_X)
        try:
            client.placeorder(strategy=STRATEGY_NAME, symbol=SYMBOL_Y, exchange=EXCHANGE,
                              action="BUY", price_type="MARKET", product=PRODUCT, quantity=qty_y)
            client.placeorder(strategy=STRATEGY_NAME, symbol=SYMBOL_X, exchange=EXCHANGE,
                              action="SELL", price_type="MARKET", product=PRODUCT, quantity=qty_x)
        except Exception:
            log.exception("Long-spread entry failed")

    def _enter_short_spread():
        log.info("SHORT SPREAD: SELL %s, BUY %s", SYMBOL_Y, SYMBOL_X)
        try:
            client.placeorder(strategy=STRATEGY_NAME, symbol=SYMBOL_Y, exchange=EXCHANGE,
                              action="SELL", price_type="MARKET", product=PRODUCT, quantity=qty_y)
            client.placeorder(strategy=STRATEGY_NAME, symbol=SYMBOL_X, exchange=EXCHANGE,
                              action="BUY", price_type="MARKET", product=PRODUCT, quantity=qty_x)
        except Exception:
            log.exception("Short-spread entry failed")

    def _flatten():
        log.info("FLAT spread")
        try: client.closeposition(strategy=STRATEGY_NAME)
        except Exception: log.exception("closeposition failed")

    watcher = BarCloseWatcher(client, SYMBOL_Y, EXCHANGE, INTERVAL, on_bar_close,
                              poll_interval_sec=POLL_INTERVAL_SEC, stop_event=stop_event)
    try: watcher.run()
    finally:
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
