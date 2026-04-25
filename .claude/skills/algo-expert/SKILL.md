---
name: algo-expert
description: OpenAlgo execution expert. Auto-loaded knowledge base for building algorithmic trading strategies that toggle between VectorBT backtest and OpenAlgo live execution. Triggers when user mentions OpenAlgo trading strategies, dual-mode strategies, /python self-hosted strategies, options execution (short straddle, iron condor), portfolio risk caps, or live execution with stop loss / target / trailing stop / risk management.
---

# OpenAlgo Execution Expert

Knowledge base for building **production-grade algorithmic trading strategies** on OpenAlgo. Every strategy is a single Python file that toggles between **backtest mode (VectorBT)** and **live execution mode (OpenAlgo SDK + WebSocket)** via one CLI flag (`--mode backtest|live`) or env var (`MODE=...`).

Strategies are also **upload-ready** for OpenAlgo's self-hosted `/python` strategy host.

## Core principles

1. **One file, two modes.** The same `signals(df)` function feeds both VectorBT (backtest) and the live event loop. Risk thresholds and cost assumptions are honored on both sides.
2. **OpenAlgo for everything broker-side.** Data via `client.history()` and WebSocket. Orders via `client.placeorder()` / `placesmartorder()` / `optionsmultiorder()`. Live vs sandbox is decided in OpenAlgo's UI analyzer toggle - the strategy code never knows.
3. **Indicator library is user's choice** - `openalgo.ta` (default) or `talib`. Specialty indicators (Supertrend, Donchian, Ichimoku, HMA, KAMA) always come from openalgo. See `rules/indicator-libraries.md`.
4. **Three execution types** - `eoc` (end-of-candle MARKET), `limit` (real-time pegged LIMIT), `stop` (broker-side SL-M trigger). User picks at strategy creation. See `rules/execution-types.md`.
5. **Real-world costs and slippage** baked into every backtest (matches `vectorbt-backtesting-skills` 4-segment Indian model). See `rules/transaction-costs.md` and `rules/slippage-handling.md`.
6. **Self-hosted `/python` compatible** - every strategy reads env vars in the canonical priority, traps SIGTERM, logs to stdout. See `rules/self-hosted-strategies.md`.

## When to read which rule

| Reading the user wants... | Load these rule files |
|---|---|
| The big-picture strategy template | `unified-strategy-pattern.md`, `mode-toggle.md`, `execution-types.md` |
| Indicator selection | `indicator-libraries.md` |
| Position sizing (the most important fix) | `position-sizing.md` |
| Preflight checks at startup | `preflight-checks.md` |
| Risk on a single position | `risk-management.md` |
| Portfolio-level risk and daily caps | `portfolio-risk.md` |
| Cost / slippage modeling | `transaction-costs.md`, `slippage-handling.md` |
| Data sources (DuckDB, Historify, API) | `duckdb-data.md` |
| WebSocket and bar-close patterns | `websocket-feeds.md`, `event-loop.md` |
| Order placement idioms | `execution-patterns.md`, `order-constants.md` |
| Options strategies | `options-execution.md` |
| Volatility strategies | `volatility-strategies.md` |
| ML strategies | `ml-strategies.md` |
| Persistent state between restarts | `state-persistence.md` |
| Logging and Telegram alerts | `logging-and-alerts.md` |
| Common production mistakes | `pitfalls.md` |
| Strategy catalog / template selection | `strategy-catalog.md` |
| OpenAlgo `/python` self-hosting | `self-hosted-strategies.md` |
| Symbol formats and lot sizes | `symbol-format.md`, `lot-sizes.md`, `order-constants.md` |
| Full SDK reference | `sdk-reference.md` |

## Production patterns (lifted from OpenAlgo examples)

- **Two-thread live model**: signal poll thread + WS callback thread (from `examples/python/emacrossover_strategy_python.py`). The WS callback NEVER places orders directly - it spawns a worker thread.
- **Bar-close logic uses `iloc[-2]`** not `iloc[-1]` - the last bar in `client.history()` is forming and would cause repaint.
- **Risk exits use `client.placesmartorder(position_size=0)`** to flatten cleanly (from `examples/python/stoploss_target_example.py`).
- **Multi-leg options entry** via `client.optionsmultiorder()` - BUY legs go first for margin efficiency. Per-leg SL via `client.placeorder(price_type="SL")` (from `examples/python/straddle_with_stops.py`).
- **Time-based entries** via `apscheduler.schedulers.background.BackgroundScheduler` with IST cron (from `examples/python/straddle_scheduler.py`).
- **Always fetch spot quote before any options order** - `client.quotes()` first, then `client.optionsorder()` with `offset="ATM"`.

## Anti-patterns (always avoid)

- `asyncio` - the OpenAlgo SDK is synchronous; use `threading` instead
- `df.iloc[-1]` on live data - that's the forming bar; use `iloc[-2]`
- Calling `client.history(start_date=end_date)` - returns 1 candle; always use multi-day lookback
- Placing exit orders directly inside the WS callback - spawn a worker thread
- Hardcoding `exchange="NSE"` when self-hosted - read `OPENALGO_STRATEGY_EXCHANGE` env var instead
- Backtests with `fees=0` and `slippage=0` - the result is fantasy; use the segment-appropriate constants
- Polling `client.get_ltp()` faster than 0.5s - use WS callbacks for real-time

## Reference docs (in OpenAlgo repo)

- SDK: `D:/openalgo-python/openalgo/docs/prompt/openalgo python sdk.md`
- Services: `D:/openalgo-python/openalgo/docs/prompt/services_documentation.md`
- WebSocket protocol: `D:/openalgo-python/openalgo/docs/prompt/websockets-format.md`
- Self-hosted /python: `D:/openalgo-python/openalgo/strategies/README.md`
- Production examples: `D:/openalgo-python/openalgo/examples/python/`
