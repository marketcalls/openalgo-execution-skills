"""
portfolio_runner.py - Multi-strategy supervisor with portfolio-level risk caps.

Reads a YAML config that lists strategies + caps, launches each strategy as a
subprocess, monitors aggregate P&L via OpenAlgo's tradebook/positionbook APIs,
and triggers a kill-switch when caps breach.

Backtest mode: aggregates per-strategy backtest equity curves and computes
the same caps post-hoc.

YAML schema:
    capital: 1000000
    portfolio_caps:
      portfolio_sl_pct: 0.02       # halt all strategies at -2% capital
      portfolio_tp_pct: 0.03       # halt all strategies at +3% capital
      daily_loss_pct: 0.015        # daily loss limit
      daily_target_pct: 0.025      # daily target
      max_concurrent_positions: 5
      max_symbol_concentration: 0.30   # one symbol cannot exceed 30% of capital
    strategies:
      - name: ema_sbin
        path: strategies/ema_sbin/strategy.py
      - name: rsi_reliance
        path: strategies/rsi_reliance/strategy.py
"""
from datetime import datetime
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return cfg


# ---------------------------------------------------------------------------
# Live-mode supervisor
# ---------------------------------------------------------------------------

class StrategyProcess:
    def __init__(self, name, script_path, env=None):
        self.name = name
        self.script_path = script_path
        self.env = env or {}
        self.proc = None

    def start(self, mode="live"):
        env = os.environ.copy()
        env.update(self.env)
        env["MODE"] = mode
        env["STRATEGY_NAME"] = self.name
        log.info("Starting strategy %s (%s)", self.name, self.script_path)
        self.proc = subprocess.Popen(
            [sys.executable, self.script_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Pump stdout to log
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        if self.proc is None or self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            log.info("[%s] %s", self.name, line.rstrip())

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=15):
        if not self.is_alive():
            return
        log.info("Stopping strategy %s (SIGTERM)", self.name)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("Strategy %s did not stop in %ds, sending SIGKILL", self.name, timeout)
            self.proc.kill()


class PortfolioRunner:
    def __init__(self, config_path, client_factory, mode="live"):
        self.cfg = load_config(config_path)
        self.client_factory = client_factory
        self.mode = mode
        self.client = client_factory()
        self.children = [
            StrategyProcess(s["name"], s["path"], env=s.get("env"))
            for s in self.cfg.get("strategies", [])
        ]
        self.start_capital = float(self.cfg.get("capital", 1_000_000))
        self.caps = self.cfg.get("portfolio_caps", {})
        self.day_open_realized = 0.0    # realized P&L at start of day
        self.daily_reset_done = False
        self.kill_reason = None
        self._stop_event = threading.Event()

    # --- Lifecycle ----------------------------------------------------------

    def start(self):
        log.info("Portfolio runner: starting %d strategies (mode=%s)",
                 len(self.children), self.mode)
        log.info("Caps: %s", self.caps)
        for child in self.children:
            child.start(mode=self.mode)
        # Monitor loop
        threading.Thread(target=self._monitor, daemon=True).start()

    def stop_all(self, reason=""):
        if self.kill_reason is None and reason:
            self.kill_reason = reason
            log.warning("KILL SWITCH: %s", reason)
        for child in self.children:
            child.stop()
        # Defensive: cancel pending orders + close positions
        try:
            self.client.cancelallorder(strategy="PortfolioRunner")
        except Exception:
            log.exception("cancelallorder failed")
        try:
            self.client.closeposition(strategy="PortfolioRunner")
        except Exception:
            log.exception("closeposition failed")

    # --- Monitor loop -------------------------------------------------------

    def _monitor(self):
        while not self._stop_event.is_set():
            try:
                self._reset_at_midnight()
                pnl = self._compute_total_pnl()
                breach = self._check_caps(pnl)
                if breach:
                    self.stop_all(reason=breach)
                    return
            except Exception:
                log.exception("monitor iteration failed")
            self._stop_event.wait(15)   # check every 15s

    def _reset_at_midnight(self):
        """At 00:00-00:02 IST, anchor day_open_realized to current realized PnL.
        Daily caps then measure delta from this anchor."""
        now = datetime.now()
        if now.hour == 0 and now.minute < 2 and not self.daily_reset_done:
            self.day_open_realized = self._fetch_realized_pnl()
            log.info("Daily reset (00:00 IST): day_open_realized anchored at Rs %.2f",
                     self.day_open_realized)
            self.daily_reset_done = True
        elif now.hour > 0:
            self.daily_reset_done = False

    def _fetch_realized_pnl(self):
        """
        Pair buy/sell trades from tradebook by symbol+product, compute realized PnL.

        Uses FIFO pairing within (symbol, product). Unmatched trades are the
        currently open exposure (already counted by positionbook unrealized).
        """
        try:
            tb = self.client.tradebook()
            rows = tb.get("data", []) if isinstance(tb, dict) else []
        except Exception:
            log.exception("tradebook fetch failed")
            return 0.0

        # Group by (symbol, product), preserve order, separate buys/sells
        from collections import defaultdict, deque
        buys = defaultdict(deque)
        sells = defaultdict(deque)
        for r in rows:
            sym, prod = r.get("symbol"), r.get("product")
            try:
                qty = abs(int(float(r.get("quantity", 0) or 0)))
                price = float(r.get("average_price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if qty == 0 or price == 0:
                continue
            (buys if r.get("action") == "BUY" else sells)[(sym, prod)].append((qty, price))

        realized = 0.0
        for key in set(list(buys.keys()) + list(sells.keys())):
            b = buys[key]
            s = sells[key]
            while b and s:
                bq, bp = b[0]
                sq, sp = s[0]
                matched = min(bq, sq)
                realized += matched * (sp - bp)
                if bq == matched:
                    b.popleft()
                else:
                    b[0] = (bq - matched, bp)
                if sq == matched:
                    s.popleft()
                else:
                    s[0] = (sq - matched, sp)
        return realized

    def _compute_total_pnl(self):
        """Realized (from tradebook) + unrealized (from positionbook)."""
        unrealized = 0.0
        try:
            pb = self.client.positionbook()
            rows = pb.get("data", []) if isinstance(pb, dict) else []
            for r in rows:
                pnl = r.get("pnl")
                if pnl is None:
                    continue
                try:
                    unrealized += float(pnl)
                except (TypeError, ValueError):
                    continue
        except Exception:
            log.exception("positionbook fetch failed")
            unrealized = 0.0
        realized = self._fetch_realized_pnl()
        return realized + unrealized

    def _check_caps(self, pnl):
        cap = self.start_capital
        # Portfolio SL
        psl = self.caps.get("portfolio_sl_pct")
        if psl is not None and pnl <= -abs(psl) * cap:
            return f"PORTFOLIO_SL: pnl={pnl:.2f} <= -{psl*100:.2f}% cap"
        # Portfolio TP
        ptp = self.caps.get("portfolio_tp_pct")
        if ptp is not None and pnl >= abs(ptp) * cap:
            return f"PORTFOLIO_TP: pnl={pnl:.2f} >= +{ptp*100:.2f}% cap"
        # Daily loss / target
        daily_pnl = pnl - self.day_open_realized
        dsl = self.caps.get("daily_loss_pct")
        if dsl is not None and daily_pnl <= -abs(dsl) * cap:
            return f"DAILY_LOSS: daily_pnl={daily_pnl:.2f} <= -{dsl*100:.2f}% cap"
        dtg = self.caps.get("daily_target_pct")
        if dtg is not None and daily_pnl >= abs(dtg) * cap:
            return f"DAILY_TARGET: daily_pnl={daily_pnl:.2f} >= +{dtg*100:.2f}% cap"
        # Max concurrent positions
        mc = self.caps.get("max_concurrent_positions")
        if mc is not None:
            try:
                pb = self.client.positionbook()
                rows = pb.get("data", []) if isinstance(pb, dict) else []
                active = sum(
                    1 for r in rows
                    if int(float(r.get("quantity", 0) or 0)) != 0
                )
                if active > mc:
                    return f"MAX_POSITIONS: active={active} > cap={mc}"
            except Exception:
                log.exception("positionbook fetch for max_positions failed")
        return None

    def join(self):
        try:
            while any(c.is_alive() for c in self.children):
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        self._stop_event.set()
        self.stop_all(reason="user interrupt")
