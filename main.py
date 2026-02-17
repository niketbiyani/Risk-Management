"""
Trade Management Platform - Main Entry Point.

Starts both the position monitor and the web dashboard.
The monitor runs in a background thread while the dashboard
serves the web UI on the configured port.

Usage:
    python main.py              # Start full platform (monitor + dashboard)
    python main.py --monitor    # Start monitor only (no dashboard)
    python main.py --dashboard  # Start dashboard only (no monitor)
    python main.py --status     # Show current risk status and exit
"""

import argparse
import logging
import sys
import threading
import time

from config import Config
from dhan_api import DhanAPI
from state_manager import StateManager
from risk_engine import RiskEngine
from monitor import PositionMonitor
from dashboard import run_dashboard, emit_status_update, set_instrument_cache
from instrument_cache import InstrumentCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("platform.log"),
    ],
)
logger = logging.getLogger(__name__)


def print_status():
    """Print current risk status to console."""
    state = StateManager()
    risk = RiskEngine(state)
    status = risk.get_risk_status()

    print("\n" + "=" * 60)
    print("  TRADE MANAGEMENT PLATFORM - STATUS")
    print("=" * 60)

    # Trading status
    can_trade = status["can_trade"]
    if status["lockout"]["active"]:
        print(f"  Status:  LOCKED OUT")
        print(f"  Reason:  {status['lockout']['reason']}")
    elif status["cooldown"]["active"]:
        print(f"  Status:  COOLDOWN ({status['cooldown']['remaining_seconds']}s)")
        print(f"  Reason:  {status['cooldown']['reason']}")
    else:
        print(f"  Status:  ACTIVE")

    print()
    print(f"  Total P&L:     ₹{status['pnl']['total']:>10,.0f}")
    print(f"  Realized:      ₹{status['pnl']['realized']:>10,.0f}")
    print(f"  Unrealized:    ₹{status['pnl']['unrealized']:>10,.0f}")
    print(f"  Peak P&L:      ₹{status['pnl']['peak']:>10,.0f}")

    print()
    print(f"  Loss Limit:    ₹{status['limits']['daily_max_loss']:>10,.0f}")
    print(f"  Loss Buffer:   ₹{status['limits']['loss_remaining']:>10,.0f}  "
          f"({100 - status['limits']['loss_used_pct']:.0f}% remaining)")

    if status["profit_lock"]["active"]:
        print(f"  Profit Lock:   ACTIVE (floor ₹{status['profit_lock']['floor']:,.0f}, "
              f"buffer ₹{status['profit_lock']['buffer']:,.0f})")
    else:
        print(f"  Profit Lock:   Inactive (₹{status['profit_lock'].get('distance', 0):,.0f} "
              f"to threshold)")

    if status["trailing_drawdown"].get("enabled"):
        dd = status["trailing_drawdown"]
        print(f"  HWM:           ₹{dd['high_water_mark']:>10,.0f}")
        print(f"  Drawdown:      ₹{dd['current_drawdown']:>10,.0f}  "
              f"(limit ₹{dd['drawdown_limit']:,.0f})")

    print()
    t = status["trades"]
    print(f"  Trades:        {t['total']}  (W:{t['winners']} L:{t['losers']})")
    print(f"  Win Rate:      {t['win_rate']:.1f}%")
    print(f"  Consec Losses: {t['consecutive_losses']}")

    print()
    print(f"  Kill Switch:   {'ACTIVATED' if status['kill_switch'] else 'Normal'}")
    print("=" * 60 + "\n")


def run_full():
    """Start both monitor and dashboard."""
    errors = Config.validate()
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        print("\nPlease configure your .env file. See .env.example for reference.")
        sys.exit(1)

    # Load instrument data for search and lot size lookups
    logger.info("Loading instrument data from Dhan...")
    instrument_cache = InstrumentCache()
    try:
        instrument_cache.load()
        logger.info("Loaded %d instruments", instrument_cache.count)
    except Exception as e:
        logger.warning("Failed to load instruments (search will be unavailable): %s", e)
    set_instrument_cache(instrument_cache)

    monitor = PositionMonitor()

    # Start monitor in background thread
    monitor_thread = threading.Thread(target=monitor.start, daemon=True)
    monitor_thread.start()

    # Push status updates to dashboard
    def status_pusher():
        while True:
            try:
                if monitor._running:
                    status = monitor.get_status()
                    emit_status_update(status)
            except Exception:
                pass
            time.sleep(Config.MONITOR_INTERVAL)

    pusher_thread = threading.Thread(target=status_pusher, daemon=True)
    pusher_thread.start()

    logger.info("Dashboard starting on http://%s:%d", Config.DASHBOARD_HOST, Config.DASHBOARD_PORT)

    # Dashboard runs in main thread (Flask)
    run_dashboard(monitor)


def main():
    parser = argparse.ArgumentParser(description="Trade Management Platform")
    parser.add_argument("--monitor", action="store_true", help="Run monitor only")
    parser.add_argument("--dashboard", action="store_true", help="Run dashboard only")
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.monitor:
        from monitor import run_monitor
        run_monitor()
    elif args.dashboard:
        errors = Config.validate()
        if errors:
            for e in errors:
                logger.error("Config error: %s", e)
            sys.exit(1)
        instrument_cache = InstrumentCache()
        try:
            instrument_cache.load()
        except Exception:
            pass
        set_instrument_cache(instrument_cache)
        monitor = PositionMonitor()
        logger.info("Dashboard starting on http://%s:%d", Config.DASHBOARD_HOST, Config.DASHBOARD_PORT)
        run_dashboard(monitor)
    else:
        run_full()


if __name__ == "__main__":
    main()
