---
name: state-persistence
description: SQLite-backed state for live strategies - open positions, watermarks, daily PnL, fill history. Survives restarts.
---

# State Persistence

A live strategy may restart mid-session (host SIGTERM, deploy, crash). State must survive so the strategy:
- Knows about open positions placed before the restart
- Continues trailing stops at the previous watermark (not at restart-time price)
- Doesn't re-enter a position that's already open
- Reports cumulative daily PnL correctly

## SQLite per strategy

Each strategy has its own `state.db` in `strategies/<name>/`:

```
strategies/
└── ema_sbin/
    ├── strategy.py
    └── state.db
```

Schema:
```sql
CREATE TABLE position (
    symbol TEXT, exchange TEXT,
    side TEXT, qty INTEGER,
    entry_price REAL, entry_time REAL,
    product TEXT, watermark REAL,
    closed INTEGER DEFAULT 0,
    PRIMARY KEY (symbol, exchange, entry_time)
);

CREATE TABLE fill (
    order_id TEXT PRIMARY KEY,
    symbol TEXT, side TEXT,
    decision_price REAL, fill_price REAL,
    qty INTEGER, ts REAL
);

CREATE TABLE daily_counter (
    date TEXT PRIMARY KEY,        -- YYYY-MM-DD IST
    realized_pnl REAL,
    trade_count INTEGER
);
```

## Implementation

`core/state.py` ships with the `StrategyState` class - already wired into every template. Each strategy auto-creates `strategies/<name>/state.db` on first run.

```python
from core.state import StrategyState, reconcile_with_broker

state_db = StrategyState(_HERE / "state.db")

# Pass to RiskManager so watermark persists:
risk_mgr = RiskManager(client, STRATEGY_NAME, RISK, state=state_db, ...)

# On startup, reconcile saved state with broker reality:
resumed = reconcile_with_broker(state_db, client, SYMBOL, EXCHANGE)
if resumed is not None:
    pos = Position(...)   # rebuild from resumed StoredPosition
    risk_mgr.set_position(pos, restore_watermark=resumed.watermark)

# Idempotency: don't act on the same bar twice
if state_db.signal_already_acted(STRATEGY_NAME, bar_ts):
    return
state_db.mark_signal_acted(STRATEGY_NAME, bar_ts)

# Cleanup on exit
state_db.close()
```

The schema described below is the actual one in `core/state.py`. No need to copy this code into your strategy - the template imports do it.

## Reference schema (in core/state.py)

Not in core by default - add as needed per strategy:

```python
import sqlite3
from pathlib import Path

class StrategyState:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS position (
                symbol TEXT, exchange TEXT,
                side TEXT, qty INTEGER,
                entry_price REAL, entry_time REAL,
                product TEXT, watermark REAL,
                closed INTEGER DEFAULT 0,
                PRIMARY KEY (symbol, exchange, entry_time)
            );
            CREATE TABLE IF NOT EXISTS daily_counter (
                date TEXT PRIMARY KEY,
                realized_pnl REAL DEFAULT 0,
                trade_count INTEGER DEFAULT 0
            );
        """)
        self.conn.commit()

    def save_position(self, pos):
        self.conn.execute(
            "INSERT OR REPLACE INTO position VALUES (?,?,?,?,?,?,?,?,?)",
            (pos.symbol, pos.exchange, pos.side, pos.qty,
             pos.entry_price, pos.entry_time,
             pos.product, pos.watermark, int(pos.closed)),
        )
        self.conn.commit()

    def load_open_positions(self):
        cur = self.conn.execute("SELECT * FROM position WHERE closed=0")
        return cur.fetchall()

    def update_watermark(self, symbol, exchange, entry_time, watermark):
        self.conn.execute(
            "UPDATE position SET watermark=? "
            "WHERE symbol=? AND exchange=? AND entry_time=?",
            (watermark, symbol, exchange, entry_time),
        )
        self.conn.commit()
```

## Reconciliation on startup

Before subscribing to feeds, reconcile recorded state with the broker's `positionbook`:

```python
def reconcile_on_startup(client, state):
    saved = state.load_open_positions()
    pb = client.positionbook()
    broker_open = {(r["symbol"], r["exchange"]): r for r in pb.get("data", [])
                   if int(float(r.get("quantity", 0) or 0)) != 0}

    for row in saved:
        sym, exch = row[0], row[1]
        if (sym, exch) not in broker_open:
            log.warning("Saved position %s/%s no longer open at broker - marking closed", sym, exch)
            state.mark_closed(sym, exch, row[5])

    for (sym, exch), broker_pos in broker_open.items():
        if not state.has_position(sym, exch):
            log.warning("Broker has position %s/%s not in state - rebuilding", sym, exch)
            state.save_position_from_broker(broker_pos)
```

This catches:
- Strategy restarted after exit but state didn't flush - drop the ghost
- Broker reduced position by force-square-off MIS - mark closed
- Position in broker that strategy doesn't know about - rebuild (manual position?)

## Watermark persistence

The trailing stop watermark must survive restarts. Save on every favorable tick:

```python
def on_tick_with_persistence(data):
    ltp = float(data["data"]["ltp"])
    pos = state["position"]
    if pos.side == "BUY" and ltp > pos.watermark:
        pos.watermark = ltp
        state_db.update_watermark(pos.symbol, pos.exchange, pos.entry_time, ltp)
```

If you don't, a restart at LTP=99 (after watermark=110) means the trail trigger doesn't fire even though it should have - you've effectively lost the lock-in.

## Daily counter reset

```python
from datetime import datetime
import pytz

def get_ist_date():
    return datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")

today = get_ist_date()
counter = state_db.get_daily(today)
if counter is None:
    state_db.create_daily(today)
```

Reset triggers when IST date rolls over. Daily PnL stop / target uses this counter.

## When NOT to use SQLite

- Single-day strategies with no overnight state (MIS-only intraday) - in-memory is fine
- Stateless predictors (most ML strategies just need the model) - no state to persist
- Backtest mode - no live state at all

For these, skip the DB layer entirely.

## Concurrency

SQLite under multi-threaded access needs `check_same_thread=False`. Use a `threading.Lock` around writes if your strategy has multiple writer threads (rare - usually only the main thread writes).

The OpenAlgo `/python` host runs each strategy in its own subprocess (full isolation), so cross-process locking is not needed.

## File location

In strategy file:
```python
STATE_DB_PATH = _HERE / "state.db"
```

When uploaded to OpenAlgo `/python`, the working directory is wherever the host runs - prefer absolute paths via `Path(__file__).parent / "state.db"`.

## Backup

State is local to one machine. For cloud deployments, snapshot `state.db` periodically:

```python
import shutil
shutil.copy(STATE_DB_PATH, f"{STATE_DB_PATH}.{int(time.time())}.bak")
```

Or use a managed DB (Postgres) instead of SQLite if you run multi-host.
