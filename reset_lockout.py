#!/usr/bin/env python3
"""
Reset Lockout - Administrative script.
Safely clears active lockouts in today's daily state file while the service is stopped.
"""

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def main():
    try:
        from state_manager import StateManager
        
        state = StateManager()
        if not state.is_locked_out:
            logger.info("No active lockout detected in today's state file.")
            sys.exit(0)
            
        old_reason = state.get("lockout_reason")
        state._state["is_locked_out"] = False
        state._state["lockout_reason"] = ""
        state._state["lockout_time"] = None
        # Also reset active floors and HWM to prevent instant re-lockout on start
        state._state["high_water_mark"] = 0.0
        state._state["drawdown_active"] = False
        state._state["profit_lock_active"] = False
        state._state["profit_lock_floor"] = 0.0
        state._state["profit_lock_level"] = 0.0
        state._save()
        
        logger.info("SUCCESS: Lockout state cleared successfully! (Previous reason: %s)", old_reason)
    except Exception as e:
        logger.error("Failed to reset lockout: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
