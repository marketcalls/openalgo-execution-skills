"""
Iron Condor - options EXECUTION-ONLY strategy.

Sells OTM CE + OTM PE (inner wings) and buys further OTM CE + OTM PE
(protective outer wings). Defined-risk credit spread.

Legs are placed atomically via client.optionsmultiorder. BUY legs go in first
for margin efficiency (handled by OpenAlgo internally).

Backtest mode is not supported (see short_straddle for explanation).
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
from core.cost_model import OPT_NRML, SlippageTracker  # noqa: E402

# === Config ===
UNDERLYING      = "NIFTY"
UNDERLYING_EXCH = os.getenv("OPENALGO_STRATEGY_EXCHANGE", "NSE_INDEX")
EXPIRY_DATE     = "30DEC25"
LOTS            = 1
LOT_SIZE        = 75
QUANTITY        = LOTS * LOT_SIZE
PRODUCT         = "NRML"
STRATEGY_NAME   = os.getenv("STRATEGY_NAME", "iron_condor")

# Wing structure: SHORT at OTM_NEAR, LONG at OTM_FAR (further OTM)
OTM_NEAR        = 4    # OTM4 = 4 strikes OTM
OTM_FAR         = 8    # OTM8 = 8 strikes OTM (protection wings)

ENTRY_TIME      = dtime(9, 30)
EXIT_TIME       = dtime(15, 15)

# IV rank filter - only enter when INDIAVIX is in upper percentile of trailing range
MIN_IV_RANK             = 0.50
IV_RANK_LOOKBACK_DAYS   = 30

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)
load_dotenv(find_dotenv(usecwd=True))
API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST','127.0.0.1')}:{os.getenv('WEBSOCKET_PORT','8765')}")
COSTS = OPT_NRML


def run_backtest():
    log.error("Options backtesting is not supported. Run with --mode live "
              "(use OpenAlgo's UI analyzer toggle for sandbox).")
    sys.exit(2)


def run_live():
    log.info("LIVE: %s on %s expiry=%s OTM_NEAR=%d OTM_FAR=%d",
             STRATEGY_NAME, UNDERLYING, EXPIRY_DATE, OTM_NEAR, OTM_FAR)

    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
    slip = SlippageTracker(assumed_pct=COSTS.slippage)

    # Wait for entry time
    log.info("Waiting for entry time %s IST", ENTRY_TIME)
    while not stop_event.is_set():
        now = _ist_now().time()
        if now >= ENTRY_TIME and now < EXIT_TIME: break
        if now >= EXIT_TIME:
            log.warning("Past exit time before entry; aborting"); return
        stop_event.wait(15)

    # IV rank gate
    if MIN_IV_RANK > 0:
        iv_rank = _compute_iv_rank(client)
        if iv_rank is not None and iv_rank < MIN_IV_RANK:
            log.warning("IV rank %.2f < %.2f - skipping (insufficient premium)",
                        iv_rank, MIN_IV_RANK)
            return
        if iv_rank is not None:
            log.info("IV rank %.2f >= %.2f - proceeding", iv_rank, MIN_IV_RANK)

    # Iron condor legs:
    #   BUY  far OTM CE (long protection)
    #   BUY  far OTM PE (long protection)
    #   SELL near OTM CE (short premium)
    #   SELL near OTM PE (short premium)
    legs = [
        {"offset": f"OTM{OTM_FAR}",  "option_type": "CE", "action": "BUY",
         "quantity": QUANTITY, "product": PRODUCT, "pricetype": "MARKET"},
        {"offset": f"OTM{OTM_FAR}",  "option_type": "PE", "action": "BUY",
         "quantity": QUANTITY, "product": PRODUCT, "pricetype": "MARKET"},
        {"offset": f"OTM{OTM_NEAR}", "option_type": "CE", "action": "SELL",
         "quantity": QUANTITY, "product": PRODUCT, "pricetype": "MARKET"},
        {"offset": f"OTM{OTM_NEAR}", "option_type": "PE", "action": "SELL",
         "quantity": QUANTITY, "product": PRODUCT, "pricetype": "MARKET"},
    ]

    log.info("Placing iron condor: BUY %s wings, SELL %s body",
             f"OTM{OTM_FAR}", f"OTM{OTM_NEAR}")
    try:
        response = client.optionsmultiorder(
            strategy=STRATEGY_NAME,
            underlying=UNDERLYING,
            exchange=UNDERLYING_EXCH,
            expiry_date=EXPIRY_DATE,
            legs=legs,
        )
    except Exception:
        log.exception("optionsmultiorder failed")
        return

    if not isinstance(response, dict) or response.get("status") != "success":
        log.error("Iron condor entry failed: %s", response); return

    for leg in response.get("results", []):
        log.info("Leg: %s %s %s oid=%s sym=%s",
                 leg.get("action"), leg.get("offset"), leg.get("option_type"),
                 leg.get("orderid"), leg.get("symbol"))

    # Monitor until exit time
    log.info("Monitoring until %s IST", EXIT_TIME)
    while not stop_event.is_set():
        if _ist_now().time() >= EXIT_TIME:
            log.info("Exit time - flattening")
            break
        stop_event.wait(30)

    log.info("Closing all positions and cancelling pending orders")
    try: client.cancelallorder(strategy=STRATEGY_NAME)
    except Exception: log.exception("cancelallorder failed")
    try: client.closeposition(strategy=STRATEGY_NAME)
    except Exception: log.exception("closeposition failed")
    log.info("\n%s", slip.report())


def _ist_now():
    return datetime.now(pytz.timezone("Asia/Kolkata"))


def _compute_iv_rank(client):
    """IV rank from INDIAVIX history. See short_straddle for details."""
    try:
        from datetime import timedelta as _td
        end = datetime.now().date()
        start = end - _td(days=IV_RANK_LOOKBACK_DAYS * 2)
        df = client.history(
            symbol="INDIAVIX", exchange="NSE_INDEX", interval="D",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            source="api",
        )
        if df is None or len(df) < 5: return None
        closes = df["close"] if "close" in df.columns else df.get("Close")
        if closes is None or len(closes) < 5: return None
        recent = closes.iloc[-IV_RANK_LOOKBACK_DAYS:]
        cur = float(recent.iloc[-1])
        lo, hi = float(recent.min()), float(recent.max())
        if hi <= lo: return None
        return (cur - lo) / (hi - lo)
    except Exception:
        log.exception("IV rank fetch failed")
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
