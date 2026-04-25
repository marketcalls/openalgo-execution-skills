---
name: ml-strategies
description: ML-driven strategies - feature engineering, walk-forward training, sklearn / XGBoost. Same predict() feeds backtest and live.
---

# ML Strategies

Two templates: `ml_logistic` (sklearn LogisticRegression pipeline) and `ml_xgb` (XGBoost classifier). Both follow the same offline-train + online-predict workflow.

## Workflow

```
1. python train.py --symbol RELIANCE          # offline fit, saves <symbol>_<model>.pkl
2. python strategy.py --mode backtest          # vectorbt backtest using the saved model
3. python strategy.py --mode live              # live execution
```

Re-train periodically (weekly or after material drift). Re-using a stale model on live data is the most common ML strategy failure mode.

## File layout

```
ml_logistic/
├── train.py              # offline fitter
├── strategy.py           # backtest + live runner
└── RELIANCE_logistic.pkl # generated artefact
```

The same `make_features(df, lib)` function lives in `train.py` and is imported by `strategy.py`. This keeps train-vs-predict feature parity guaranteed (no chance of train/predict skew).

## Feature engineering

```python
def make_features(df, lib="openalgo"):
    ind = get_indicators(lib)
    feats = pd.DataFrame(index=df.index)
    feats["ret_1"]    = close.pct_change(1)
    feats["ret_5"]    = close.pct_change(5)
    feats["ret_20"]   = close.pct_change(20)
    feats["rsi_14"]   = ind.rsi(close, 14)
    ema12 = ind.ema(close, 12); ema26 = ind.ema(close, 26)
    feats["ema_diff"] = (ema12 - ema26) / close
    feats["atr_pct"]  = ind.atr(high, low, close, 14) / close
    feats["vol_z"]    = (volume - vol_ma) / vol_std
    # XGB template adds: macd_hist, bb_pos, etc.

    label = (close.pct_change().shift(-1) > 0).astype(int)
    feats = feats.dropna(); label = label.loc[feats.index]
    return feats, label
```

Things that work as features:
- Returns at multiple horizons (1, 5, 20 bars)
- Momentum (RSI, MACD histogram)
- Trend (EMA differences, normalized by close)
- Volatility (ATR/close)
- Volume z-score
- Time-of-day one-hot for intraday (extension)
- Multi-asset features (SPY return, sector return, vix change)

Avoid:
- Future-leaking features (any indicator computed using future bars)
- Highly correlated features (regularize or drop)
- Raw prices (non-stationary; use returns or normalized)

## Label

Default: sign of next-bar return (binary classification, 0/1).

Alternatives:
- Triple-barrier: label = "did SL or TP hit first within N bars?" - reflects risk-aware classification
- Multi-bar return sign: `(close.shift(-N) > close).astype(int)` - longer prediction horizon
- Quantile labels: top quartile = 1, bottom = 0, middle = drop - sharpens the signal

## Walk-forward CV (in `train.py`)

```python
from sklearn.model_selection import TimeSeriesSplit
tss = TimeSeriesSplit(n_splits=5)
accs = []
for train_idx, test_idx in tss.split(X):
    pipeline.fit(X.iloc[train_idx], y.iloc[train_idx])
    pred = pipeline.predict(X.iloc[test_idx])
    accs.append(accuracy_score(y.iloc[test_idx], pred))
log.info("Walk-forward avg acc: %.4f", np.mean(accs))
```

Walk-forward CV respects time order - never train on future data. Average accuracy across folds is your honest out-of-sample estimate.

If walk-forward acc is below 53%, the model isn't learning much. Below 50% means features are anti-predictive (consider sign-flipping the signal as a contrarian strategy).

## Probability threshold

Models output `predict_proba(X)[:,1]` = P(class=1). Don't enter on every probability spike - require a confidence threshold:

```python
PROB_THRESHOLD = 0.55           # only act when P(up) >= 0.55
prev = proba_full.shift(1)
entries = ((proba_full >= PROB_THRESHOLD) & (prev < PROB_THRESHOLD))
exits   = ((proba_full < (1 - PROB_THRESHOLD)) & (prev >= (1 - PROB_THRESHOLD)))
```

Higher thresholds = fewer trades, higher win rate (usually). Tune via backtest.

## Backtest mode

`strategy.py --mode backtest`:
1. Loads the saved pickle
2. Fetches history for the test symbol
3. Calls `signals(df, bundle)` which calls the model's `predict_proba`
4. Pipes entries/exits into VectorBT's `from_signals` with cost model + RISK
5. Prints stats and saves trades CSV

This is honest in-sample evaluation if you backtest on the same period the model was trained on - useful for sanity but not for predicting live performance. To predict live performance:
- Train on `[t-N, t-1]`
- Backtest on `[t, t+M]` where t is after train cutoff
- Or use the walk-forward acc from `train.py` as your out-of-sample estimate

## Live mode

Same `signals()` function feeds `BarCloseWatcher` → `placeorder` → `RiskManager`. The model is loaded once at startup; predictions happen on each bar close.

## Pitfalls

- **Overfitting**: train accuracy 70%, walk-forward 51% = overfit. Reduce features, tune `C` (logistic) or `max_depth` (xgb)
- **Train-predict skew**: features computed differently in train vs predict = bug. Mitigated by sharing `make_features()`
- **Stale models**: don't trust a 6-month-old model in live trading. Retrain weekly
- **Lookahead in features**: any feature using `df["close"].shift(-1)` is forbidden in real-time
- **Class imbalance**: if 60% of bars are up moves, a "always predict up" model gets 60% acc. Stratify the label or use class weights
- **Tiny edge after costs**: 53% accuracy with 0.4% round-trip cost = barely break-even. Demand higher edge or lower cost segment

## Extending

- Add LSTM / Transformer (pulls torch, ~500 MB install) - intentionally not in default templates
- Add reinforcement learning (PPO via stable-baselines3) - too brittle for a template
- Add ensemble of XGB + LogReg - vote on signal; reduces model risk
