"""
cost_model.py - Real-world transaction cost and slippage model for Indian markets.

Centralizes the 4-segment Indian fee structure (Intraday/Delivery Equity, F&O Futures,
F&O Options) and per-segment slippage assumptions. These same constants flow into:

  - VectorBT backtests via fees / fixed_fees / slippage parameters
  - Live runners via LIMIT-with-offset placement (slippage protection)
  - Drift reports comparing assumed vs measured slippage

Broker-neutral: override individual constants for your broker's actual rates.

Reference: matches conventions in vectorbt-backtesting-skills/indian-market-costs.md
"""
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Segment fee table (Indian Market Standard)
#
# Derived from STT + exchange transaction + GST + SEBI + stamp duty across a
# Rs 10L turnover. Brokerage is conservative Rs 20 per order across the board.
# Adjust to your actual broker rates if needed.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostBlock:
    fees: float          # decimal, applied to turnover per side (0.001 = 0.1%)
    fixed_fees: float    # absolute, applied per order
    slippage: float      # decimal, applied to fill price per side
    label: str

INTRADAY_EQ = CostBlock(
    fees=0.000225,       # 0.0225% per side (statutory only, MIS pays no STT on buy)
    fixed_fees=20.0,     # Rs 20 brokerage per order
    slippage=0.0005,     # 5 bps - liquid intraday equity
    label="Intraday Equity (MIS)",
)

DELIVERY_EQ = CostBlock(
    fees=0.00111,        # 0.111% per side (STT 0.1% on both + statutory)
    fixed_fees=20.0,     # Conservative; many brokers offer free delivery
    slippage=0.0003,     # 3 bps - daily-bar delivery, less time-sensitive
    label="Delivery Equity (CNC)",
)

FUT_NRML = CostBlock(
    fees=0.00018,        # 0.018% per side (STT 0.02% sell side + statutory)
    fixed_fees=20.0,
    slippage=0.0002,     # 2 bps - very liquid index futures
    label="F&O Futures (NRML)",
)

OPT_NRML = CostBlock(
    fees=0.00098,        # 0.098% per side (STT 0.1% sell side + statutory)
    fixed_fees=20.0,
    slippage=0.0010,     # 10 bps - wider option spreads
    label="F&O Options (NRML)",
)

ILLIQUID_FALLBACK = CostBlock(
    fees=0.00111,
    fixed_fees=20.0,
    slippage=0.0030,     # 30 bps - thin names
    label="Illiquid (manual override)",
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

# Map (product, exchange-or-instrument) -> CostBlock.
# Falls back conservatively if the pair isn't found.
_TABLE = {
    ("MIS", "NSE"):   INTRADAY_EQ,
    ("MIS", "BSE"):   INTRADAY_EQ,
    ("CNC", "NSE"):   DELIVERY_EQ,
    ("CNC", "BSE"):   DELIVERY_EQ,
    ("NRML", "NFO"):  FUT_NRML,    # default to futures; override for options below
    ("NRML", "BFO"):  FUT_NRML,
    ("NRML", "MCX"):  FUT_NRML,
    ("NRML", "CDS"):  FUT_NRML,
    ("NRML", "BCD"):  FUT_NRML,
}


def lookup(product, exchange, instrument_type=None):
    """
    Resolve a CostBlock for a (product, exchange) pair.

    instrument_type: optional. Pass "OPT" to force the option-fee block when
    the exchange is a derivatives one (NFO/BFO).
    """
    if instrument_type and instrument_type.upper().startswith("OPT"):
        if exchange in ("NFO", "BFO"):
            return OPT_NRML
    return _TABLE.get((product.upper(), exchange.upper()), DELIVERY_EQ)


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def cost_summary(block, turnover):
    """
    Estimate one-side cost on a given rupee turnover. Useful for printing
    backtest cost assumptions before running.
    """
    statutory = turnover * block.fees
    slip      = turnover * block.slippage
    brokerage = block.fixed_fees
    return {
        "label": block.label,
        "turnover": turnover,
        "statutory_per_side": statutory,
        "slippage_per_side": slip,
        "brokerage_per_order": brokerage,
        "round_trip": (statutory + slip) * 2 + brokerage * 2,
    }


def format_cost_report(block, turnover):
    s = cost_summary(block, turnover)
    return (
        f"=== Cost Model: {s['label']} ===\n"
        f"  Statutory + Exchange:  {block.fees*100:.4f}% per side\n"
        f"  Brokerage:             Rs {block.fixed_fees:.0f} per order\n"
        f"  Slippage:              {block.slippage*100:.4f}% per side\n"
        f"  On Rs {s['turnover']:,.0f} turnover:\n"
        f"    statutory  = Rs {s['statutory_per_side']:,.2f} (per side)\n"
        f"    slippage   = Rs {s['slippage_per_side']:,.2f} (per side)\n"
        f"    brokerage  = Rs {s['brokerage_per_order']:,.0f} (per order)\n"
        f"    round-trip = Rs {s['round_trip']:,.2f}"
    )


# ---------------------------------------------------------------------------
# Live-mode slippage tracker
# ---------------------------------------------------------------------------

class SlippageTracker:
    """
    Records realized slippage per fill and produces an end-of-session report.

    Usage:
        tracker = SlippageTracker(assumed_pct=0.0005)
        tracker.record(decision_price=100.0, fill_price=100.05)
        ...
        print(tracker.report())
    """

    def __init__(self, assumed_pct):
        self.assumed_pct = assumed_pct
        self.fills = []

    def record(self, decision_price, fill_price, qty=1, side="BUY"):
        if decision_price <= 0:
            return
        signed = (fill_price - decision_price) if side == "BUY" else (decision_price - fill_price)
        self.fills.append({
            "decision_price": decision_price,
            "fill_price": fill_price,
            "qty": qty,
            "side": side,
            "signed_slip_abs": signed,
            "signed_slip_pct": signed / decision_price,
        })

    def measured_pct(self):
        if not self.fills:
            return 0.0
        return sum(f["signed_slip_pct"] for f in self.fills) / len(self.fills)

    def report(self):
        if not self.fills:
            return "Slippage: no fills recorded."
        m = self.measured_pct()
        a = self.assumed_pct
        ratio = (m / a) if a else float("inf")
        worst = max(self.fills, key=lambda f: abs(f["signed_slip_pct"]))
        drift = "OK" if abs(ratio) < 2.0 else "DRIFT - measured >2x assumed"
        return (
            f"Slippage Report:\n"
            f"  Fills:               {len(self.fills)}\n"
            f"  Assumed (per side):  {a*100:.4f}%\n"
            f"  Measured (avg):      {m*100:.4f}%\n"
            f"  Ratio measured/assumed: {ratio:.2f}x  [{drift}]\n"
            f"  Worst slip: {worst['signed_slip_pct']*100:.4f}% "
            f"({worst['side']} @ decided {worst['decision_price']:.2f}, "
            f"filled {worst['fill_price']:.2f})"
        )
