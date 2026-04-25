---
name: portfolio-risk
description: Portfolio-level caps - portfolio SL/TP, daily PnL stop/target, max concurrent positions. Works in both modes.
---

# Portfolio-Level Risk

For multi-strategy setups, the `PortfolioRunner` (`core/portfolio_runner.py`) supervises N strategies and enforces global caps. Triggered via `/algo-portfolio <config.yaml>`.

## YAML config

```yaml
capital: 1000000

portfolio_caps:
  portfolio_sl_pct: 0.02         # halt all strategies at -2% capital
  portfolio_tp_pct: 0.03         # halt all strategies at +3% capital
  daily_loss_pct: 0.015          # daily loss limit
  daily_target_pct: 0.025        # daily target
  max_concurrent_positions: 5
  max_symbol_concentration: 0.30 # 30% cap per symbol

strategies:
  - name: ema_sbin
    path: strategies/ema_sbin/strategy.py
    env:
      SYMBOL: SBIN
      INTERVAL: 5m
  - name: rsi_reliance
    path: strategies/rsi_reliance/strategy.py
    env:
      SYMBOL: RELIANCE
      INTERVAL: 15m
```

## How it works (live)

1. **Spawn**: each strategy launches as a subprocess with the parent's env + per-strategy env merged. `MODE` is set to whatever `--mode` the runner was invoked with.
2. **Pump**: each subprocess's stdout streams into the runner's logger with `[strategy_name]` prefix.
3. **Monitor**: every 15 seconds the runner:
   - Calls `client.positionbook()` and sums `pnl` across all positions
   - Compares against caps
4. **Kill switch**: when a cap breaches, runner:
   - Logs `KILL_SWITCH: <reason>`
   - Sends SIGTERM to every child strategy
   - Calls `client.cancelallorder(strategy="PortfolioRunner")` to drop pending orders
   - Calls `client.closeposition(strategy="PortfolioRunner")` to flatten everything
5. **Daily reset**: at 00:00-00:02 IST the runner re-anchors `day_open_realized` so daily caps reset for the new session.

## How it works (backtest)

In backtest mode, each strategy runs its own `vbt.Portfolio.from_signals()` and reports stats independently. To apply portfolio-level caps to a backtest you need to merge the equity curves and check caps post-hoc:

```python
# Extract per-strategy returns
returns_by_strategy = {}
for strat in cfg["strategies"]:
    pf = run_strategy_backtest(strat)
    returns_by_strategy[strat["name"]] = pf.returns()

# Combined equity (equal weight or YAML-defined weights)
combined = pd.concat(returns_by_strategy, axis=1).sum(axis=1)
equity = (1 + combined).cumprod() * cfg["capital"]

# Apply portfolio SL/TP by truncating equity at first breach
sl = cfg["portfolio_caps"]["portfolio_sl_pct"]
breach = equity <= cfg["capital"] * (1 - sl)
if breach.any():
    cutoff = breach.idxmax()
    equity = equity.loc[:cutoff]
```

A canonical implementation lives in `core/portfolio_runner.py` (live path). The backtest path is left as user-extensible because portfolio backtests are nuanced (rebalancing, weights, lookahead).

## Cap definitions

| Cap | Meaning |
|---|---|
| `portfolio_sl_pct` | Hard stop on aggregate P&L; once tripped, all positions flat for the day |
| `portfolio_tp_pct` | Hard target; locks in gains and stops trading |
| `daily_loss_pct` | Same as portfolio_sl_pct but resets at IST midnight |
| `daily_target_pct` | Same as portfolio_tp_pct but resets daily |
| `max_concurrent_positions` | Caps how many positions can be open at once |
| `max_symbol_concentration` | Cap on % of capital in any single symbol |

When multiple caps conflict, the **most restrictive wins** (e.g. portfolio_sl + daily_loss firing on the same threshold yields one halt event).

## What strategies see

Strategies launched under PortfolioRunner are unaware of the runner. They place orders normally with their own `STRATEGY_NAME`. The runner's monitoring is invisible until SIGTERM arrives - then strategies drop into their existing graceful shutdown handlers.

This means individual strategies can be developed and tested standalone, then composed into a portfolio without code changes.

## Strategy-level vs portfolio-level risk

| Layer | Resolution | Owns |
|---|---|---|
| Per-position `RiskManager` | Tick | sl_pct, tp_pct, trail_pct, time_exit_min |
| `PortfolioRunner` | 15 s | portfolio_sl_pct, daily_loss_pct, max_positions |

Per-position handles per-trade risk (the trade went south). Portfolio handles aggregate / day-of risk (multiple losers compound). Both run concurrently - portfolio caps are an outer brake.

## Telegram alerts on cap breach

The runner can be wired to fire Telegram alerts on breach:

```python
# In portfolio_runner.py PortfolioRunner.stop_all (extend):
self.client.telegram(
    username=os.getenv("TELEGRAM_USERNAME", ""),
    message=f"PORTFOLIO HALT: {self.kill_reason}",
)
```

See `logging-and-alerts.md`.
