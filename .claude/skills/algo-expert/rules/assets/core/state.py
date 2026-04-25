"""
state.py - SQLite-backed strategy state.

Per-strategy local DB at strategies/<name>/state.db. Tables:
  - position:       open positions with watermark, entry_time, qty
  - daily_counter:  date-keyed cumulative realized PnL and trade count
  - signal_marker:  last bar_ts for which a signal was acted on (idempotency)
  - fill:           fill history (decision_price, fill_price for slippage)

Use cases:
  - Restart-safe trailing watermark
  - Restart-safe time_exit_min anchored at original entry
  - Idempotency: don't re-enter the same bar's signal twice
  - Realized PnL aggregation independent of broker reset
"""
from dataclasses import dataclass
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class StoredPosition:
    symbol: str
    exchange: str
    side: str                # "BUY" / "SELL"
    qty: int
    entry_price: float
    entry_time: float        # epoch seconds
    product: str
    watermark: float
    closed: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS position (
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_time  REAL NOT NULL,
    product     TEXT NOT NULL,
    watermark   REAL NOT NULL,
    closed      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, exchange, entry_time)
);

CREATE TABLE IF NOT EXISTS daily_counter (
    date        TEXT PRIMARY KEY,
    realized_pnl REAL NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signal_marker (
    key       TEXT PRIMARY KEY,
    bar_ts    TEXT NOT NULL,
    placed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fill (
    order_id        TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    decision_price  REAL,
    fill_price      REAL,
    ts              REAL NOT NULL
);
"""


class StrategyState:
    """SQLite-backed state for a single strategy. Thread-safe."""

    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    # --- Positions ---------------------------------------------------------

    def save_position(self, pos):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO position VALUES (?,?,?,?,?,?,?,?,?)",
                (pos.symbol, pos.exchange, pos.side, int(pos.qty),
                 float(pos.entry_price), float(pos.entry_time),
                 pos.product, float(pos.watermark), int(pos.closed)),
            )
            self.conn.commit()

    def update_watermark(self, symbol, exchange, entry_time, watermark):
        with self._lock:
            self.conn.execute(
                "UPDATE position SET watermark=? "
                "WHERE symbol=? AND exchange=? AND entry_time=?",
                (float(watermark), symbol, exchange, float(entry_time)),
            )
            self.conn.commit()

    def mark_closed(self, symbol, exchange, entry_time):
        with self._lock:
            self.conn.execute(
                "UPDATE position SET closed=1 "
                "WHERE symbol=? AND exchange=? AND entry_time=?",
                (symbol, exchange, float(entry_time)),
            )
            self.conn.commit()

    def load_open_positions(self):
        with self._lock:
            cur = self.conn.execute(
                "SELECT symbol, exchange, side, qty, entry_price, entry_time, "
                "product, watermark, closed FROM position WHERE closed=0"
            )
            rows = cur.fetchall()
        return [StoredPosition(*r[:8], closed=bool(r[8])) for r in rows]

    def has_open_position(self, symbol, exchange):
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM position WHERE symbol=? AND exchange=? AND closed=0 LIMIT 1",
                (symbol, exchange),
            )
            return cur.fetchone() is not None

    # --- Idempotency markers ----------------------------------------------

    def signal_already_acted(self, key, bar_ts):
        """Return True if we already acted on this bar's signal."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT bar_ts FROM signal_marker WHERE key=?", (key,),
            )
            row = cur.fetchone()
            return row is not None and row[0] == str(bar_ts)

    def mark_signal_acted(self, key, bar_ts):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO signal_marker VALUES (?,?,?)",
                (key, str(bar_ts), time.time()),
            )
            self.conn.commit()

    # --- Daily counter ------------------------------------------------------

    def get_daily(self, date_str):
        with self._lock:
            cur = self.conn.execute(
                "SELECT realized_pnl, trade_count FROM daily_counter WHERE date=?",
                (date_str,),
            )
            row = cur.fetchone()
        if row is None:
            return {"realized_pnl": 0.0, "trade_count": 0}
        return {"realized_pnl": float(row[0]), "trade_count": int(row[1])}

    def update_daily(self, date_str, pnl_delta, trades_delta=1):
        with self._lock:
            self.conn.execute(
                "INSERT INTO daily_counter (date, realized_pnl, trade_count) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET "
                "  realized_pnl = realized_pnl + excluded.realized_pnl, "
                "  trade_count  = trade_count  + excluded.trade_count",
                (date_str, float(pnl_delta), int(trades_delta)),
            )
            self.conn.commit()

    # --- Fills --------------------------------------------------------------

    def record_fill(self, order_id, symbol, side, qty,
                    decision_price=None, fill_price=None):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO fill VALUES (?,?,?,?,?,?,?)",
                (str(order_id), symbol, side, int(qty),
                 None if decision_price is None else float(decision_price),
                 None if fill_price is None else float(fill_price),
                 time.time()),
            )
            self.conn.commit()

    # --- Lifecycle ----------------------------------------------------------

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Reconciliation helper
# ---------------------------------------------------------------------------

def reconcile_with_broker(state, client, symbol, exchange):
    """
    On startup, sync local state with the broker's positionbook.

    Detects:
      - Saved position no longer at broker (was force-squared) -> mark closed
      - Broker has position not in state (manual/external) -> log warning

    Returns the active StoredPosition (if any) ready to be re-armed in the risk manager.
    """
    saved_open = [p for p in state.load_open_positions()
                  if p.symbol == symbol and p.exchange == exchange]
    try:
        pb = client.positionbook()
        rows = pb.get("data", []) if isinstance(pb, dict) else []
    except Exception:
        log.exception("positionbook() failed during reconcile")
        return saved_open[0] if saved_open else None

    broker_open = [
        r for r in rows
        if r.get("symbol") == symbol and r.get("exchange") == exchange
        and int(float(r.get("quantity", 0) or 0)) != 0
    ]

    if saved_open and not broker_open:
        log.warning("Reconcile: saved position %s/%s not at broker - marking closed",
                    symbol, exchange)
        for p in saved_open:
            state.mark_closed(p.symbol, p.exchange, p.entry_time)
        return None

    if not saved_open and broker_open:
        log.warning("Reconcile: broker has position %s/%s not in state - "
                    "manual review recommended", symbol, exchange)
        # Don't auto-rebuild - we don't know the watermark or entry_time precisely
        return None

    if saved_open and broker_open:
        log.info("Reconcile: saved + broker positions match, resuming with stored watermark")
        return saved_open[0]

    return None
