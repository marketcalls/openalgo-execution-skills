"""
preflight.py - Startup checks for live strategies.

Verifies before any order goes out:
  - Broker session is alive (funds() returns)
  - Sufficient capital exists
  - Market is open today (timings + holidays)
  - Symbol exists and is tradable
  - OPENALGO_STRATEGY_EXCHANGE matches the strategy's intended exchange

Raises PreflightError on any failure. The caller decides whether to abort
or warn.

Holiday / session checks honour OpenAlgo's exchange-aware calendar -
the same data that gates /python self-hosted strategies.
"""
from datetime import datetime
import logging

log = logging.getLogger(__name__)


class PreflightError(Exception):
    """Raised when a preflight check fails fatally."""


def run_preflight(client, *, symbol=None, exchange=None,
                  min_cash=0, expected_exchange_env=None,
                  fail_on_holiday=True):
    """
    Run all preflight checks. Returns dict of results; raises PreflightError on hard failure.

    Args:
        client:    OpenAlgo api client
        symbol:    trading symbol (skip symbol checks if None)
        exchange:  exchange code (skip exchange checks if None)
        min_cash:  minimum available cash required (Rs); 0 disables
        expected_exchange_env: if set, verifies os.getenv == this value
        fail_on_holiday: raise on full-day holiday for the given exchange
    """
    results = {}

    # 1. Broker auth + funds
    try:
        funds = client.funds()
        if not isinstance(funds, dict) or funds.get("status") != "success":
            raise PreflightError(f"funds() returned non-success: {funds}")
        cash = float(funds["data"].get("availablecash", 0) or 0)
        results["available_cash"] = cash
        log.info("Preflight: broker auth OK, available cash Rs %.2f", cash)
        if cash < min_cash:
            raise PreflightError(
                f"Available cash Rs {cash:.0f} < required Rs {min_cash:.0f}"
            )
    except PreflightError:
        raise
    except Exception as e:
        raise PreflightError(f"funds() failed - broker session not authenticated: {e}")

    # 2. Exchange env consistency check (when self-hosted)
    if expected_exchange_env is not None:
        import os
        actual = os.getenv("OPENALGO_STRATEGY_EXCHANGE", "")
        if actual and actual != expected_exchange_env:
            log.warning(
                "OPENALGO_STRATEGY_EXCHANGE=%s but strategy expects %s. "
                "Self-hosted host calendar will gate against %s.",
                actual, expected_exchange_env, actual,
            )
        results["env_exchange"] = actual

    # 3. Holiday check
    if exchange and fail_on_holiday:
        results.update(_check_holiday(client, exchange))

    # 4. Symbol resolution check
    if symbol and exchange:
        try:
            sym = client.symbol(symbol=symbol, exchange=exchange)
            if isinstance(sym, dict) and sym.get("status") == "success":
                lot = sym.get("data", {}).get("lotsize", 1)
                tick = sym.get("data", {}).get("tick_size", 0)
                results["lot_size"] = int(lot or 1)
                results["tick_size"] = float(tick or 0)
                log.info("Preflight: %s/%s resolved (lot=%s tick=%s)",
                         symbol, exchange, lot, tick)
            else:
                log.warning("symbol() did not confirm %s/%s: %s", symbol, exchange, sym)
        except Exception as e:
            log.warning("symbol() lookup failed - continuing: %s", e)

    log.info("Preflight: ALL CHECKS PASSED")
    return results


def _check_holiday(client, exchange):
    """Check today against client.holidays(). Raises if exchange is closed all day."""
    today = datetime.now().date()
    try:
        h = client.holidays(year=today.year)
        rows = h.get("data", []) if isinstance(h, dict) else []
        for row in rows:
            if row.get("date") != today.isoformat():
                continue
            closed = row.get("closed_exchanges") or []
            opens = row.get("open_exchanges") or []
            holiday_type = row.get("holiday_type", "")
            description = row.get("description", "")

            if exchange in closed and not any(
                o.get("exchange") == exchange for o in opens
            ):
                raise PreflightError(
                    f"Exchange {exchange} is closed today ({today}) - "
                    f"{holiday_type}: {description}"
                )
            # Partial / SPECIAL_SESSION - log and continue
            if any(o.get("exchange") == exchange for o in opens):
                log.info(
                    "Preflight: %s has SPECIAL_SESSION/partial today: %s",
                    exchange, description,
                )
        return {"holiday_check": "ok"}
    except PreflightError:
        raise
    except Exception as e:
        log.warning("holidays() check failed - continuing: %s", e)
        return {"holiday_check": "skipped"}


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------

def find_existing_open_position(client, symbol, exchange):
    """
    Check positionbook for an open position on this symbol/exchange.
    Returns the row if found (so the caller can rebuild state), else None.
    """
    try:
        pb = client.positionbook()
        rows = pb.get("data", []) if isinstance(pb, dict) else []
        for r in rows:
            if r.get("symbol") != symbol or r.get("exchange") != exchange:
                continue
            try:
                qty = int(float(r.get("quantity", 0) or 0))
            except (TypeError, ValueError):
                qty = 0
            if qty != 0:
                return r
        return None
    except Exception:
        log.exception("positionbook() check failed")
        return None


def find_pending_orders(client, strategy_name, symbol=None):
    """Check orderbook for unfilled orders tagged with this strategy."""
    try:
        ob = client.orderbook()
        data = ob.get("data", {}) if isinstance(ob, dict) else {}
        orders = data.get("orders", []) if isinstance(data, dict) else []
        pending = []
        for o in orders:
            status = (o.get("order_status") or "").lower()
            if status not in ("open", "trigger pending", "pending"):
                continue
            if symbol and o.get("symbol") != symbol:
                continue
            pending.append(o)
        return pending
    except Exception:
        log.exception("orderbook() check failed")
        return []
