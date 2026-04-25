"""
sizing.py - Position sizing helpers.

Three sizing methods:
  1. Fixed fractional (default) - risk a fixed % of capital per trade
  2. Volatility targeted        - inverse-ATR sizing for vol-regime adaptation
  3. Live-mode quantity         - converts capital + risk into share/lot count

For backtests, returns a `size_pct` to feed `vbt.Portfolio.from_signals(size=, size_type='percent')`.
For live mode, returns an integer quantity sized against current account funds.
"""
import logging
import math

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backtest sizing
# ---------------------------------------------------------------------------

def fixed_fractional_size(risk_per_trade=0.005, sl_pct=0.01, max_size=0.50):
    """
    size_pct = risk_per_trade / sl_pct, capped at max_size.

    Example:
        risk_per_trade=0.005 (0.5% of capital), sl_pct=0.01 (1% stop)
        -> size_pct = 0.50 (deploy 50% of equity per trade)

    Worst case loss per trade = risk_per_trade (= 0.5% of capital).
    10 consecutive losers -> ~5% drawdown (recoverable).
    """
    if sl_pct is None or sl_pct <= 0:
        log.warning("sl_pct is None/0 - falling back to %.2f", max_size)
        return max_size
    raw = risk_per_trade / sl_pct
    return min(raw, max_size)


def vol_targeted_size(target_vol=0.005, atr_pct=None, max_size=1.0):
    """
    size_pct = target_vol / atr_pct, capped at max_size.

    Use for volatility-regime strategies (atr_breakout, bb_squeeze).
    On a high-vol day positions shrink; on a calm day they grow.
    """
    if atr_pct is None or atr_pct <= 0:
        return max_size
    return min(target_vol / atr_pct, max_size)


# ---------------------------------------------------------------------------
# Live sizing (integer quantity)
# ---------------------------------------------------------------------------

def compute_live_qty(client, symbol, exchange, sl_pct,
                     risk_per_trade=0.005,
                     lot_size=1,
                     min_qty=1,
                     max_capital_pct=0.50):
    """
    Compute integer quantity to place such that:
        max_loss_at_sl <= risk_per_trade * available_cash
        notional       <= max_capital_pct * available_cash

    Both constraints are enforced; the smaller of the two wins.

    For futures/options, pass the lot_size so the result is a multiple of it.

    Returns at least min_qty (or 0 if not even one lot fits).
    """
    try:
        funds_resp = client.funds()
        available = float(funds_resp.get("data", {}).get("availablecash", 0) or 0)
    except Exception:
        log.exception("funds() failed - sizing falls back to min_qty")
        return min_qty

    if available <= 0:
        log.warning("Available cash is %.2f - cannot size", available)
        return 0

    try:
        q = client.quotes(symbol=symbol, exchange=exchange)
        ltp = float(q.get("data", {}).get("ltp", 0) or 0)
    except Exception:
        log.exception("quotes() failed - sizing falls back to min_qty")
        return min_qty

    if ltp <= 0:
        log.warning("LTP is %.2f for %s/%s - cannot size", ltp, symbol, exchange)
        return min_qty

    sl_distance_rs = ltp * (sl_pct or 0.01)
    risk_budget = available * risk_per_trade
    qty_by_risk = int(math.floor(risk_budget / max(sl_distance_rs, 1e-9)))

    notional_cap = available * max_capital_pct
    qty_by_notional = int(math.floor(notional_cap / max(ltp, 1e-9)))

    qty = min(qty_by_risk, qty_by_notional)

    # Round down to lot multiples
    if lot_size > 1:
        qty = (qty // lot_size) * lot_size

    qty = max(qty, min_qty if min_qty * lot_size <= qty_by_notional else 0)

    log.info("Sizing %s/%s: cash=Rs %.0f ltp=%.2f sl=%.2f%% "
             "risk_budget=Rs %.0f notional_cap=Rs %.0f -> qty=%d (lot=%d)",
             symbol, exchange, available, ltp, (sl_pct or 0)*100,
             risk_budget, notional_cap, qty, lot_size)
    return qty
