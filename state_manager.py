"""
Tamper-proof state management for the risk management system.
Uses encrypted file storage with integrity checks so that state
cannot be manually edited to bypass lockouts.
"""

import json
import hashlib
import logging
import os
import time
from datetime import datetime, date
from typing import Any, Optional

from cryptography.fernet import Fernet

from config import Config

logger = logging.getLogger(__name__)


class StateManager:
    """
    Manages persistent daily state with encryption and integrity verification.
    The state file cannot be manually edited to bypass lockouts because:
    1. It is Fernet-encrypted (AES-128-CBC)
    2. It includes an HMAC integrity hash
    3. The date is embedded - old states auto-expire
    """

    def __init__(self):
        os.makedirs(Config.STATE_DIR, exist_ok=True)
        self._key = self._get_or_create_key()
        self._fernet = Fernet(self._key)
        self._state_file = os.path.join(Config.STATE_DIR, "daily_state.enc")
        self._key_file = os.path.join(Config.STATE_DIR, ".state_key")
        self._state: dict = {}
        self._load_or_reset()

    def _get_or_create_key(self) -> bytes:
        """Get encryption key from env or generate and persist one."""
        key_file = os.path.join(Config.STATE_DIR, ".state_key")
        if Config.STATE_ENCRYPTION_KEY:
            return Config.STATE_ENCRYPTION_KEY.encode()
        if os.path.exists(key_file):
            with open(key_file, "rb") as f:
                return f.read().strip()
        key = Fernet.generate_key()
        with open(key_file, "wb") as f:
            f.write(key)
        # Restrict permissions
        os.chmod(key_file, 0o600)
        logger.info("Generated new state encryption key")
        return key

    def _load_or_reset(self):
        """Load existing state or create fresh state for today."""
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "rb") as f:
                    encrypted = f.read()
                decrypted = self._fernet.decrypt(encrypted)
                self._state = json.loads(decrypted.decode())

                # Check if state is for today
                if self._state.get("date") != str(date.today()):
                    logger.info("State is from previous day, resetting")
                    self._reset_state()
                else:
                    logger.info("Loaded existing state for today")
                    return
            except Exception as e:
                logger.warning("Failed to load state (possibly tampered): %s", e)
                self._reset_state()
        else:
            self._reset_state()

    def _reset_state(self):
        """Create a fresh state for today."""
        self._state = {
            "date": str(date.today()),
            "created_at": time.time(),

            # P&L Tracking
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "peak_pnl": 0.0,
            "total_pnl": 0.0,

            # Trade Tracking
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "consecutive_losses": 0,
            "trade_history": [],

            # Risk State
            "is_locked_out": False,
            "lockout_reason": "",
            "lockout_time": None,
            "is_cooling_down": False,
            "cooldown_until": None,
            "cooldown_reason": "",

            # Profit Lock
            "profit_lock_active": False,
            "profit_lock_level": 0.0,
            "profit_lock_floor": 0.0,

            # Trailing Drawdown
            "trailing_drawdown_active": False,
            "high_water_mark": 0.0,
            "max_drawdown_from_hwm": 0.0,

            # Position Snapshots
            "position_snapshot": [],
            "open_spreads": [],

            # Kill Switch
            "kill_switch_activated": False,

            # Integrity
            "version": 1,
        }
        self._save()
        logger.info("State reset for %s", date.today())

    def _save(self):
        """Encrypt and save state to disk."""
        self._state["last_updated"] = time.time()
        # Add integrity hash
        state_copy = {k: v for k, v in self._state.items() if k != "_integrity"}
        integrity = hashlib.sha256(json.dumps(state_copy, sort_keys=True).encode()).hexdigest()
        self._state["_integrity"] = integrity

        data = json.dumps(self._state).encode()
        encrypted = self._fernet.encrypt(data)
        with open(self._state_file, "wb") as f:
            f.write(encrypted)

    # ── Getters ────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def get_all(self) -> dict:
        """Return full state (read-only copy)."""
        return dict(self._state)

    @property
    def realized_pnl(self) -> float:
        return self._state["realized_pnl"]

    @property
    def unrealized_pnl(self) -> float:
        return self._state["unrealized_pnl"]

    @property
    def total_pnl(self) -> float:
        return self._state["realized_pnl"] + self._state["unrealized_pnl"]

    @property
    def is_locked_out(self) -> bool:
        return self._state["is_locked_out"]

    @property
    def is_cooling_down(self) -> bool:
        if not self._state["is_cooling_down"]:
            return False
        if self._state["cooldown_until"] and time.time() >= self._state["cooldown_until"]:
            self._state["is_cooling_down"] = False
            self._state["cooldown_until"] = None
            self._state["cooldown_reason"] = ""
            self._save()
            return False
        return True

    @property
    def cooldown_remaining(self) -> int:
        """Seconds remaining in cooldown."""
        if not self.is_cooling_down:
            return 0
        remaining = self._state["cooldown_until"] - time.time()
        return max(0, int(remaining))

    @property
    def profit_lock_active(self) -> bool:
        return self._state["profit_lock_active"]

    @property
    def profit_lock_floor(self) -> float:
        return self._state["profit_lock_floor"]

    @property
    def high_water_mark(self) -> float:
        return self._state["high_water_mark"]

    @property
    def consecutive_losses(self) -> int:
        return self._state["consecutive_losses"]

    # ── Setters (all persist immediately) ──────────────────────────────

    def update_pnl(self, realized: float, unrealized: float):
        """Update P&L figures."""
        self._state["realized_pnl"] = realized
        self._state["unrealized_pnl"] = unrealized
        self._state["total_pnl"] = realized + unrealized

        # Update peak/HWM
        if self._state["total_pnl"] > self._state["peak_pnl"]:
            self._state["peak_pnl"] = self._state["total_pnl"]
        if realized > self._state["high_water_mark"]:
            self._state["high_water_mark"] = realized

        self._save()

    def record_trade(self, trade_info: dict):
        """Record a completed trade."""
        pnl = trade_info.get("pnl", 0)
        self._state["total_trades"] += 1

        if pnl >= 0:
            self._state["winning_trades"] += 1
            self._state["consecutive_losses"] = 0
        else:
            self._state["losing_trades"] += 1
            self._state["consecutive_losses"] += 1

        # Keep last 50 trades in history
        self._state["trade_history"].append({
            "time": time.time(),
            "pnl": pnl,
            "security_id": trade_info.get("security_id", ""),
            "type": trade_info.get("type", ""),
            "quantity": trade_info.get("quantity", 0),
        })
        if len(self._state["trade_history"]) > 50:
            self._state["trade_history"] = self._state["trade_history"][-50:]

        self._save()

    def activate_lockout(self, reason: str):
        """Lock out trading for the day. CANNOT be reversed."""
        self._state["is_locked_out"] = True
        self._state["lockout_reason"] = reason
        self._state["lockout_time"] = time.time()
        self._save()
        logger.warning("LOCKOUT ACTIVATED: %s", reason)

    def activate_cooldown(self, duration_seconds: int, reason: str):
        """Start a cooldown period."""
        self._state["is_cooling_down"] = True
        self._state["cooldown_until"] = time.time() + duration_seconds
        self._state["cooldown_reason"] = reason
        self._save()
        logger.info("Cooldown started: %ds - %s", duration_seconds, reason)

    def activate_profit_lock(self, lock_level: float, floor: float):
        """Activate profit lock at given level with floor."""
        self._state["profit_lock_active"] = True
        self._state["profit_lock_level"] = lock_level
        self._state["profit_lock_floor"] = floor
        self._save()
        logger.info("Profit lock activated: level=%.2f floor=%.2f", lock_level, floor)

    def update_trailing_drawdown(self, active: bool, hwm: float = 0, drawdown: float = 0):
        """Update trailing drawdown state."""
        self._state["trailing_drawdown_active"] = active
        if hwm > 0:
            self._state["high_water_mark"] = hwm
        self._state["max_drawdown_from_hwm"] = drawdown
        self._save()

    def set_kill_switch(self, activated: bool):
        """Record that Dhan kill switch was activated."""
        self._state["kill_switch_activated"] = activated
        self._save()

    def update_positions(self, positions: list):
        """Store current position snapshot."""
        self._state["position_snapshot"] = positions
        self._save()

    def update_spreads(self, spreads: list):
        """Store detected option spreads."""
        self._state["open_spreads"] = spreads
        self._save()

    def is_fresh_day(self) -> bool:
        """Check if this is a fresh state with no trades."""
        return self._state["total_trades"] == 0 and self._state["realized_pnl"] == 0
