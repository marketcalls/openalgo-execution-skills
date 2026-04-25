"""
Offline trainer for ML Logistic Regression strategy.

Pulls historical OHLCV via OpenAlgo, computes features, fits a
LogisticRegression to predict next-bar return sign, saves the pickle.

Usage:
    python train.py
    python train.py --symbol RELIANCE --interval 15m --lookback-days 365
"""
import argparse, logging, os, pickle, sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent)); break

from openalgo import api  # noqa: E402
from core.indicator_adapter import get_indicators  # noqa: E402
from core.data_router import fetch_backtest_data  # noqa: E402

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("ml_logistic.train")
load_dotenv(find_dotenv(usecwd=True))


def make_features(df, lib="openalgo"):
    """Engineered features from OHLCV. Returns (X DataFrame, y Series)."""
    ind = get_indicators(lib)
    close = df["close"]
    feats = pd.DataFrame(index=df.index)

    # Returns
    feats["ret_1"]  = close.pct_change(1)
    feats["ret_5"]  = close.pct_change(5)
    feats["ret_20"] = close.pct_change(20)

    # Momentum
    feats["rsi_14"] = ind.rsi(close, 14)

    # Trend
    ema_fast = ind.ema(close, 12)
    ema_slow = ind.ema(close, 26)
    feats["ema_diff"] = (ema_fast - ema_slow) / close

    # Volatility
    atr = ind.atr(df["high"], df["low"], close, 14)
    feats["atr_pct"] = atr / close

    # Volume z-score
    if "volume" in df.columns:
        v = df["volume"].astype(float)
        feats["vol_z"] = (v - v.rolling(20).mean()) / v.rolling(20).std()
    else:
        feats["vol_z"] = 0.0

    # Label: triple-barrier (more stable than next-bar sign)
    # For each bar, look forward up to LOOKAHEAD bars and label:
    #   1 if upper barrier (close + atr_mult*ATR) hit first
    #   0 if lower barrier (close - atr_mult*ATR) hit first
    #   drop if neither hits within LOOKAHEAD (uncertain)
    LOOKAHEAD  = 8       # bars to look forward
    ATR_MULT   = 1.0     # barrier width in ATR multiples
    high = df["high"]; low = df["low"]; atr14 = ind.atr(high, low, close, 14)

    label = pd.Series(np.nan, index=close.index)
    closes_arr = close.values
    highs_arr  = high.values
    lows_arr   = low.values
    atr_arr    = atr14.values
    n = len(closes_arr)
    for i in range(n - LOOKAHEAD):
        if np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        upper = closes_arr[i] + ATR_MULT * atr_arr[i]
        lower = closes_arr[i] - ATR_MULT * atr_arr[i]
        for j in range(1, LOOKAHEAD + 1):
            if highs_arr[i + j] >= upper:
                label.iloc[i] = 1; break
            if lows_arr[i + j] <= lower:
                label.iloc[i] = 0; break

    # Drop rows with NaN feature OR unlabeled barrier outcome
    feats = feats.dropna()
    label = label.loc[feats.index].dropna().astype(int)
    feats = feats.loc[label.index]
    return feats, label


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="RELIANCE")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--interval", default="15m")
    p.add_argument("--lookback-days", type=int, default=365 * 2)
    p.add_argument("--lib", default="openalgo")
    p.add_argument("--out", default=None, help="Output pickle path")
    args = p.parse_args()

    api_key  = os.getenv("OPENALGO_API_KEY", "")
    api_host = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
    client = api(api_key=api_key, host=api_host)

    end = datetime.now().date(); start = end - timedelta(days=args.lookback_days)
    log.info("Fetching %s %s %s %s..%s", args.symbol, args.exchange, args.interval, start, end)
    df = fetch_backtest_data(client, args.symbol, args.exchange, args.interval,
                             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if df is None or len(df) < 200:
        log.error("Not enough data (%d bars)", 0 if df is None else len(df)); sys.exit(1)

    X, y = make_features(df, lib=args.lib)
    log.info("Built %d feature rows, %d features", len(X), X.shape[1])

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, C=1.0)),
    ])

    # Walk-forward CV
    tss = TimeSeriesSplit(n_splits=5)
    accs = []
    for fold, (train_idx, test_idx) in enumerate(tss.split(X)):
        pipeline.fit(X.iloc[train_idx], y.iloc[train_idx])
        pred = pipeline.predict(X.iloc[test_idx])
        acc = accuracy_score(y.iloc[test_idx], pred)
        log.info("Fold %d: acc=%.4f (n=%d)", fold + 1, acc, len(test_idx))
        accs.append(acc)
    log.info("Walk-forward avg accuracy: %.4f", np.mean(accs))

    # Final fit on all data
    pipeline.fit(X, y)
    log.info("In-sample report:\n%s", classification_report(y, pipeline.predict(X)))

    out = Path(args.out) if args.out else _HERE / f"{args.symbol}_logistic.pkl"
    with open(out, "wb") as f:
        pickle.dump({
            "pipeline": pipeline,
            "features": list(X.columns),
            "symbol": args.symbol, "exchange": args.exchange, "interval": args.interval,
            "trained_at": datetime.utcnow().isoformat(),
            "walk_forward_acc": float(np.mean(accs)),
            "lib": args.lib,
        }, f)
    log.info("Saved model: %s", out)


if __name__ == "__main__":
    main()
