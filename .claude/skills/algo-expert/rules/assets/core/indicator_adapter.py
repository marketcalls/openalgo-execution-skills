"""
indicator_adapter.py - Routes indicator calls to either openalgo.ta or talib.

Strategy templates pick a library at creation time. They import this module
and call ind.ema(...), ind.rsi(...), etc. Switching libraries is a one-line
change in the strategy file (LIBRARY = "openalgo" -> "talib").

talib doesn't have Supertrend, Donchian, Ichimoku, HMA, KAMA, ZLEMA, ALMA,
VWMA - those always route to openalgo.ta regardless of LIBRARY setting.
"""
import numpy as np
import pandas as pd


def _to_series(arr, index=None):
    if isinstance(arr, pd.Series):
        return arr
    return pd.Series(arr, index=index)


class _OpenAlgoBackend:
    """openalgo.ta backend (default). Fast Numba JIT, 100+ indicators."""

    name = "openalgo"

    def __init__(self):
        from openalgo import ta
        self._ta = ta

    def sma(self, close, period):
        return _to_series(self._ta.sma(close, period), getattr(close, "index", None))

    def ema(self, close, period):
        return _to_series(self._ta.ema(close, period), getattr(close, "index", None))

    def rsi(self, close, period=14):
        return _to_series(self._ta.rsi(close, period), getattr(close, "index", None))

    def macd(self, close, fast=12, slow=26, signal=9):
        macd, sig, hist = self._ta.macd(close, fast, slow, signal)
        idx = getattr(close, "index", None)
        return _to_series(macd, idx), _to_series(sig, idx), _to_series(hist, idx)

    def atr(self, high, low, close, period=14):
        return _to_series(self._ta.atr(high, low, close, period), getattr(close, "index", None))

    def bbands(self, close, period=20, std=2.0):
        upper, mid, lower = self._ta.bbands(close, period, std)
        idx = getattr(close, "index", None)
        return _to_series(upper, idx), _to_series(mid, idx), _to_series(lower, idx)

    def adx(self, high, low, close, period=14):
        return _to_series(self._ta.adx(high, low, close, period), getattr(close, "index", None))

    def stochastic(self, high, low, close, k=14, d=3, smooth=3):
        k_line, d_line = self._ta.stochastic(high, low, close, k, d, smooth)
        idx = getattr(close, "index", None)
        return _to_series(k_line, idx), _to_series(d_line, idx)

    def supertrend(self, high, low, close, period=10, multiplier=3.0):
        st, direction = self._ta.supertrend(high, low, close, period, multiplier)
        idx = getattr(close, "index", None)
        return _to_series(st, idx), _to_series(direction, idx)

    def donchian(self, high, low, period=20):
        upper, middle, lower = self._ta.donchian(high, low, period)
        idx = getattr(high, "index", None)
        return _to_series(upper, idx), _to_series(middle, idx), _to_series(lower, idx)

    def hma(self, close, period):
        return _to_series(self._ta.hma(close, period), getattr(close, "index", None))

    def kama(self, close, period=10):
        return _to_series(self._ta.kama(close, period), getattr(close, "index", None))

    def stdev(self, close, period):
        return _to_series(self._ta.stdev(close, period), getattr(close, "index", None))

    def crossover(self, a, b):
        return self._ta.crossover(a, b)

    def crossunder(self, a, b):
        return self._ta.crossunder(a, b)

    def exrem(self, primary, secondary):
        return self._ta.exrem(primary, secondary)


class _TaLibBackend:
    """talib backend. Standard library, faster than openalgo for some calls.
    Falls back to openalgo for indicators talib doesn't have."""

    name = "talib"

    def __init__(self):
        import talib
        self._tl = talib
        # Lazy fallback only when a missing indicator is requested
        self._fallback = None

    def _fb(self):
        if self._fallback is None:
            self._fallback = _OpenAlgoBackend()
        return self._fallback

    def _arr(self, x):
        if isinstance(x, pd.Series):
            return x.values.astype(np.float64)
        return np.asarray(x, dtype=np.float64)

    def sma(self, close, period):
        out = self._tl.SMA(self._arr(close), timeperiod=period)
        return _to_series(out, getattr(close, "index", None))

    def ema(self, close, period):
        out = self._tl.EMA(self._arr(close), timeperiod=period)
        return _to_series(out, getattr(close, "index", None))

    def rsi(self, close, period=14):
        out = self._tl.RSI(self._arr(close), timeperiod=period)
        return _to_series(out, getattr(close, "index", None))

    def macd(self, close, fast=12, slow=26, signal=9):
        macd, sig, hist = self._tl.MACD(
            self._arr(close), fastperiod=fast, slowperiod=slow, signalperiod=signal
        )
        idx = getattr(close, "index", None)
        return _to_series(macd, idx), _to_series(sig, idx), _to_series(hist, idx)

    def atr(self, high, low, close, period=14):
        out = self._tl.ATR(self._arr(high), self._arr(low), self._arr(close), timeperiod=period)
        return _to_series(out, getattr(close, "index", None))

    def bbands(self, close, period=20, std=2.0):
        upper, mid, lower = self._tl.BBANDS(
            self._arr(close), timeperiod=period, nbdevup=std, nbdevdn=std
        )
        idx = getattr(close, "index", None)
        return _to_series(upper, idx), _to_series(mid, idx), _to_series(lower, idx)

    def adx(self, high, low, close, period=14):
        out = self._tl.ADX(self._arr(high), self._arr(low), self._arr(close), timeperiod=period)
        return _to_series(out, getattr(close, "index", None))

    def stochastic(self, high, low, close, k=14, d=3, smooth=3):
        k_line, d_line = self._tl.STOCH(
            self._arr(high), self._arr(low), self._arr(close),
            fastk_period=k, slowk_period=smooth, slowd_period=d,
        )
        idx = getattr(close, "index", None)
        return _to_series(k_line, idx), _to_series(d_line, idx)

    def stdev(self, close, period):
        out = self._tl.STDDEV(self._arr(close), timeperiod=period, nbdev=1.0)
        return _to_series(out, getattr(close, "index", None))

    # talib doesn't have these - always route to openalgo
    def supertrend(self, *a, **kw):    return self._fb().supertrend(*a, **kw)
    def donchian(self, *a, **kw):       return self._fb().donchian(*a, **kw)
    def hma(self, *a, **kw):            return self._fb().hma(*a, **kw)
    def kama(self, *a, **kw):           return self._fb().kama(*a, **kw)
    def crossover(self, *a, **kw):      return self._fb().crossover(*a, **kw)
    def crossunder(self, *a, **kw):     return self._fb().crossunder(*a, **kw)
    def exrem(self, *a, **kw):          return self._fb().exrem(*a, **kw)


def get_indicators(library="openalgo"):
    """Return an indicator backend. library = 'openalgo' | 'talib'."""
    lib = library.lower().strip()
    if lib == "talib":
        return _TaLibBackend()
    return _OpenAlgoBackend()
