---
name: algo-portfolio
description: Run multiple strategies under one supervisor with portfolio-level risk caps (portfolio SL/TP, daily PnL limits, max concurrent positions). YAML-driven.
argument-hint: "[config.yaml] [--mode backtest|live]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Launch a multi-strategy portfolio with global risk caps.

## Arguments

- `$0` = path to YAML config. Default: `portfolio.yaml` in current directory
- Pass `--mode backtest` or `--mode live` after the config path

If no config exists, generate a starter `portfolio.yaml`.

## Instructions

1. Read `algo-expert/rules/portfolio-risk.md`.
2. If the config file doesn't exist, generate this starter:

```yaml
# Portfolio Runner Configuration
# See algo-expert/rules/portfolio-risk.md for cap explanations

capital: 1000000

portfolio_caps:
  portfolio_sl_pct: 0.02         # halt all at -2% capital
  portfolio_tp_pct: 0.03         # halt all at +3% capital
  daily_loss_pct: 0.015          # daily loss limit
  daily_target_pct: 0.025        # daily target
  max_concurrent_positions: 5
  max_symbol_concentration: 0.30 # max 30% of capital in one symbol

strategies:
  # List your generated strategies. The runner spawns each as a subprocess.
  - name: ema_sbin
    path: strategies/ema_crossover_SBIN/strategy.py
    env:
      SYMBOL: SBIN
      INTERVAL: 5m
  - name: rsi_reliance
    path: strategies/rsi_RELIANCE/strategy.py
    env:
      SYMBOL: RELIANCE
      INTERVAL: 15m
```

3. Verify each strategy path exists. If not, suggest running `/algo-strategy` to create them first.
4. Generate a runner script `run_portfolio.py` at the project root (or update if exists) that wraps `core/portfolio_runner.py`:

```python
"""Portfolio runner entry point."""
import argparse, logging, os, sys
from pathlib import Path
from dotenv import find_dotenv, load_dotenv

# Add core helpers to path
_HERE = Path(__file__).resolve().parent
for p in [_HERE, *_HERE.parents]:
    c = p / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if c.exists():
        sys.path.insert(0, str(c.parent)); break

from openalgo import api
from core.portfolio_runner import PortfolioRunner

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                    stream=sys.stdout)
load_dotenv(find_dotenv(usecwd=True))

def make_client():
    return api(
        api_key=os.getenv("OPENALGO_API_KEY", ""),
        host=os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"),
    )

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="portfolio.yaml")
    p.add_argument("--mode", choices=["backtest", "live"], default="live")
    args = p.parse_args()

    runner = PortfolioRunner(args.config, client_factory=make_client, mode=args.mode)
    runner.start()
    runner.join()

if __name__ == "__main__":
    main()
```

5. Run it:
   ```bash
   python run_portfolio.py --config portfolio.yaml --mode live
   ```

6. Tell the user:
   - Each strategy runs in its own subprocess (full isolation)
   - SIGTERM is propagated when caps breach OR on Ctrl-C
   - The runner monitors aggregate P&L every 15s via `client.positionbook()`
   - On breach: `cancelallorder` + `closeposition` are called platform-wide
   - Live vs sandbox is decided by OpenAlgo's UI analyzer toggle (same as a single strategy)

## Backtest mode

Aggregate-equity backtest of multiple strategies needs custom merging logic (rebalancing weights, cap timing). The default runner does NOT do this in backtest mode - each child runs its own VectorBT backtest independently. To get a true portfolio backtest, see `algo-expert/rules/portfolio-risk.md` for the merge-equities pattern.

## Cap precedence

When multiple caps fire at once, the most restrictive wins. The kill switch fires once per session - re-running the runner resets daily counters at IST midnight.

## Telegram alerts on cap breach

To wire Telegram alerts when the portfolio halts, set `TELEGRAM_USERNAME` in `.env` and extend `core/portfolio_runner.py`'s `stop_all()` method. See `logging-and-alerts.md`.

## Avoid

- Do not use icons/emojis
- Do not silently increase the cap when it breaches - that defeats the safety net
- Do not run two `PortfolioRunner` processes against the same OpenAlgo instance - they'd both try to flatten on breach and conflict
