"""
strategy_loader.py - Dynamic strategy import + parameter patching.

Foundation for /algo-optimize, /algo-walkforward, /algo-robustness, /algo-scan.

Each strategy template is a single file with module-level config (FAST_EMA,
SLOW_EMA, INTERVAL, RISK, etc.). To run a parameter sweep we:
  1. Dynamically load the strategy module
  2. Fetch data once (using its own config)
  3. For each param combo: patch the module's globals, call signals(df)
  4. Build a portfolio with the strategy's cost model + sizing
  5. Collect stats

This keeps the strategy file unchanged - all sweep machinery lives in one
place outside the templates.
"""
import importlib.util
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def load_strategy(strategy_path):
    """
    Import a strategy file as a module without running its main().

    Returns the module object. The caller can read/patch module-level
    constants (SYMBOL, FAST_EMA, RISK, ...) and call mod.signals(df).
    """
    p = Path(strategy_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Strategy not found: {p}")

    # Ensure the strategy can find core/* via the same parent-walk it does
    here = p.parent
    for parent in [here, *here.parents]:
        candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
        if candidate.exists():
            sys.path.insert(0, str(candidate.parent))
            break

    spec = importlib.util.spec_from_file_location(f"strategy_{p.stem}_{id(p)}", p)
    mod = importlib.util.module_from_spec(spec)
    # Sandbox MODE/argparse so import doesn't dispatch run_live/backtest
    import os
    os.environ.setdefault("MODE", "noop")     # not a recognized mode
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Data fetch using strategy's config
# ---------------------------------------------------------------------------

def fetch_data_for_strategy(mod, lookback_days=None):
    """Pull historical data using the strategy module's own SYMBOL/EXCHANGE/INTERVAL/DATA_SOURCE."""
    from openalgo import api
    from core.data_router import fetch_backtest_data

    client = api(api_key=mod.API_KEY, host=mod.API_HOST)
    days = lookback_days or getattr(mod, "LOOKBACK_DAYS", 365 * 2)
    end = datetime.now().date()
    start = end - timedelta(days=days)
    return fetch_backtest_data(
        client, mod.SYMBOL, mod.EXCHANGE, mod.INTERVAL,
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        source=getattr(mod, "DATA_SOURCE", "api"),
    )


def fetch_data_for_symbol(mod, symbol, exchange=None, lookback_days=None):
    """Pull historical data for an arbitrary symbol using the strategy's interval/source."""
    from openalgo import api
    from core.data_router import fetch_backtest_data

    client = api(api_key=mod.API_KEY, host=mod.API_HOST)
    days = lookback_days or getattr(mod, "LOOKBACK_DAYS", 365 * 2)
    end = datetime.now().date()
    start = end - timedelta(days=days)
    return fetch_backtest_data(
        client, symbol, exchange or mod.EXCHANGE, mod.INTERVAL,
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        source=getattr(mod, "DATA_SOURCE", "api"),
    )


# ---------------------------------------------------------------------------
# Run a single backtest with patched parameters
# ---------------------------------------------------------------------------

def run_one_backtest(mod, df, params=None, override_close=None, override_open=None):
    """
    Patch params on `mod`, call mod.signals(df), build a vbt Portfolio with
    the strategy's cost model + sizing config. Returns (pf, stats_dict).

    params: optional dict of {name: value} to patch on mod before signals().
    override_close / override_open: optional Series (e.g. with noise added).
    """
    import vectorbt as vbt
    from core.cost_model import lookup as cost_lookup
    from core.sizing import fixed_fractional_size

    if params:
        for k, v in params.items():
            setattr(mod, k, v)

    # Some strategies have signals(df) -> (entries, exits).
    # ML / pairs strategies may have signals(df, bundle) - skip those for now;
    # the runner can override behaviour for those cases.
    sig_result = mod.signals(df)
    if isinstance(sig_result, tuple) and len(sig_result) == 2:
        entries, exits = sig_result
        long_entries, short_entries = entries, None
    elif isinstance(sig_result, tuple) and len(sig_result) == 3:
        long_entries, short_entries, exits = sig_result
        entries = long_entries
    else:
        raise ValueError(f"Unexpected signals() return shape: {type(sig_result)}")

    # Resolve close / open with optional overrides
    close = override_close if override_close is not None else df["close"]
    if override_open is not None:
        price = override_open.shift(-1)
    else:
        price = df["open"].shift(-1) if "open" in df.columns else close.shift(-1)

    costs = cost_lookup(getattr(mod, "PRODUCT", "MIS"), getattr(mod, "EXCHANGE", "NSE"))
    risk = mod.RISK
    size_pct = fixed_fractional_size(
        getattr(mod, "RISK_PER_TRADE", 0.005),
        risk.sl_pct,
        getattr(mod, "MAX_SIZE_PCT", 0.50),
    )
    lot_size = getattr(mod, "LOT_SIZE", 1)
    interval = getattr(mod, "INTERVAL", "5m")

    kwargs = dict(
        init_cash=getattr(mod, "INIT_CASH", 1_000_000),
        fees=costs.fees, fixed_fees=costs.fixed_fees, slippage=costs.slippage,
        size=size_pct, size_type="percent",
        sl_stop=risk.sl_pct,
        sl_trail=False if risk.trail_pct is None else risk.trail_pct,
        freq=_vbt_freq(interval),
        min_size=lot_size, size_granularity=lot_size,
        price=price,
    )
    if risk.tp_pct is not None:
        kwargs["tp_stop"] = risk.tp_pct

    if short_entries is not None:
        pf = vbt.Portfolio.from_signals(
            close, entries=long_entries, short_entries=short_entries,
            exits=exits, short_exits=exits, **kwargs,
        )
    else:
        pf = vbt.Portfolio.from_signals(close, entries=entries, exits=exits, **kwargs)

    return pf, _extract_stats(pf)


def _extract_stats(pf):
    """Coerce pf.stats() Series into a flat dict with normalized keys."""
    s = pf.stats()
    return {
        "total_return": _safe_float(s.get("Total Return [%]"), 0),
        "sharpe": _safe_float(s.get("Sharpe Ratio"), 0),
        "sortino": _safe_float(s.get("Sortino Ratio"), 0),
        "calmar": _safe_float(s.get("Calmar Ratio"), 0),
        "max_dd": _safe_float(s.get("Max Drawdown [%]"), 0),
        "win_rate": _safe_float(s.get("Win Rate [%]"), 0),
        "trades": int(_safe_float(s.get("Total Trades"), 0)),
        "profit_factor": _safe_float(s.get("Profit Factor"), 0),
        "avg_winning_trade": _safe_float(s.get("Avg Winning Trade [%]"), 0),
        "avg_losing_trade": _safe_float(s.get("Avg Losing Trade [%]"), 0),
        "best_trade": _safe_float(s.get("Best Trade [%]"), 0),
        "worst_trade": _safe_float(s.get("Worst Trade [%]"), 0),
        "expectancy": _safe_float(s.get("Expectancy"), 0),
    }


def _safe_float(x, default=0.0):
    if x is None:
        return float(default)
    try:
        return float(x)
    except (TypeError, ValueError):
        return float(default)


def _vbt_freq(interval):
    return {
        "1m": "1min", "3m": "3min", "5m": "5min", "10m": "10min",
        "15m": "15min", "30m": "30min", "1h": "1H", "D": "1D",
    }.get(interval, "5min")


# ---------------------------------------------------------------------------
# Capture/restore module attributes (for nested patches)
# ---------------------------------------------------------------------------

def snapshot_attrs(mod, names):
    """Save current values of named attrs on mod. Returns dict."""
    return {n: getattr(mod, n, None) for n in names}


def restore_attrs(mod, snap):
    """Restore values from snapshot."""
    for n, v in snap.items():
        setattr(mod, n, v)
