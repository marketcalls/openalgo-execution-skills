"""
Short Straddle - options EXECUTION-ONLY strategy.

Sells ATM CE + ATM PE at a scheduled time. Each leg has its own broker-side
SL trigger (price_type="SL") at SL_MULTIPLIER * entry_premium.

Backtest mode is not supported - options backtesting is intentionally out of
scope for this skill pack (options pricing changes intraday in ways that
historical OHLCV backtests do not capture well).

Pattern lifted from OpenAlgo examples/python/straddle_with_stops.py and
straddle_scheduler.py.
"""
import argparse, logging, os, signal, sys, threading, time
from datetime import datetime, time as dtime
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
import pytz

_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent)); break

from openalgo import api  # noqa: E402
from core.cost_model import OPT_NRML, format_cost_report, SlippageTracker  # noqa: E402

# === Config ===
UNDERLYING      = "NIFTY"
UNDERLYING_EXCH = os.getenv("OPENALGO_STRATEGY_EXCHANGE", "NSE_INDEX")
EXPIRY_DATE     = "30DEC25"     # DDMMMYY format - update each cycle
LOTS            = 1             # NIFTY lot = 75 (SEBI Apr 2026)
LOT_SIZE        = 75
PRODUCT         = "NRML"
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "short_straddle")

ENTRY_TIME      = dtime(9, 20)   # IST entry
EXIT_TIME       = dtime(15, 15)  # IST forced flatten
SL_MULTIPLIER   = 1.30           # 30% stop loss on each leg's premium

# IV rank filter: only sell premium when implied vol is in upper percentile of
# its trailing range. Set MIN_IV_RANK to 0 to disable. Computed from
# client.optiongreeks() over the last IV_RANK_LOOKBACK_DAYS daily samples.
MIN_IV_RANK             = 0.50    # require IV percentile >= 50% (median)
IV_RANK_LOOKBACK_DAYS   = 30

INIT_CASH       = 1_000_000      # only used for cost reporting

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)
load_dotenv(find_dotenv(usecwd=True))
API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST','127.0.0.1')}:{os.getenv('WEBSOCKET_PORT','8765')}")
COSTS = OPT_NRML
QUANTITY = LOTS * LOT_SIZE


def run_backtest():
    log.error("=" * 70)
    log.error("Options backtesting is not supported in this skill pack.")
    log.error("Options pricing depends on volatility surfaces, time decay,")
    log.error("and OI dynamics that intraday OHLCV backtests don't capture.")
    log.error("Use --mode live (with OpenAlgo's UI analyzer toggle for sandbox).")
    log.error("=" * 70)
    sys.exit(2)


def run_live():
    log.info("=" * 70)
    log.info("LIVE: %s on %s expiry=%s lots=%d (qty=%d)",
             STRATEGY_NAME, UNDERLYING, EXPIRY_DATE, LOTS, QUANTITY)
    log.info("=" * 70)
    log.info("Cost model: %s", COSTS.label)

    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
    slip = SlippageTracker(assumed_pct=COSTS.slippage)

    state = {
        "ce_oid": None, "pe_oid": None,
        "ce_symbol": None, "pe_symbol": None,
        "ce_entry": None, "pe_entry": None,
        "ce_sl_oid": None, "pe_sl_oid": None,
        "entered": False,
    }

    # --- 1. Wait for ENTRY_TIME ---
    log.info("Waiting for entry time %s IST", ENTRY_TIME)
    while not stop_event.is_set():
        now = _ist_now().time()
        if now >= ENTRY_TIME and now < EXIT_TIME:
            break
        if now >= EXIT_TIME:
            log.warning("Past exit time before entry; aborting")
            return
        stop_event.wait(15)

    # --- 1.5 IV rank filter ---
    if MIN_IV_RANK > 0:
        iv_rank = _compute_iv_rank(client)
        if iv_rank is not None and iv_rank < MIN_IV_RANK:
            log.warning("IV rank %.2f < required %.2f - skipping entry "
                        "(low premium environment)", iv_rank, MIN_IV_RANK)
            return
        if iv_rank is not None:
            log.info("IV rank %.2f >= required %.2f - proceeding", iv_rank, MIN_IV_RANK)

    # --- 2. Place straddle via optionsmultiorder ---
    log.info("Placing ATM straddle...")
    try:
        response = client.optionsmultiorder(
            strategy=STRATEGY_NAME,
            underlying=UNDERLYING,
            exchange=UNDERLYING_EXCH,
            expiry_date=EXPIRY_DATE,
            legs=[
                {"offset": "ATM", "option_type": "CE", "action": "SELL",
                 "quantity": QUANTITY, "product": PRODUCT, "pricetype": "MARKET"},
                {"offset": "ATM", "option_type": "PE", "action": "SELL",
                 "quantity": QUANTITY, "product": PRODUCT, "pricetype": "MARKET"},
            ],
        )
    except Exception:
        log.exception("optionsmultiorder failed - aborting")
        return

    if not isinstance(response, dict) or response.get("status") != "success":
        log.error("Straddle entry failed: %s", response); return

    results = response.get("results", [])
    if len(results) < 2:
        log.error("Unexpected results: %s", results); return

    state["ce_oid"]    = results[0].get("orderid")
    state["pe_oid"]    = results[1].get("orderid")
    state["ce_symbol"] = results[0].get("symbol")
    state["pe_symbol"] = results[1].get("symbol")
    log.info("CE leg: oid=%s sym=%s", state["ce_oid"], state["ce_symbol"])
    log.info("PE leg: oid=%s sym=%s", state["pe_oid"], state["pe_symbol"])

    # --- 3. Wait for fills, capture entry premiums ---
    state["ce_entry"] = _wait_fill(client, state["ce_oid"])
    state["pe_entry"] = _wait_fill(client, state["pe_oid"])
    if not state["ce_entry"] or not state["pe_entry"]:
        log.error("Could not confirm fills - manual check required")
        return
    log.info("Filled: CE @ %.2f | PE @ %.2f", state["ce_entry"], state["pe_entry"])
    state["entered"] = True

    # --- 4. Place per-leg SL (broker-side trigger, BUY back at 1.30x premium) ---
    ce_sl_trigger = round(state["ce_entry"] * SL_MULTIPLIER, 2)
    pe_sl_trigger = round(state["pe_entry"] * SL_MULTIPLIER, 2)
    ce_sl_price   = round(ce_sl_trigger * 1.005, 2)   # SL = trigger + 0.5% buffer
    pe_sl_price   = round(pe_sl_trigger * 1.005, 2)

    try:
        r = client.placeorder(
            strategy=STRATEGY_NAME, symbol=state["ce_symbol"], exchange="NFO",
            action="BUY", price_type="SL", product=PRODUCT,
            quantity=QUANTITY,
            trigger_price=str(ce_sl_trigger), price=str(ce_sl_price),
        )
        state["ce_sl_oid"] = r.get("orderid") if isinstance(r, dict) else None
        log.info("CE SL: trigger=%.2f price=%.2f oid=%s",
                 ce_sl_trigger, ce_sl_price, state["ce_sl_oid"])
    except Exception:
        log.exception("CE SL placement failed")

    try:
        r = client.placeorder(
            strategy=STRATEGY_NAME, symbol=state["pe_symbol"], exchange="NFO",
            action="BUY", price_type="SL", product=PRODUCT,
            quantity=QUANTITY,
            trigger_price=str(pe_sl_trigger), price=str(pe_sl_price),
        )
        state["pe_sl_oid"] = r.get("orderid") if isinstance(r, dict) else None
        log.info("PE SL: trigger=%.2f price=%.2f oid=%s",
                 pe_sl_trigger, pe_sl_price, state["pe_sl_oid"])
    except Exception:
        log.exception("PE SL placement failed")

    # --- 5. Monitor until EXIT_TIME ---
    log.info("Monitoring until exit time %s IST", EXIT_TIME)
    while not stop_event.is_set():
        if _ist_now().time() >= EXIT_TIME:
            log.info("Exit time reached - flattening")
            break
        stop_event.wait(30)

    # --- 6. Square off both legs and cancel SL orders ---
    log.info("Cancelling SL orders and closing positions")
    try: client.cancelallorder(strategy=STRATEGY_NAME)
    except Exception: log.exception("cancelallorder failed")
    try: client.closeposition(strategy=STRATEGY_NAME)
    except Exception: log.exception("closeposition failed")

    log.info("\n%s", slip.report())


def _ist_now():
    return datetime.now(pytz.timezone("Asia/Kolkata"))


def _compute_iv_rank(client):
    """
    IV rank from INDIAVIX history.

    Rank = (current VIX - min) / (max - min) over IV_RANK_LOOKBACK_DAYS.
    Returns a value in [0, 1] or None if data isn't available.

    INDIAVIX is the most reliable proxy for NIFTY option IV regime - it's the
    market-implied volatility expectation across the option chain. Premium-
    selling strategies like short straddle should require VIX in the upper
    half of its recent range to ensure adequate premium.
    """
    try:
        from datetime import timedelta as _td
        end = datetime.now().date()
        start = end - _td(days=IV_RANK_LOOKBACK_DAYS * 2)   # extra buffer for weekends
        df = client.history(
            symbol="INDIAVIX", exchange="NSE_INDEX", interval="D",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            source="api",
        )
        if df is None or len(df) < 5:
            log.info("IV rank: INDIAVIX history unavailable - skipping filter")
            return None
        closes = df["close"] if "close" in df.columns else df.get("Close")
        if closes is None or len(closes) < 5:
            return None
        recent = closes.iloc[-IV_RANK_LOOKBACK_DAYS:]
        cur = float(recent.iloc[-1])
        lo  = float(recent.min())
        hi  = float(recent.max())
        if hi <= lo:
            return None
        rank = (cur - lo) / (hi - lo)
        log.info("INDIAVIX %.2f (range %.2f..%.2f over %d days) -> rank=%.2f",
                 cur, lo, hi, len(recent), rank)
        return float(rank)
    except Exception:
        log.exception("IV rank fetch failed - skipping filter")
        return None


def _wait_fill(client, oid, retries=20, sleep_s=1.0):
    if not oid: return None
    for _ in range(retries):
        try:
            r = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
            d = r.get("data", {}) if isinstance(r, dict) else {}
            if d.get("order_status") == "complete":
                avg = d.get("average_price") or d.get("price")
                if avg: return float(avg)
        except Exception:
            log.exception("orderstatus failed")
        time.sleep(sleep_s)
    return None


stop_event = threading.Event()
def _sh(s, f): log.info("signal %d - shutting down", s); stop_event.set()
signal.signal(signal.SIGTERM, _sh); signal.signal(signal.SIGINT, _sh)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backtest","live"], default=os.getenv("MODE","live"))
    a = p.parse_args()
    run_backtest() if a.mode == "backtest" else run_live()

if __name__ == "__main__": main()
