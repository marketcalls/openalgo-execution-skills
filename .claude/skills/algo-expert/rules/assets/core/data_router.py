"""
data_router.py - Unified data access for backtest and live modes.

Three data sources supported:
  1. OpenAlgo API           - source="api"     (live broker fetch)
  2. OpenAlgo Historify     - source="db"      (SDK reads stored DuckDB)
  3. Direct DuckDB           - source="duckdb:/path/to/file.duckdb"
                                (auto-detects Historify vs custom format)

Backtest mode:
  - Single-shot fetch via fetch_backtest_data()
  - Returns a normalized DataFrame (datetime index, lowercase OHLCV columns)

Live mode:
  - warmup_live_data()  - last N bars so indicators are valid on bar 1
  - BarCloseWatcher     - polls history(), fires callback once per closed bar
                          (uses iloc[-2] - iloc[-1] is the forming bar)

WebSocket reconnection:
  - reconnect_ws()       - retry loop wrapping client.connect / subscribe
"""
from datetime import datetime, timedelta
import logging
import time

import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_history(df):
    """Coerce data into a sorted DatetimeIndex DataFrame with lowercase OHLCV columns."""
    if df is None or len(df) == 0:
        return df
    df = df.copy()
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    else:
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df.columns = [c.lower() for c in df.columns]
    return df


# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------

def fetch_backtest_data(client, symbol, exchange, interval,
                        start_date, end_date, source="api"):
    """
    Single-shot fetch for backtest mode. Routes to API, Historify (via SDK),
    or direct DuckDB based on `source`.

    source can be:
      - "api"           OpenAlgo live broker fetch
      - "db"            OpenAlgo Historify via SDK (client.history(source="db"))
      - "duckdb:/path"  direct DuckDB file read (skips OpenAlgo entirely)
    """
    log.info("Fetching backtest data: %s %s %s %s..%s (source=%s)",
             symbol, exchange, interval, start_date, end_date, source)

    if source.startswith("duckdb:"):
        return fetch_from_duckdb(
            db_path=source[len("duckdb:"):],
            symbol=symbol, exchange=exchange, interval=interval,
            start_date=start_date, end_date=end_date,
        )

    df = client.history(
        symbol=symbol, exchange=exchange, interval=interval,
        start_date=start_date, end_date=end_date,
        source=source,                    # "api" or "db"
    )
    df = normalize_history(df)
    if df is not None and len(df) > 0:
        log.info("Loaded %d bars from %s to %s", len(df), df.index[0], df.index[-1])
    return df


# ---------------------------------------------------------------------------
# Direct DuckDB readers (Historify auto-detection)
# ---------------------------------------------------------------------------

def fetch_from_duckdb(db_path, symbol, exchange, interval,
                      start_date=None, end_date=None):
    """
    Read OHLCV directly from a DuckDB file. Auto-detects Historify vs custom format.

    Historify schema:
        market_data(symbol, exchange, interval, timestamp(epoch), o,h,l,c,volume,oi)
    Custom OHLCV schema:
        ohlcv(symbol, date, time, open, high, low, close, volume)
    """
    try:
        import duckdb
    except ImportError as e:
        raise ImportError(
            "duckdb not installed - pip install duckdb to use source='duckdb:...'"
        ) from e

    fmt = _detect_duckdb_format(duckdb, db_path)
    if fmt == "historify":
        return _load_historify(duckdb, db_path, symbol, exchange, interval,
                               start_date, end_date)
    if fmt == "custom_ohlcv":
        return _load_custom_ohlcv(duckdb, db_path, symbol,
                                  start_date, end_date)
    raise ValueError(
        f"DuckDB at {db_path} has neither 'market_data' (Historify) "
        f"nor 'ohlcv' table - unable to auto-detect format. "
        f"Inspect with: duckdb.connect(db_path).execute('SHOW TABLES').fetchdf()"
    )


def _detect_duckdb_format(duckdb, db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        tables = con.execute("SHOW TABLES").fetchdf()["name"].tolist()
        if "market_data" in tables:
            cols = con.execute("DESCRIBE market_data").fetchdf()["column_name"].tolist()
            if all(c in cols for c in ["symbol", "exchange", "interval", "timestamp"]):
                return "historify"
        if "ohlcv" in tables:
            return "custom_ohlcv"
        return "unknown"
    finally:
        con.close()


def _load_historify(duckdb, db_path, symbol, exchange, interval,
                    start_date=None, end_date=None):
    """
    Load from Historify. Storage intervals are only '1m' and 'D' - if the user
    asks for '5m', '15m' etc, we read '1m' and resample.
    """
    storage_interval = interval if interval in ("1m", "D") else "1m"
    where_clauses = ["symbol = ?", "exchange = ?", "interval = ?"]
    params = [symbol.upper(), exchange.upper(), storage_interval]

    if start_date:
        where_clauses.append("timestamp >= ?")
        params.append(int(pd.Timestamp(start_date).timestamp()))
    if end_date:
        where_clauses.append("timestamp <= ?")
        params.append(int(pd.Timestamp(end_date).timestamp() + 86400))  # inclusive end

    sql = (
        "SELECT timestamp, open, high, low, close, volume "
        "FROM market_data WHERE " + " AND ".join(where_clauses) +
        " ORDER BY timestamp"
    )
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()

    if df is None or len(df) == 0:
        log.warning("Historify returned 0 bars for %s/%s %s %s..%s",
                    symbol, exchange, interval, start_date, end_date)
        return df

    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    df = df.set_index("datetime").drop(columns=["timestamp"]).sort_index()

    if interval not in ("1m", "D") and storage_interval == "1m":
        df = resample_ohlcv(df, _interval_to_pandas(interval))

    log.info("Loaded %d bars from Historify (%s/%s %s)", len(df), symbol, exchange, interval)
    return df


def _load_custom_ohlcv(duckdb, db_path, symbol, start_date=None, end_date=None):
    where_clauses = ["symbol = ?"]
    params = [symbol]
    if start_date:
        where_clauses.append("date >= ?")
        params.append(str(start_date))
    if end_date:
        where_clauses.append("date <= ?")
        params.append(str(end_date))
    sql = (
        "SELECT date, time, open, high, low, close, volume FROM ohlcv "
        "WHERE " + " AND ".join(where_clauses) + " ORDER BY date, time"
    )
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()

    if df is None or len(df) == 0:
        return df

    df["datetime"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
    df = df.set_index("datetime").drop(columns=["date", "time"]).sort_index()
    log.info("Loaded %d bars from custom DuckDB (%s)", len(df), symbol)
    return df


def resample_ohlcv(df, timeframe="5min"):
    """Resample OHLCV with Indian market alignment (09:15 open)."""
    return df.resample(
        timeframe, origin="start_day", offset="9h15min",
        label="right", closed="right",
    ).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def _interval_to_pandas(interval):
    return {
        "1m": "1min", "3m": "3min", "5m": "5min", "10m": "10min",
        "15m": "15min", "30m": "30min", "1h": "1H", "D": "1D",
    }.get(interval, "1min")


# ---------------------------------------------------------------------------
# Live warmup + bar-close watcher
# ---------------------------------------------------------------------------

def warmup_live_data(client, symbol, exchange, interval, lookback_bars=200,
                     source="api"):
    """Fetch enough history at startup so indicators are valid on bar 1."""
    end = datetime.now().date()
    if interval == "D":
        start = end - timedelta(days=lookback_bars * 2)
    elif interval in ("1h",):
        start = end - timedelta(days=max(60, lookback_bars // 6))
    else:
        start = end - timedelta(days=max(15, lookback_bars // 75))
    df = fetch_backtest_data(
        client, symbol, exchange, interval,
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        source=source,
    )
    if df is None or len(df) < 50:
        log.warning("Warmup returned only %d bars - indicators may NaN initially.",
                    0 if df is None else len(df))
    return df


def poll_for_new_bar(client, symbol, exchange, interval, last_seen_ts,
                     lookback_days=7):
    """
    Re-fetch recent history. Returns (df, new_bar_closed).

    new_bar_closed is True ONLY when the most-recently-closed bar (iloc[-2]) is
    strictly newer than last_seen_ts. The first call (with last_seen_ts=None)
    seeds the timestamp WITHOUT firing a "new bar" event - prevents the
    off-by-one bug where startup re-evaluates an old signal as if it just fired.
    """
    end = datetime.now().date()
    start = end - timedelta(days=lookback_days)
    df = client.history(
        symbol=symbol, exchange=exchange, interval=interval,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        source="api",
    )
    df = normalize_history(df)
    if df is None or len(df) < 2:
        return df, False

    closed_ts = df.index[-2]

    if last_seen_ts is None:
        # First poll - just seed; do NOT fire callback (would re-evaluate
        # the most recent already-closed bar as if it were brand new)
        return df, False

    return df, closed_ts > last_seen_ts


class BarCloseWatcher:
    """
    Polls client.history() at POLL_INTERVAL_SEC, fires the on_bar_close
    callback once per newly-closed bar.

    On startup the first poll seeds last_seen_ts WITHOUT calling the callback.
    The callback fires only when a strictly-newer closed bar appears.
    """

    def __init__(self, client, symbol, exchange, interval,
                 on_bar_close, poll_interval_sec=15, lookback_days=7,
                 stop_event=None):
        self.client = client
        self.symbol = symbol
        self.exchange = exchange
        self.interval = interval
        self.on_bar_close = on_bar_close
        self.poll_interval_sec = poll_interval_sec
        self.lookback_days = lookback_days
        self.stop_event = stop_event
        self.last_seen_ts = None
        self._first_poll = True

    def run(self):
        while self.stop_event is None or not self.stop_event.is_set():
            try:
                df, is_new = poll_for_new_bar(
                    self.client, self.symbol, self.exchange, self.interval,
                    self.last_seen_ts, lookback_days=self.lookback_days,
                )
                if df is not None and len(df) >= 2:
                    if self._first_poll:
                        # Seed without firing - prevents stale-bar replay
                        self.last_seen_ts = df.index[-2]
                        self._first_poll = False
                        log.info("BarCloseWatcher: seeded last_seen_ts=%s "
                                 "(first poll, no signal fired)",
                                 self.last_seen_ts)
                    elif is_new:
                        self.last_seen_ts = df.index[-2]
                        try:
                            self.on_bar_close(df)
                        except Exception:
                            log.exception("on_bar_close raised - continuing")
            except Exception:
                log.exception("poll_for_new_bar failed - retrying after backoff")
                time.sleep(self.poll_interval_sec)
                continue

            if self.stop_event is not None:
                self.stop_event.wait(self.poll_interval_sec)
            else:
                time.sleep(self.poll_interval_sec)


# ---------------------------------------------------------------------------
# WebSocket reconnection wrapper
# ---------------------------------------------------------------------------

def reconnect_ws(client, instruments, on_data_received, mode="ltp",
                 stop_event=None, max_retries=None,
                 backoff_initial=2.0, backoff_max=30.0):
    """
    Connect to OpenAlgo WS and subscribe. On disconnect, retry with backoff.

    Loops until stop_event is set (or max_retries is hit). Use this in place
    of bare client.connect() + subscribe_*() to harden against network blips.
    """
    sub_fn = {
        "ltp":   client.subscribe_ltp,
        "quote": client.subscribe_quote,
        "depth": client.subscribe_depth,
    }.get(mode)
    if sub_fn is None:
        raise ValueError(f"Unknown mode: {mode}")

    backoff = backoff_initial
    attempts = 0
    while stop_event is None or not stop_event.is_set():
        try:
            client.connect()
            sub_fn(instruments, on_data_received=on_data_received)
            log.info("WS connected and subscribed (%s mode, %d instruments)",
                     mode, len(instruments))
            backoff = backoff_initial    # reset on successful connect
            # Block here until disconnected or stop requested
            while stop_event is None or not stop_event.is_set():
                if stop_event is not None:
                    stop_event.wait(1)
                else:
                    time.sleep(1)
            break
        except Exception:
            attempts += 1
            log.exception("WS connect/subscribe failed (attempt %d) - retrying in %.1fs",
                          attempts, backoff)
            if max_retries is not None and attempts >= max_retries:
                log.error("WS retries exhausted (%d) - giving up", max_retries)
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
        finally:
            try:
                if mode == "ltp":   client.unsubscribe_ltp(instruments)
                elif mode == "quote": client.unsubscribe_quote(instruments)
                elif mode == "depth": client.unsubscribe_depth(instruments)
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass
