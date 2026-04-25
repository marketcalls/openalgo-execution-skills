---
name: algo-setup
description: Set up the Python environment for OpenAlgo execution skills - venv, openalgo[indicators], vectorbt, talib, scikit-learn, xgboost. Scaffolds strategies/ folder and .env.
argument-hint: "[python-version]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Set up the local Python environment for building and running OpenAlgo dual-mode trading strategies.

## Arguments

`$0` (optional) = python interpreter name (`python`, `python3`, `python3.12`). Default: `python`.

## What to do

1. **Detect the OS** (Windows / macOS / Linux) via `uname -s` or `os.name`. Use OS-specific install commands for the TA-Lib C library (which `pip install ta-lib` depends on).

2. **Create venv** if not already present:
   ```bash
   python -m venv venv
   ```

3. **Install TA-Lib C library** (required by Python `ta-lib`):
   - macOS: `brew install ta-lib`
   - Ubuntu/Debian: `sudo apt install libta-lib-dev` (may need `sudo apt update`)
   - Windows: `pip install ta-lib` uses pre-built wheels - no C library install needed

4. **Activate venv and install Python packages** from `requirements.txt`:
   ```bash
   # Linux/macOS
   source venv/bin/activate
   pip install -r requirements.txt

   # Windows
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

5. **Scaffold folders**:
   ```
   strategies/
       (empty - generated strategies will land here)
   backtests/
       (empty - generated backtest CSV/HTML output lands here)
   ```

6. **Create `.env`** by copying `.env.sample` and prompt the user to fill in `OPENALGO_API_KEY`:
   ```bash
   cp .env.sample .env
   ```

7. **Verify the install** by running:
   ```python
   from openalgo import api, ta
   import vectorbt as vbt
   import talib
   import sklearn
   import xgboost
   print("OK")
   ```

8. **Print next steps**:
   ```
   Setup complete. Next:
     1. Edit .env and set OPENALGO_API_KEY
     2. Make sure OpenAlgo is running at http://127.0.0.1:5000
     3. Generate a strategy: /algo-strategy ema-crossover SBIN NSE 5m
   ```

## Avoid

- Do not use icons/emojis in output
- Do not auto-run `OPENALGO_API_KEY=...` in the shell - the user pastes it into `.env` manually
- Do not assume `pip` exists outside the venv after step 4 - always activate first

## When the user already has a venv

Skip step 2. Activate the existing venv, run `pip install -r requirements.txt` against it, and continue.

## Failure modes

- **TA-Lib install fails on Linux**: tell the user to run `sudo apt update && sudo apt install build-essential libta-lib-dev`. If that fails (older distros), suggest skipping TA-Lib and using only `INDICATOR_LIB="openalgo"` in strategies.
- **OpenAlgo not running**: setup completes but the verify-install step's `from openalgo import api` works (SDK installs without network). Real connection is verified in `/algo-strategy`.
