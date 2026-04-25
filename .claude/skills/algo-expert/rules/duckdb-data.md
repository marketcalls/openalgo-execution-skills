---
name: duckdb-data
description: Three data sources for backtest - OpenAlgo API live, OpenAlgo Historify (DuckDB via SDK), or direct DuckDB file read. Auto-detects Historify vs custom OHLCV format.
---

# DuckDB and Direct Broker Data

Backtests accept three data sources via the `DATA_SOURCE` config in every strategy:

```python
DATA_SOURCE = os.getenv("DATA_SOURCE", "api")
```

| Value | What it does |
|---|---|
| `"api"` | OpenAlgo REST live broker fetch (default; data flows from broker) |
| `"db"` | OpenAlgo's stored Historify DuckDB via the SDK (`client.history(source="db")`) |
| `"duckdb:/path/to/file.duckdb"` | Direct DuckDB read - bypasses OpenAlgo entirely |

## When to use which

| Use case | Recommended source |
|---|---|
| Just-now backtest of recent strategy | `api` |
| Reproducible backtest using your stored history | `db` (Historify) |
| Backtest with vendor data (Kite, Algotest) you've ingested | `duckdb:/...` |
| Backtest WITHOUT OpenAlgo running | `duckdb:/...` (auto-detects format) |
| Long backtest covering 5+ years (broker rate limits) | `db` or `duckdb:/...` |

## Custom DuckDB format

Schema:
```sql
CREATE TABLE ohlcv (
    symbol  VARCHAR,
    date    VARCHAR,
    time    VARCHAR,
    open    DOUBLE,
    high    DOUBLE,
    low     DOUBLE,
    close   DOUBLE,
    volume  BIGINT
);
```

The data router auto-detects this when the DuckDB file has an `ohlcv` table with these columns.

## Historify DuckDB format

OpenAlgo's `db/historify.duckdb` schema:
```sql
CREATE TABLE market_data (
    symbol     VARCHAR,
    exchange   VARCHAR,
    interval   VARCHAR,        -- '1m' or 'D' (storage intervals)
    timestamp  BIGINT,         -- Unix epoch seconds
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     BIGINT,
    oi         BIGINT,
    PRIMARY KEY (symbol, exchange, interval, timestamp)
);
```

Auto-detected when `market_data` table is present with `symbol+exchange+interval+timestamp` columns.

Storage intervals are physically only `1m` and `D`. When you ask for `5m`, `15m`, etc., the data router fetches `1m` and resamples with Indian market alignment (09:15 origin):

```python
df.resample("5min", origin="start_day", offset="9h15min",
            label="right", closed="right").agg(...)
```

## Direct broker fetch (`source="api"`)

Each strategy's `fetch_backtest_data()` call routes through `client.history(source="api")` which proxies to the broker's historical data API. Subject to:
- Broker rate limits (typically 30-50 req/min for historical)
- Broker max date range (often 30-90 days per call for intraday intervals)
- The broker session must be active (preflight catches this)

For long backtests (3+ years of intraday), prefer `db` or `duckdb:/...` to avoid rate limits.

## OpenAlgo Historify (`source="db"`)

Reads from `db/historify.duckdb` via the SDK. Requires you've populated Historify previously by running an "Ingest" job in OpenAlgo's UI. Fast (no broker round-trip), reproducible.

## Direct DuckDB (`source="duckdb:/path"`)

```python
DATA_SOURCE = "duckdb:/path/to/historify.duckdb"
# OR for a custom file:
DATA_SOURCE = "duckdb:/data/my_market.duckdb"
```

The path is the colon-separated suffix. Works without OpenAlgo running - useful for offline analysis or notebooks.

```python
# Inspect a DuckDB file before pointing the strategy at it:
import duckdb
con = duckdb.connect(path, read_only=True)
print(con.execute("SHOW TABLES").fetchdf())
print(con.execute("DESCRIBE market_data").fetchdf())   # or DESCRIBE ohlcv
```

## Setting the source

Three ways:
1. **Edit the strategy file**: `DATA_SOURCE = "duckdb:/path"` at the top
2. **Set env var**: `export DATA_SOURCE=duckdb:/path` (or in `.env`)
3. **Per-run via /python upload form**: add a `DATA_SOURCE` parameter

## Live mode is unaffected

Live mode always uses `client.history(source="api")` for warmup and bar-close polling. The `DATA_SOURCE` config only affects backtests. WebSocket feeds always come from OpenAlgo regardless.

## Don't use a stale DuckDB for live decisions

If `DATA_SOURCE` points to a DuckDB file that hasn't been refreshed in days, your backtest will show old data and current strategy decisions are off. Refresh Historify or your custom DuckDB before each backtest run.

## Cross-reference

This pattern is borrowed from `vectorbt-backtesting-skills/rules/duckdb-data.md`. Both packs use the same auto-detection logic so a DuckDB file works in either pack.
