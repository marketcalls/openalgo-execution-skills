"""
Offline trainer for ML XGBoost strategy.

Same feature pipeline as ml_logistic but uses xgboost.XGBClassifier with
walk-forward CV. Saves model + feature schema to a pickle.

Usage:
    python train.py
    python train.py --symbol RELIANCE --interval 15m --lookback-days 730
"""
import argparse, logging, os, pickle, sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import TimeSeriesSplit

import xgboost as xgb

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
log = logging.getLogger("ml_xgb.train")
load_dotenv(find_dotenv(usecwd=True))


def make_features(df, lib="openalgo"):
    """Engineered features. Same shape as ml_logistic for consistency."""
    ind = get_indicators(lib)
    close = df["close"]
    feats = pd.DataFrame(index=df.index)
    feats["ret_1"]  = close.pct_change(1)
    feats["ret_5"]  = close.pct_change(5)
    feats["ret_20"] = close.pct_change(20)
    feats["rsi_14"] = ind.rsi(close, 14)
    ema_fast = ind.ema(close, 12)
    ema_slow = ind.ema(close, 26)
    feats["ema_diff"] = (ema_fast - ema_slow) / close
    macd_line, sig_line, hist = ind.macd(close, 12, 26, 9)
    feats["macd_hist"] = hist
    atr = ind.atr(df["high"], df["low"], close, 14)
    feats["atr_pct"] = atr / close
    upper, mid, lower = ind.bbands(close, 20, 2.0)
    feats["bb_pos"] = (close - lower) / (upper - lower)
    if "volume" in df.columns:
        v = df["volume"].astype(float)
        feats["vol_z"] = (v - v.rolling(20).mean()) / v.rolling(20).std()
    else:
        feats["vol_z"] = 0.0

    # Triple-barrier label: looks forward LOOKAHEAD bars, labels 1 if upper
    # ATR-band hit first, 0 if lower, drop if neither (avoids noisy boundary).
    LOOKAHEAD = 8
    ATR_MULT  = 1.0
    high = df["high"]; low = df["low"]
    atr14 = ind.atr(high, low, close, 14)
    label = pd.Series(np.nan, index=close.index)
    c = close.values; h = high.values; l = low.values; a = atr14.values
    n = len(c)
    for i in range(n - LOOKAHEAD):
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        up = c[i] + ATR_MULT * a[i]; dn = c[i] - ATR_MULT * a[i]
        for j in range(1, LOOKAHEAD + 1):
            if h[i + j] >= up:
                label.iloc[i] = 1; break
            if l[i + j] <= dn:
                label.iloc[i] = 0; break
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
    p.add_argument("--out", default=None)
    args = p.parse_args()

    api_key  = os.getenv("OPENALGO_API_KEY", "")
    api_host = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
    client = api(api_key=api_key, host=api_host)

    end = datetime.now().date(); start = end - timedelta(days=args.lookback_days)
    log.info("Fetching %s %s %s %s..%s", args.symbol, args.exchange, args.interval, start, end)
    df = fetch_backtest_data(client, args.symbol, args.exchange, args.interval,
                             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if df is None or len(df) < 300:
        log.error("Not enough data (%d bars)", 0 if df is None else len(df)); sys.exit(1)

    X, y = make_features(df, lib=args.lib)
    log.info("Built %d feature rows, %d features", len(X), X.shape[1])

    tss = TimeSeriesSplit(n_splits=5)
    accs = []
    for fold, (train_idx, test_idx) in enumerate(tss.split(X)):
        clf = xgb.XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            objective="binary:logistic",
            eval_metric="logloss",
            n_jobs=-1, tree_method="hist", random_state=42,
        )
        clf.fit(X.iloc[train_idx], y.iloc[train_idx])
        pred = clf.predict(X.iloc[test_idx])
        acc = accuracy_score(y.iloc[test_idx], pred)
        log.info("Fold %d: acc=%.4f (n=%d)", fold + 1, acc, len(test_idx))
        accs.append(acc)
    log.info("Walk-forward avg accuracy: %.4f", np.mean(accs))

    final = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        objective="binary:logistic", eval_metric="logloss",
        n_jobs=-1, tree_method="hist", random_state=42,
    )
    final.fit(X, y)
    log.info("In-sample report:\n%s", classification_report(y, final.predict(X)))

    out = Path(args.out) if args.out else _HERE / f"{args.symbol}_xgb.pkl"
    with open(out, "wb") as f:
        pickle.dump({
            "model": final,
            "features": list(X.columns),
            "symbol": args.symbol, "exchange": args.exchange, "interval": args.interval,
            "trained_at": datetime.utcnow().isoformat(),
            "walk_forward_acc": float(np.mean(accs)),
            "lib": args.lib,
        }, f)
    log.info("Saved model: %s", out)


if __name__ == "__main__":
    main()
