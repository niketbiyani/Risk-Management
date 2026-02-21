#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════
  RISK MANAGEMENT SAFETY SYSTEMS — LIVE SIMULATION
═══════════════════════════════════════════════════════════════════

Simulates a full trading day with realistic scenarios to demonstrate
how daily loss limits, profit lock, and trailing drawdown work together.

Run: python simulate_safety.py
"""

import os
import sys
import time
import shutil
import tempfile

# Setup isolated state
_sim_dir = tempfile.mkdtemp(prefix="risk_sim_")
os.environ["STATE_ENCRYPTION_KEY"] = ""
os.environ["DHAN_CLIENT_ID"] = "SIM_TRADER"
os.environ["DHAN_ACCESS_TOKEN"] = "sim"

sys.path.insert(0, os.path.dirname(__file__))

from config import Config
Config.STATE_DIR = _sim_dir

from state_manager import StateManager
from risk_engine import RiskEngine

# ── Formatting helpers ──────────────────────────────────────────

def _fresh_state():
    """Wipe and recreate state dir for a clean scenario."""
    for f in os.listdir(Config.STATE_DIR):
        os.remove(os.path.join(Config.STATE_DIR, f))

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

def header(title):
    print(f"\n{BOLD}{'=' * 64}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'=' * 64}{RESET}\n")

def subheader(title):
    print(f"\n{CYAN}{BOLD}--- {title} ---{RESET}\n")

def trade(desc):
    print(f"  {YELLOW}>> TRADE:{RESET} {desc}")

def status_line(state, risk):
    s = risk.get_risk_status()
    pnl = s["pnl"]
    lim = s["limits"]
    can = s["can_trade"]
    lock = s["lockout"]["active"]

    pnl_color = GREEN if pnl["total"] >= 0 else RED
    status_text = f"{GREEN}ACTIVE{RESET}" if can else f"{RED}LOCKED OUT{RESET}"

    print(f"     Status: {status_text}")
    print(f"     P&L:    {pnl_color}₹{pnl['total']:,.0f}{RESET}  (R: ₹{pnl['realized']:,.0f}  U: ₹{pnl['unrealized']:,.0f})")
    print(f"     Buffer: ₹{lim['loss_remaining']:,.0f} remaining of ₹{Config.DAILY_MAX_LOSS:,.0f} limit")

    if s["trailing_drawdown"].get("enabled") and s["trailing_drawdown"]["high_water_mark"] > 0:
        dd = s["trailing_drawdown"]
        print(f"     HWM:    ₹{dd['high_water_mark']:,.0f}  |  Drawdown: ₹{dd['current_drawdown']:,.0f}  |  DD Limit: ₹{dd['drawdown_limit']:,.0f}")

    if s["profit_lock"].get("active"):
        pl = s["profit_lock"]
        print(f"     Profit Lock: ACTIVE  |  Floor: ₹{pl['floor']:,.0f}  |  Buffer: ₹{pl['buffer']:,.0f}")

    if lock:
        print(f"     {RED}{BOLD}Lockout reason: {s['lockout']['reason']}{RESET}")
    print()

def check_order(risk, qty, risk_amt, positions=0):
    decision = risk.check_new_order(
        order_quantity=qty,
        num_open_positions=positions,
        estimated_risk=risk_amt,
    )
    if decision.allowed:
        print(f"     {GREEN}ORDER APPROVED{RESET}: qty={qty}, risk=₹{risk_amt:,.0f}")
    else:
        print(f"     {RED}ORDER BLOCKED{RESET}: {decision.reason} [{decision.action}]")
    return decision.allowed

def pause():
    print(f"  {DIM}(press Enter to continue...){RESET}")
    input()


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO 1: Normal Day — Daily Loss Limit Hit
# ═══════════════════════════════════════════════════════════════════

def scenario_daily_loss():
    header("SCENARIO 1: DAILY LOSS LIMIT")
    print(f"  Config: DAILY_MAX_LOSS = ₹{Config.DAILY_MAX_LOSS:,.0f}")
    print(f"  A trader has a bad morning. Each trade goes against them.")
    print(f"  Watch how the system protects the account.\n")
    pause()

    _fresh_state()
    state = StateManager()
    risk = RiskEngine(state)

    # Trade 1: Small loss
    subheader("9:20 AM — Trade 1: Short NIFTY 25500 CE")
    trade("Sold NIFTY 25500 CE @ ₹120, SL hit @ ₹155 → Loss ₹35 x 25 = -₹875")
    risk.evaluate_pnl(-875, 0)
    risk.evaluate_trade_result(-875, {"security_id": "CE25500"})
    status_line(state, risk)
    pause()

    # Trade 2: Another loss
    subheader("9:45 AM — Trade 2: Long NIFTY 25400 PE")
    trade("Bought NIFTY 25400 PE @ ₹90, SL hit @ ₹65 → Loss ₹25 x 50 = -₹1,250")
    risk.evaluate_pnl(-2125, 0)
    risk.evaluate_trade_result(-1250, {"security_id": "PE25400"})
    status_line(state, risk)
    pause()

    # Trade 3: Loss continues
    subheader("10:15 AM — Trade 3: Short BANKNIFTY 54000 CE")
    trade("Sold BNF 54000 CE @ ₹200, SL hit @ ₹280 → Loss ₹80 x 25 = -₹2,000")
    risk.evaluate_pnl(-4125, 0)
    risk.evaluate_trade_result(-2000, {"security_id": "CE54000"})
    status_line(state, risk)
    pause()

    # Trade 4: Unrealized loss pushes over the limit
    subheader("10:30 AM — Trade 4: Long NIFTY Futures (still open)")
    trade("Bought NIFTY FUT @ 25,480, drops to 25,440 → Unrealized loss: -₹2,000")
    risk.evaluate_pnl(-4125, -2000)
    print(f"     {RED}{BOLD}TOTAL P&L: -₹6,125 exceeds -₹{Config.DAILY_MAX_LOSS:,.0f} limit!{RESET}")
    status_line(state, risk)
    pause()

    # Try to place order
    subheader("10:31 AM — Trader tries ANY order")
    check_order(risk, qty=1, risk_amt=100, positions=0)
    print(f"\n  {RED}{BOLD}LOCKOUT ACTIVATED:{RESET}")
    print(f"  1. All pending orders → CANCELLED")
    print(f"  2. All open positions → CLOSED at MARKET")
    print(f"  3. Dhan Kill Switch → ACTIVATED")
    print(f"  4. State encrypted → Cannot be bypassed until tomorrow")
    pause()


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO 2: Trailing Drawdown — Protecting Profits (Total P&L)
# ═══════════════════════════════════════════════════════════════════

def scenario_trailing_drawdown():
    header("SCENARIO 2: TRAILING DRAWDOWN (TOTAL P&L)")
    print(f"  Config: PROFIT_LOCK_THRESHOLD = ₹{Config.PROFIT_LOCK_THRESHOLD:,.0f}")
    print(f"          PROFIT_LOCK_PERCENTAGE = {Config.PROFIT_LOCK_PERCENTAGE:.0f}%")
    print(f"          TRAILING_DRAWDOWN = {Config.TRAILING_DRAWDOWN_PERCENTAGE:.0f}%")
    print(f"  HWM tracks TOTAL P&L (realized + unrealized).")
    print(f"  Once you're profitable enough, the system protects your gains.\n")
    pause()

    _fresh_state()
    state = StateManager()
    risk = RiskEngine(state)

    # Morning: Building profits
    subheader("Phase 1: Building Profits (9:15 - 11:00 AM)")

    profit_steps = [
        (2000, 0, "9:20 — Scalped NIFTY CE for ₹2,000"),
        (5000, 0, "9:45 — BNF straddle adjustment netted ₹3,000 more"),
        (8000, 0, "10:15 — Another successful CE scalp, ₹3,000"),
        (10000, 0, "10:30 — PE credit spread profit: ₹2,000"),
    ]

    for realized, unrealized, desc in profit_steps:
        print(f"  {GREEN}+{RESET} {desc}")
        result = risk.evaluate_pnl(realized, unrealized)

        if result == "profit_lock_activated":
            floor = state.profit_lock_floor
            print(f"\n     {CYAN}{BOLD}PROFIT LOCK ACTIVATED!{RESET}")
            print(f"     Realized: ₹{realized:,.0f} >= Threshold: ₹{Config.PROFIT_LOCK_THRESHOLD:,.0f}")
            print(f"     Floor set: ₹{floor:,.0f} ({Config.PROFIT_LOCK_PERCENTAGE:.0f}% of ₹{realized:,.0f})")
            print(f"     {DIM}If realized P&L drops below ₹{floor:,.0f}, system locks out{RESET}")

        s = risk.get_risk_status()
        dd = s["trailing_drawdown"]
        if dd.get("high_water_mark", 0) > 0:
            print(f"     HWM: ₹{dd['high_water_mark']:,.0f}")
        print()

    pause()

    # Show unrealized boosting HWM
    subheader("Phase 2: Unrealized Gains Push HWM Higher (11:00 - 12:30 PM)")
    print(f"  Realized stays at ₹10,000 but open positions are in profit.\n")

    unrealized_steps = [
        (10000, 3000, "11:15 — Open positions running +₹3,000 unrealized"),
        (10000, 6000, "11:45 — Unrealized climbs to +₹6,000 (total: ₹16,000)"),
        (10000, 8000, "12:15 — Peak! Unrealized +₹8,000 (total: ₹18,000)"),
    ]

    for realized, unrealized, desc in unrealized_steps:
        total = realized + unrealized
        print(f"  {GREEN}+{RESET} {desc}")
        risk.evaluate_pnl(realized, unrealized)
        s = risk.get_risk_status()
        dd = s["trailing_drawdown"]
        print(f"     Total P&L: ₹{total:,.0f}  |  HWM: ₹{dd['high_water_mark']:,.0f}  |  DD Limit: ₹{dd['drawdown_limit']:,.0f}")
        print()

    print(f"  {CYAN}KEY: HWM rose to ₹18,000 even though realized is only ₹10,000{RESET}")
    print(f"  {CYAN}     Drawdown limit = 50% of ₹18,000 = ₹9,000{RESET}")
    pause()

    # Close positions and give back
    subheader("Phase 3: Closing Positions & Giving Back (1:00 - 2:30 PM)")
    print(f"  Trader closes profitable positions, then starts losing.\n")

    decline_steps = [
        (15000, 0, "1:00 — Closed positions, realized now ₹15,000"),
        (13000, 0, "1:30 — Loss brings realized to ₹13,000"),
        (11000, 0, "2:00 — Another loss, realized ₹11,000"),
        (9000,  0, "2:30 — Continued losses, realized ₹9,000"),
    ]

    for realized, unrealized, desc in decline_steps:
        total = realized + unrealized
        print(f"  {RED}-{RESET} {desc}")
        result = risk.evaluate_pnl(realized, unrealized)
        s = risk.get_risk_status()
        dd = s["trailing_drawdown"]
        buffer = dd["buffer"]
        pct_used = (dd["current_drawdown"] / dd["drawdown_limit"] * 100) if dd["drawdown_limit"] > 0 else 0

        bar_width = 30
        filled = int(pct_used / 100 * bar_width)
        bar_color = GREEN if pct_used < 50 else (YELLOW if pct_used < 80 else RED)
        bar = f"{bar_color}{'█' * filled}{DIM}{'░' * (bar_width - filled)}{RESET}"

        print(f"     Drawdown: ₹{dd['current_drawdown']:,.0f} / ₹{dd['drawdown_limit']:,.0f}  [{bar}] {pct_used:.0f}%")
        print(f"     Buffer remaining: ₹{buffer:,.0f}")

        if result == "lockout_trailing_drawdown":
            print(f"\n     {RED}{BOLD}TRAILING DRAWDOWN LOCKOUT!{RESET}")
            print(f"     Drew down ₹{dd['current_drawdown']:,.0f} from HWM ₹{dd['high_water_mark']:,.0f}")
            print(f"     Trader keeps ₹{realized:,.0f} — system saved profits!")
            break
        elif result == "lockout_profit_lock":
            print(f"\n     {RED}{BOLD}PROFIT LOCK FLOOR BREACHED!{RESET}")
            print(f"     P&L ₹{realized:,.0f} fell below floor ₹{state.profit_lock_floor:,.0f}")
            print(f"     System protected minimum ₹{state.profit_lock_floor:,.0f} of profits!")
            break
        print()

    pause()


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO 3: Unrealized P&L Drawdown — The Key Scenario
# ═══════════════════════════════════════════════════════════════════

def scenario_unrealized_drawdown():
    header("SCENARIO 3: UNREALIZED DRAWDOWN SCENARIO")
    print(f"  This demonstrates the exact scenario discussed:")
    print(f"  Unrealized hits ₹18,000 → close at ₹15,000 → how far from lockout?\n")
    pause()

    _fresh_state()
    state = StateManager()
    risk = RiskEngine(state)

    subheader("Step 1: Build unrealized profit")
    trade("Open positions running. Realized: ₹0, Unrealized: ₹18,000")
    risk.evaluate_pnl(0, 18000)
    s = risk.get_risk_status()
    dd = s["trailing_drawdown"]
    print(f"     Total P&L: ₹18,000")
    print(f"     HWM: ₹{dd['high_water_mark']:,.0f}")
    print(f"     Drawdown limit: ₹{dd['drawdown_limit']:,.0f} (50% of HWM)")
    print(f"     Current drawdown: ₹{dd['current_drawdown']:,.0f}")
    print()
    pause()

    subheader("Step 2: Close the position at ₹15,000")
    trade("Closed position. Realized: ₹15,000, Unrealized: ₹0")
    risk.evaluate_pnl(15000, 0)
    s = risk.get_risk_status()
    dd = s["trailing_drawdown"]
    total = 15000
    drawdown = dd["current_drawdown"]
    buffer = dd["buffer"]
    limit = dd["drawdown_limit"]

    print(f"     Total P&L: ₹{total:,.0f}")
    print(f"     HWM: ₹{dd['high_water_mark']:,.0f} (still ₹18,000 — it NEVER goes down)")
    print(f"     Drawdown: ₹{drawdown:,.0f} (from HWM)")
    print(f"     DD Limit: ₹{limit:,.0f}")
    print(f"     {CYAN}{BOLD}Buffer remaining: ₹{buffer:,.0f}{RESET}")
    print()
    print(f"  {GREEN}Answer: You are ₹{buffer:,.0f} away from being locked out.{RESET}")
    print(f"  {DIM}(HWM ₹18,000 → 50% limit = ₹9,000 drawdown allowed)")
    print(f"  {DIM}(Current drawdown = ₹18,000 - ₹15,000 = ₹3,000)")
    print(f"  {DIM}(Buffer = ₹9,000 - ₹3,000 = ₹6,000){RESET}")
    pause()

    subheader("Step 3: What if losses continue?")
    further_losses = [
        (12000, 0, "Realized drops to ₹12,000"),
        (10000, 0, "Realized drops to ₹10,000"),
        (9000,  0, "Realized drops to ₹9,000 — RIGHT at the edge!"),
    ]

    for realized, unrealized, desc in further_losses:
        total = realized + unrealized
        print(f"  {RED}-{RESET} {desc}")
        result = risk.evaluate_pnl(realized, unrealized)
        s = risk.get_risk_status()
        dd = s["trailing_drawdown"]
        buffer = dd["buffer"]
        pct_used = (dd["current_drawdown"] / dd["drawdown_limit"] * 100) if dd["drawdown_limit"] > 0 else 0

        bar_width = 30
        filled = int(pct_used / 100 * bar_width)
        bar_color = GREEN if pct_used < 50 else (YELLOW if pct_used < 80 else RED)
        bar = f"{bar_color}{'█' * filled}{DIM}{'░' * (bar_width - filled)}{RESET}"

        print(f"     Drawdown: ₹{dd['current_drawdown']:,.0f} / ₹{dd['drawdown_limit']:,.0f}  [{bar}] {pct_used:.0f}%")
        print(f"     Buffer: ₹{buffer:,.0f}")

        if result == "lockout_trailing_drawdown":
            print(f"\n     {RED}{BOLD}LOCKED OUT! Drawdown limit reached.{RESET}")
            print(f"     Trader saved ₹{realized:,.0f} of their ₹18,000 peak profits.")
            break
        elif result == "lockout_profit_lock":
            print(f"\n     {RED}{BOLD}PROFIT LOCK FLOOR BREACHED!{RESET}")
            print(f"     Realized ₹{realized:,.0f} dropped below floor ₹{state.profit_lock_floor:,.0f}")
            break
        print()

    pause()


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO 4: Order Interceptor in Action
# ═══════════════════════════════════════════════════════════════════

def scenario_order_interceptor():
    header("SCENARIO 4: ORDER INTERCEPTOR")
    print(f"  Shows what happens at the order level — every order passes through")
    print(f"  the interceptor before reaching the exchange.\n")
    pause()

    _fresh_state()
    state = StateManager()
    risk = RiskEngine(state)

    subheader("Normal Orders")
    print(f"  Trader places several orders:\n")

    orders = [
        (25, 1500, 0, "BUY NIFTY 25500 CE @ ₹60, risk=₹1,500"),
        (50, 1800, 0, "BUY BNF 54000 PE @ ₹36, risk=₹1,800"),
        (25, 500, 1, "SELL NIFTY 25500 CE (closing position), risk=₹500"),
    ]

    for qty, risk_amt, positions, desc in orders:
        trade(desc)
        check_order(risk, qty, risk_amt, positions)
        print()

    pause()

    subheader("Blocked Orders")
    print(f"  Now some orders that violate rules:\n")

    blocked = [
        (Config.MAX_ORDER_QUANTITY + 100, 1000, 0,
         f"BUY qty={Config.MAX_ORDER_QUANTITY + 100} (exceeds max {Config.MAX_ORDER_QUANTITY})"),
        (25, Config.MAX_SINGLE_TRADE_RISK + 500, 0,
         f"SELL naked CE, risk=₹{Config.MAX_SINGLE_TRADE_RISK + 500:,.0f} (exceeds max ₹{Config.MAX_SINGLE_TRADE_RISK:,.0f})"),
        (25, 1000, Config.MAX_OPEN_POSITIONS,
         f"BUY with {Config.MAX_OPEN_POSITIONS} positions open (max reached)"),
    ]

    for qty, risk_amt, positions, desc in blocked:
        trade(desc)
        check_order(risk, qty, risk_amt, positions)
        print()

    pause()

    subheader("After Lockout")
    state.activate_lockout("Daily loss limit: ₹-5,250")
    print(f"  {RED}System is now LOCKED OUT{RESET}\n")

    trade("ANY order attempt — even tiny, safe order:")
    check_order(risk, qty=1, risk_amt=10, positions=0)
    print(f"\n  {DIM}Nothing gets through. Kill switch active until tomorrow.{RESET}")
    pause()


# ═══════════════════════════════════════════════════════════════════
#  Main Menu
# ═══════════════════════════════════════════════════════════════════

def main():
    print(f"""
{BOLD}╔══════════════════════════════════════════════════════════════╗
║       RISK MANAGEMENT SAFETY SYSTEMS — SIMULATION          ║
╠══════════════════════════════════════════════════════════════╣
║                                                            ║
║  Current Config:                                           ║
║    Daily Max Loss:      ₹{Config.DAILY_MAX_LOSS:>8,.0f}                        ║
║    Max Single Trade:    ₹{Config.MAX_SINGLE_TRADE_RISK:>8,.0f}                        ║
║    Max Positions:       {Config.MAX_OPEN_POSITIONS:>8}                        ║
║    Profit Lock:         {Config.PROFIT_LOCK_PERCENTAGE:>7.0f}% after ₹{Config.PROFIT_LOCK_THRESHOLD:,.0f}          ║
║    Trailing Drawdown:   {Config.TRAILING_DRAWDOWN_PERCENTAGE:>7.0f}% of HWM (total P&L)     ║
║                                                            ║
╠══════════════════════════════════════════════════════════════╣
║  Scenarios:                                                ║
║    1. Daily Loss Limit — bad day, system locks out         ║
║    2. Trailing Drawdown — protecting profits from giveback ║
║    3. Unrealized Drawdown — HWM tracks total P&L demo     ║
║    4. Order Interceptor — per-order blocking examples      ║
║    A. Run ALL scenarios                                    ║
║    Q. Quit                                                 ║
╚══════════════════════════════════════════════════════════════╝{RESET}
""")

    while True:
        choice = input(f"  {BOLD}Select scenario (1-4, A, Q): {RESET}").strip().upper()

        if choice == "1":
            scenario_daily_loss()
        elif choice == "2":
            scenario_trailing_drawdown()
        elif choice == "3":
            scenario_unrealized_drawdown()
        elif choice == "4":
            scenario_order_interceptor()
        elif choice == "A":
            scenario_daily_loss()
            scenario_trailing_drawdown()
            scenario_unrealized_drawdown()
            scenario_order_interceptor()
            header("ALL SCENARIOS COMPLETE")
            print(f"  All safety systems demonstrated successfully.")
            print(f"  Your trading account is protected from:")
            print(f"    - Daily max loss breaches (hard lockout)")
            print(f"    - Giving back profits (trailing drawdown from total P&L)")
            print(f"    - Oversized orders (interceptor)")
            print(f"    - Over-leveraging (position limits)")
            print()
            break
        elif choice == "Q":
            break
        else:
            print(f"  Invalid choice. Enter 1-4, A, or Q.")

    # Cleanup
    shutil.rmtree(_sim_dir, ignore_errors=True)
    print(f"\n  {DIM}Simulation state cleaned up.{RESET}\n")


if __name__ == "__main__":
    main()
