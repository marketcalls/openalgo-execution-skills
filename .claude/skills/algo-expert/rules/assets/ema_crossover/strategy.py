"""
EMA Crossover - dual-mode strategy (backtest with VectorBT, live with OpenAlgo SDK).

Single-file design. The same signals() function feeds both VectorBT (backtest)
and the live event loop. Risk thresholds and cost assumptions are honored in
both modes.

Usage:
    # Local backtest
    python strategy.py --mode backtest

    # Local live (sandbox or real - controlled by OpenAlgo UI analyzer toggle)
    python strategy.py --mode live

    # Self-hosted via OpenAlgo /python (env-driven, no CLI flags)
    # The platform sets OPENALGO_STRATEGY_EXCHANGE, OPENALGO_API_KEY, etc.
"""
import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv

# --- Add core to import path (when running locally from generated strategies/ folder) ---
_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent))
        break

from openalgo import api  # noqa: E402

from core.cost_model import lookup as cost_lookup, format_cost_report, SlippageTracker  # noqa: E402
from core.indicator_adapter import get_indicators  # noqa: E402
from core.data_router import (
    fetch_backtest_data, warmup_live_data, BarCloseWatcher,
)  # noqa: E402
from core.risk_manager import RiskManager, RiskConfig, Position  # noqa: E402
from core.sizing import fixed_fractional_size, compute_live_qty  # noqa: E402
from core.preflight import run_preflight, find_existing_open_position  # noqa: E402
from core.state import StrategyState, reconcile_with_broker  # noqa: E402


# ============================================================================
# CONFIG - edit these for your symbol / timeframe / params
# ============================================================================

SYMBOL          = "SBIN"
EXCHANGE        = os.getenv("OPENALGO_STRATEGY_EXCHANGE",
                            os.getenv("EXCHANGE", "NSE"))
INTERVAL        = "5m"           # 1m, 3m, 5m, 10m, 15m, 30m, 1h, D
PRODUCT         = "MIS"          # MIS / CNC / NRML
LOT_SIZE        = 1              # 1 for equity; 65 for NIFTY; etc
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "ema_crossover")

# Data source: "api" | "db" | "duckdb:/path/to/file.duckdb"
DATA_SOURCE     = os.getenv("DATA_SOURCE", "api")

# Position sizing - fixed fractional based on RISK.sl_pct
RISK_PER_TRADE  = 0.005          # 0.5% of capital risked per trade
MAX_SIZE_PCT    = 0.50           # never deploy more than 50% of capital

# Indicator parameters
FAST_EMA        = 10
SLOW_EMA        = 20

# Indicator library (set at strategy creation: "openalgo" or "talib")
INDICATOR_LIB   = "openalgo"

# ============================================================================
# EXECUTION TYPE - "eoc" | "limit" | "stop"
# eoc:    evaluate signals at bar close, MARKET order on next bar
# limit:  pre-place LIMIT orders, modify on tick (use ATR breakout style)
# stop:   broker-side SL/SL-M trigger orders (use opening-range style)
# ============================================================================

EXECUTION_TYPE  = "eoc"

POLL_INTERVAL_SEC     = 15       # eoc: how often to refetch history
LIMIT_OFFSET_PCT      = 0.0005   # limit mode: peg distance from LTP
LIMIT_TIMEOUT_SEC     = 3        # limit: fallback to MARKET after this

# ============================================================================
# RISK CONFIG - per-position. Set values to None to disable.
# ============================================================================

RISK = RiskConfig(
    sl_pct=0.01,           # 1% stop loss
    tp_pct=0.02,           # 2% take profit
    trail_pct=0.008,       # 0.8% trailing stop after profit
    time_exit_min=240,     # 4 hour max hold
)

# ============================================================================
# BACKTEST CONFIG
# ============================================================================

INIT_CASH       = 1_000_000      # Rs 10 lakh
LOOKBACK_DAYS   = 365 * 2        # backtest history window

# ============================================================================
# Logging - stdout only (OpenAlgo /python host captures it)
# ============================================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(STRATEGY_NAME)

# ============================================================================
# Env resolution - HOST_SERVER wins over OPENALGO_HOST per OpenAlgo convention
# ============================================================================

load_dotenv(find_dotenv(usecwd=True))

API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}"
)

# ============================================================================
# Cost model - auto-resolved from PRODUCT + EXCHANGE
# ============================================================================

COSTS = cost_lookup(PRODUCT, EXCHANGE)


# ============================================================================
# SIGNALS - the only piece of strategy logic. Used by both backtest and live.
# ============================================================================

def signals(df):
    """
    Compute entries and exits given a normalized OHLCV DataFrame.
    Returns (entries: bool Series, exits: bool Series).
    """
    ind = get_indicators(INDICATOR_LIB)
    close = df["close"]
    fast = ind.ema(close, FAST_EMA)
    slow = ind.ema(close, SLOW_EMA)

    buy_raw  = ind.crossover(fast, slow)
    sell_raw = ind.crossunder(fast, slow)

    # Clean duplicate signals (always fillna before exrem)
    buy_raw  = pd.Series(buy_raw, index=close.index).fillna(False).astype(bool)
    sell_raw = pd.Series(sell_raw, index=close.index).fillna(False).astype(bool)

    entries = pd.Series(ind.exrem(buy_raw, sell_raw),  index=close.index).fillna(False).astype(bool)
    exits   = pd.Series(ind.exrem(sell_raw, buy_raw), index=close.index).fillna(False).astype(bool)
    return entries, exits


# ============================================================================
# BACKTEST RUNNER (VectorBT)
# ============================================================================

def run_backtest():
    import vectorbt as vbt

    log.info("=" * 70)
    log.info("BACKTEST: %s on %s/%s @ %s", STRATEGY_NAME, SYMBOL, EXCHANGE, INTERVAL)
    log.info("=" * 70)
    log.info("\n%s", format_cost_report(COSTS, INIT_CASH))

    client = api(api_key=API_KEY, host=API_HOST)

    end = datetime.now().date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    df = fetch_backtest_data(
        client, SYMBOL, EXCHANGE, INTERVAL,
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        source=DATA_SOURCE,
    )
    if df is None or len(df) < max(FAST_EMA, SLOW_EMA) + 5:
        log.error("Insufficient bars for backtest (got %d)", 0 if df is None else len(df))
        return

    entries, exits = signals(df)
    close = df["close"]

    # Position sizing: risk_per_trade / sl_pct, capped at max_size_pct
    size_pct = fixed_fractional_size(
        risk_per_trade=RISK_PER_TRADE,
        sl_pct=RISK.sl_pct, max_size=MAX_SIZE_PCT,
    )
    log.info("Position sizing: %.2f%% of equity per trade "
             "(risk=%.2f%% / sl=%.2f%%)",
             size_pct*100, RISK_PER_TRADE*100, (RISK.sl_pct or 0)*100)

    pf = vbt.Portfolio.from_signals(
        close,
        entries=entries,
        exits=exits,
        price=df["open"].shift(-1),     # fill at NEXT bar open (signal at close)
        init_cash=INIT_CASH,
        fees=COSTS.fees,
        fixed_fees=COSTS.fixed_fees,
        slippage=COSTS.slippage,
        size=size_pct,
        size_type="percent",
        sl_stop=RISK.sl_pct,
        tp_stop=RISK.tp_pct,
        sl_trail=False if RISK.trail_pct is None else RISK.trail_pct,
        freq=_vbt_freq(INTERVAL),
        min_size=LOT_SIZE,
        size_granularity=LOT_SIZE,
    )

    stats = pf.stats()
    log.info("\n=== Backtest Stats ===\n%s", stats)

    # Strategy vs Benchmark comparison
    try:
        bench = fetch_backtest_data(
            client, "NIFTY", "NSE_INDEX", INTERVAL,
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
            source="api",
        )
        if bench is not None and len(bench) > 0:
            bench_close = bench["close"].reindex(df.index, method="ffill")
            bench_pf = vbt.Portfolio.from_holding(bench_close, init_cash=INIT_CASH, freq=_vbt_freq(INTERVAL))
            log.info("\n=== Strategy vs NIFTY 50 ===")
            log.info(_format_compare(pf, bench_pf))
    except Exception:
        log.exception("Benchmark comparison failed - continuing")

    # Save trades + equity
    out_dir = Path("backtests") / STRATEGY_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    pf.trades.records_readable.to_csv(out_dir / f"{SYMBOL}_trades.csv", index=False)

    # QuantStats tearsheet (optional)
    try:
        import quantstats as qs
        tearsheet = out_dir / f"{SYMBOL}_tearsheet.html"
        qs.reports.html(pf.returns(), output=str(tearsheet), title=f"{STRATEGY_NAME} - {SYMBOL}")
        log.info("Tearsheet: %s", tearsheet)
    except ImportError:
        log.info("quantstats not installed - skipping tearsheet")
    except Exception:
        log.exception("Tearsheet generation failed - continuing")

    log.info("Trades CSV: %s", out_dir / f"{SYMBOL}_trades.csv")


def _vbt_freq(interval):
    return {
        "1m": "1min", "3m": "3min", "5m": "5min", "10m": "10min",
        "15m": "15min", "30m": "30min", "1h": "1H", "D": "1D",
    }.get(interval, "5min")


def _format_compare(strat_pf, bench_pf):
    s = strat_pf.stats()
    b = bench_pf.stats()
    rows = [
        ("Total Return [%]",    s.get("Total Return [%]"),    b.get("Total Return [%]")),
        ("Sharpe Ratio",         s.get("Sharpe Ratio"),         b.get("Sharpe Ratio")),
        ("Max Drawdown [%]",     s.get("Max Drawdown [%]"),     b.get("Max Drawdown [%]")),
        ("Win Rate [%]",         s.get("Win Rate [%]"),         "n/a"),
        ("Total Trades",         s.get("Total Trades"),         "n/a"),
    ]
    return "\n".join(f"  {k:<22} | strat={_v(v1):>10} | bench={_v(v2):>10}" for k, v1, v2 in rows)


def _v(x):
    if x is None or x == "n/a":
        return "n/a"
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return str(x)


# ============================================================================
# LIVE RUNNER
# ============================================================================

def run_live():
    log.info("=" * 70)
    log.info("LIVE: %s on %s/%s @ %s (execution_type=%s)",
             STRATEGY_NAME, SYMBOL, EXCHANGE, INTERVAL, EXECUTION_TYPE)
    log.info("=" * 70)
    log.info("Cost model: %s | slippage assumption: %.4f%%",
             COSTS.label, COSTS.slippage * 100)

    if EXECUTION_TYPE == "eoc":
        _run_live_eoc()
    elif EXECUTION_TYPE == "limit":
        _run_live_limit()
    elif EXECUTION_TYPE == "stop":
        _run_live_stop()
    else:
        log.error("Unknown EXECUTION_TYPE=%s", EXECUTION_TYPE)


def _run_live_eoc():
    """End-of-candle: evaluate signal at bar close, MARKET order on next bar."""
    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)

    # --- Preflight: broker auth, funds, holiday ---
    try:
        run_preflight(client, symbol=SYMBOL, exchange=EXCHANGE,
                      min_cash=0, expected_exchange_env=EXCHANGE)
    except Exception as e:
        log.error("Preflight failed: %s - aborting", e)
        return

    # --- State persistence (per-strategy SQLite) ---
    state_db = StrategyState(_HERE / "state.db")

    # --- Live qty sizing (capital-aware) ---
    qty = compute_live_qty(client, SYMBOL, EXCHANGE,
                           sl_pct=RISK.sl_pct,
                           risk_per_trade=RISK_PER_TRADE,
                           lot_size=LOT_SIZE, min_qty=LOT_SIZE,
                           max_capital_pct=MAX_SIZE_PCT)
    if qty <= 0:
        log.error("compute_live_qty returned 0 - aborting")
        state_db.close()
        return
    log.info("Live qty: %d (lot_size=%d)", qty, LOT_SIZE)

    client.connect()
    slip = SlippageTracker(assumed_pct=COSTS.slippage)
    state = {"position": None}
    risk_mgr = RiskManager(
        client, STRATEGY_NAME, RISK,
        on_exit_callback=lambda *a: state.update({"position": None}),
        slippage_tracker=slip, state=state_db,
    )

    # --- Reconcile with broker on startup (idempotency) ---
    resumed = reconcile_with_broker(state_db, client, SYMBOL, EXCHANGE)
    if resumed is not None:
        log.info("Resuming open position from state.db: side=%s qty=%d entry=%.2f watermark=%.2f",
                 resumed.side, resumed.qty, resumed.entry_price, resumed.watermark)
        pos = Position(
            symbol=resumed.symbol, exchange=resumed.exchange, side=resumed.side,
            qty=resumed.qty, entry_price=resumed.entry_price,
            entry_time=resumed.entry_time, product=resumed.product,
            strategy=STRATEGY_NAME,
        )
        state["position"] = pos
        risk_mgr.set_position(pos, restore_watermark=resumed.watermark)

    # Warmup
    _ = warmup_live_data(client, SYMBOL, EXCHANGE, INTERVAL, lookback_bars=200,
                         source=DATA_SOURCE)

    def on_bar_close(df):
        entries, exits = signals(df)
        # iloc[-2] = just-closed bar; iloc[-1] = forming bar
        if len(df) < 3:
            return
        new_entry = bool(entries.iloc[-2])
        new_exit  = bool(exits.iloc[-2])
        ltp = float(df["close"].iloc[-2])
        bar_ts = str(df.index[-2])

        # Idempotency: don't re-act on the same signal bar twice
        if state_db.signal_already_acted(STRATEGY_NAME, bar_ts):
            return

        if new_entry and state["position"] is None:
            # Idempotency #2: if a position is already open at the broker, skip
            if find_existing_open_position(client, SYMBOL, EXCHANGE) is not None:
                log.warning("Broker has open position for %s/%s - skipping ENTRY",
                            SYMBOL, EXCHANGE)
                state_db.mark_signal_acted(STRATEGY_NAME, bar_ts)
                return

            log.info("ENTRY signal at %s ltp=%.2f", df.index[-2], ltp)
            response = client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=SYMBOL, exchange=EXCHANGE,
                action="BUY", price_type="MARKET",
                product=PRODUCT, quantity=qty,
            )
            order_id = response.get("orderid") if isinstance(response, dict) else None
            fill = _wait_fill(client, order_id, fallback=ltp) if order_id else ltp
            slip.record(decision_price=ltp, fill_price=fill, qty=qty, side="BUY")
            pos = Position(
                symbol=SYMBOL, exchange=EXCHANGE, side="BUY",
                qty=qty, entry_price=fill, entry_time=time.time(),
                product=PRODUCT, strategy=STRATEGY_NAME,
            )
            state["position"] = pos
            risk_mgr.set_position(pos)
            state_db.mark_signal_acted(STRATEGY_NAME, bar_ts)

        elif new_exit and state["position"] is not None:
            log.info("EXIT signal at %s ltp=%.2f", df.index[-2], ltp)
            try:
                client.placesmartorder(
                    strategy=STRATEGY_NAME,
                    symbol=SYMBOL, exchange=EXCHANGE,
                    action="SELL", price_type="MARKET",
                    product=PRODUCT, quantity=state["position"].qty,
                    position_size=0,
                )
            except Exception:
                log.exception("Exit placement failed")
            risk_mgr.clear_position()
            state["position"] = None
            state_db.mark_signal_acted(STRATEGY_NAME, bar_ts)

    watcher = BarCloseWatcher(
        client, SYMBOL, EXCHANGE, INTERVAL,
        on_bar_close=on_bar_close,
        poll_interval_sec=POLL_INTERVAL_SEC,
        stop_event=stop_event,
    )

    try:
        watcher.run()
    finally:
        risk_mgr.stop()
        try:
            client.disconnect()
        except Exception:
            pass
        state_db.close()
        log.info("\n%s", slip.report())


def _run_live_limit():
    log.warning("EMA crossover does not use 'limit' execution by default. "
                "Switch to atr_breakout template, or override the entry "
                "logic below to peg LIMIT at a level.")
    _run_live_eoc()


def _run_live_stop():
    log.warning("EMA crossover does not use 'stop' execution by default. "
                "Switch to opening_range template, or override.")
    _run_live_eoc()


def _wait_fill(client, order_id, fallback, retries=10, sleep_s=0.5):
    for _ in range(retries):
        try:
            resp = client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            if data.get("order_status") == "complete":
                avg = data.get("average_price") or data.get("price")
                if avg:
                    return float(avg)
        except Exception:
            log.exception("orderstatus poll failed")
        time.sleep(sleep_s)
    return fallback


# ============================================================================
# SIGTERM-safe shutdown (required for OpenAlgo /python self-hosted)
# ============================================================================

stop_event = threading.Event()

def _shutdown_handler(signum, frame):
    log.info("Signal %d received - shutting down gracefully", signum)
    stop_event.set()

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)


# ============================================================================
# Dispatcher
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description=f"{STRATEGY_NAME} - dual-mode strategy")
    parser.add_argument(
        "--mode", choices=["backtest", "live"],
        default=os.getenv("MODE", "live"),
        help="backtest = vectorbt simulation; live = OpenAlgo execution (sandbox/real per UI toggle)",
    )
    args = parser.parse_args()

    if args.mode == "backtest":
        run_backtest()
    else:
        run_live()


if __name__ == "__main__":
    main()
