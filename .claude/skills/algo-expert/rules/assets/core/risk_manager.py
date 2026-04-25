"""
risk_manager.py - Per-position risk: stop loss, take profit, trailing stop, time exit.

The same risk thresholds are honored in both modes:
  - Backtest: passed to vbt.Portfolio.from_signals(sl_stop=, tp_stop=, sl_trail=)
  - Live:     this RiskManager subscribes to LTP via WebSocket and fires
              client.placesmartorder(position_size=0) when a threshold breaks

Critical pattern (lifted from OpenAlgo's emacrossover_strategy_python.py):
  - WebSocket callback NEVER places an exit order directly. It checks
    thresholds and spawns a worker thread to place the exit. This keeps the
    callback fast and prevents the WS feed from blocking on broker latency.
"""
from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """All thresholds are percentages of entry price unless suffixed _abs.
    Set a value to None to disable that check."""
    sl_pct: Optional[float] = None         # 0.01 = 1% stop loss
    tp_pct: Optional[float] = None         # 0.02 = 2% take profit
    trail_pct: Optional[float] = None      # 0.01 = 1% trailing stop
    time_exit_min: Optional[int] = None    # exit after N minutes regardless
    sl_abs: Optional[float] = None         # absolute stop loss price
    tp_abs: Optional[float] = None         # absolute take profit price


@dataclass
class Position:
    symbol: str
    exchange: str
    side: str                # "BUY" or "SELL" (entry side)
    qty: int
    entry_price: float
    entry_time: float        # epoch seconds
    product: str             # "MIS" / "CNC" / "NRML"
    strategy: str
    watermark: float = 0.0   # for trailing stop: best favourable price seen
    closed: bool = False


class RiskManager:
    """
    Manages a single open position's exit triggers via the LTP WebSocket feed.

    Usage:
        rm = RiskManager(client, strategy_name, risk_config, on_exit_callback)
        rm.set_position(Position(...))
        rm.start()    # begins LTP subscription
        ...
        rm.stop()     # unsubscribes and disconnects
    """

    def __init__(self, client, strategy_name, risk_config,
                 on_exit_callback=None, slippage_tracker=None, state=None):
        self.client = client
        self.strategy_name = strategy_name
        self.risk = risk_config
        self.on_exit = on_exit_callback        # called(position, reason, exit_price)
        self.slippage_tracker = slippage_tracker
        self.state = state                     # optional StrategyState for persistence
        self.position: Optional[Position] = None
        self._subscribed = False
        self._exit_in_progress = False
        self._exit_lock = threading.Lock()
        self._stop_event = threading.Event()

    # --- Position lifecycle -------------------------------------------------

    def set_position(self, position, restore_watermark=None):
        """
        Arm the risk manager for a new position.

        restore_watermark: pass a stored watermark when resuming from state on
        restart - otherwise the trailing stop loses its lock-in.
        """
        self.position = position
        self.position.watermark = restore_watermark or position.entry_price
        self._exit_in_progress = False
        self._subscribe_if_needed()
        if self.state is not None:
            try:
                from core.state import StoredPosition
                stored = StoredPosition(
                    symbol=position.symbol, exchange=position.exchange,
                    side=position.side, qty=position.qty,
                    entry_price=position.entry_price,
                    entry_time=position.entry_time,
                    product=position.product,
                    watermark=self.position.watermark,
                    closed=False,
                )
                self.state.save_position(stored)
            except Exception:
                log.exception("state persist on entry failed - continuing")
        log.info("Risk manager armed: %s %s %s qty=%d entry=%.2f watermark=%.2f",
                 position.strategy, position.side, position.symbol,
                 position.qty, position.entry_price, self.position.watermark)

    def clear_position(self):
        self._unsubscribe_if_needed()
        self.position = None
        self._exit_in_progress = False

    # --- WS subscription ---------------------------------------------------

    def _subscribe_if_needed(self):
        if self._subscribed or self.position is None:
            return
        instruments = [{"exchange": self.position.exchange, "symbol": self.position.symbol}]
        try:
            self.client.subscribe_ltp(instruments, on_data_received=self._on_tick)
            self._subscribed = True
        except Exception:
            log.exception("subscribe_ltp failed")

    def _unsubscribe_if_needed(self):
        if not self._subscribed or self.position is None:
            return
        instruments = [{"exchange": self.position.exchange, "symbol": self.position.symbol}]
        try:
            self.client.unsubscribe_ltp(instruments)
        except Exception:
            log.exception("unsubscribe_ltp failed - continuing")
        self._subscribed = False

    # --- Tick handler -------------------------------------------------------

    def _on_tick(self, data):
        # WS callbacks must stay fast. Check thresholds, spawn exit thread if needed.
        pos = self.position
        if pos is None or pos.closed or self._exit_in_progress:
            return
        try:
            ltp = float(data.get("data", {}).get("ltp", 0))
        except (TypeError, ValueError):
            return
        if ltp <= 0:
            return

        # Update watermark for trailing stop, persist if state available
        watermark_changed = False
        if pos.side == "BUY" and ltp > pos.watermark:
            pos.watermark = ltp
            watermark_changed = True
        elif pos.side == "SELL" and ltp < pos.watermark:
            pos.watermark = ltp
            watermark_changed = True
        if watermark_changed and self.state is not None:
            try:
                self.state.update_watermark(pos.symbol, pos.exchange,
                                            pos.entry_time, pos.watermark)
            except Exception:
                log.exception("watermark persist failed - continuing")

        reason = self._check_exits(pos, ltp)
        if reason is None:
            return

        with self._exit_lock:
            if self._exit_in_progress:
                return
            self._exit_in_progress = True

        # Spawn worker - never block the WS callback
        threading.Thread(
            target=self._place_exit, args=(pos, reason, ltp), daemon=True,
        ).start()

    def _check_exits(self, pos, ltp):
        """Returns reason string if any exit should fire, else None."""
        # Time exit
        if self.risk.time_exit_min is not None:
            elapsed_min = (time.time() - pos.entry_time) / 60.0
            if elapsed_min >= self.risk.time_exit_min:
                return f"TIME_EXIT ({elapsed_min:.1f}min)"

        # Absolute SL / TP
        if pos.side == "BUY":
            if self.risk.sl_abs is not None and ltp <= self.risk.sl_abs:
                return f"SL_ABS ({ltp:.2f} <= {self.risk.sl_abs:.2f})"
            if self.risk.tp_abs is not None and ltp >= self.risk.tp_abs:
                return f"TP_ABS ({ltp:.2f} >= {self.risk.tp_abs:.2f})"
        else:
            if self.risk.sl_abs is not None and ltp >= self.risk.sl_abs:
                return f"SL_ABS ({ltp:.2f} >= {self.risk.sl_abs:.2f})"
            if self.risk.tp_abs is not None and ltp <= self.risk.tp_abs:
                return f"TP_ABS ({ltp:.2f} <= {self.risk.tp_abs:.2f})"

        # Percent SL
        if self.risk.sl_pct is not None:
            if pos.side == "BUY":
                trigger = pos.entry_price * (1.0 - self.risk.sl_pct)
                if ltp <= trigger:
                    return f"SL_PCT ({ltp:.2f} <= {trigger:.2f}, -{self.risk.sl_pct*100:.2f}%)"
            else:
                trigger = pos.entry_price * (1.0 + self.risk.sl_pct)
                if ltp >= trigger:
                    return f"SL_PCT ({ltp:.2f} >= {trigger:.2f}, +{self.risk.sl_pct*100:.2f}%)"

        # Percent TP
        if self.risk.tp_pct is not None:
            if pos.side == "BUY":
                trigger = pos.entry_price * (1.0 + self.risk.tp_pct)
                if ltp >= trigger:
                    return f"TP_PCT ({ltp:.2f} >= {trigger:.2f}, +{self.risk.tp_pct*100:.2f}%)"
            else:
                trigger = pos.entry_price * (1.0 - self.risk.tp_pct)
                if ltp <= trigger:
                    return f"TP_PCT ({ltp:.2f} <= {trigger:.2f}, -{self.risk.tp_pct*100:.2f}%)"

        # Trailing stop (only after price has moved favourably)
        if self.risk.trail_pct is not None and pos.watermark != pos.entry_price:
            if pos.side == "BUY":
                trail_trigger = pos.watermark * (1.0 - self.risk.trail_pct)
                if ltp <= trail_trigger:
                    return (f"TRAIL ({ltp:.2f} <= {trail_trigger:.2f}, "
                            f"watermark={pos.watermark:.2f}, "
                            f"-{self.risk.trail_pct*100:.2f}%)")
            else:
                trail_trigger = pos.watermark * (1.0 + self.risk.trail_pct)
                if ltp >= trail_trigger:
                    return (f"TRAIL ({ltp:.2f} >= {trail_trigger:.2f}, "
                            f"watermark={pos.watermark:.2f}, "
                            f"+{self.risk.trail_pct*100:.2f}%)")

        return None

    # --- Exit placement -----------------------------------------------------

    def _place_exit(self, pos, reason, decision_ltp):
        """Runs in its own thread. Uses placesmartorder(position_size=0) to flatten."""
        log.info("EXIT trigger: %s %s reason=%s ltp=%.2f",
                 pos.strategy, pos.symbol, reason, decision_ltp)
        opposite = "SELL" if pos.side == "BUY" else "BUY"
        try:
            response = self.client.placesmartorder(
                strategy=pos.strategy,
                symbol=pos.symbol,
                action=opposite,
                exchange=pos.exchange,
                price_type="MARKET",
                product=pos.product,
                quantity=pos.qty,
                position_size=0,
            )
            log.info("Exit order placed: %s", response)
            order_id = response.get("orderid") if isinstance(response, dict) else None

            # Try to read fill price for slippage tracking
            fill_price = decision_ltp
            if order_id:
                fill_price = self._read_fill_price(order_id, fallback=decision_ltp)
                if self.slippage_tracker:
                    self.slippage_tracker.record(
                        decision_price=decision_ltp,
                        fill_price=fill_price,
                        qty=pos.qty,
                        side=opposite,
                    )

            pos.closed = True
            if self.state is not None:
                try:
                    self.state.mark_closed(pos.symbol, pos.exchange, pos.entry_time)
                    if order_id:
                        self.state.record_fill(order_id, pos.symbol, opposite,
                                               pos.qty, decision_ltp, fill_price)
                except Exception:
                    log.exception("state persist on exit failed - continuing")
            if self.on_exit:
                try:
                    self.on_exit(pos, reason, fill_price)
                except Exception:
                    log.exception("on_exit callback raised")
        except Exception:
            log.exception("Exit order placement failed - manual intervention may be needed")
        finally:
            self._unsubscribe_if_needed()

    def _read_fill_price(self, order_id, fallback, retries=10, sleep_s=0.5):
        """Poll orderstatus for the fill price. Falls back to decision price."""
        for _ in range(retries):
            try:
                resp = self.client.orderstatus(order_id=order_id, strategy=self.strategy_name)
                data = resp.get("data", {}) if isinstance(resp, dict) else {}
                if data.get("order_status") == "complete":
                    avg = data.get("average_price") or data.get("price")
                    if avg:
                        return float(avg)
            except Exception:
                log.exception("orderstatus poll failed")
            time.sleep(sleep_s)
        return fallback

    # --- Lifecycle ----------------------------------------------------------

    def stop(self):
        self._stop_event.set()
        self._unsubscribe_if_needed()
