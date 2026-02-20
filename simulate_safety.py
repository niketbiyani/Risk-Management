#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════
  RISK MANAGEMENT SAFETY SYSTEMS — LIVE SIMULATION
═══════════════════════════════════════════════════════════════════

Simulates a full trading day with realistic scenarios to demonstrate
how daily loss limits, cooldowns, and trailing drawdown work together.

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
    cool = s["cooldown"]["active"]

    pnl_color = GREEN if pnl["total"] >= 0 else RED
    status_text = f"{GREEN}ACTIVE{RESET}" if can else (
        f"{RED}LOCKED OUT{RESET}" if lock else f"{YELLOW}COOLDOWN ({s['cooldown']['remaining_seconds']}s){RESET}"
    )

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

    # Trade 3: Big loss
    subheader("10:15 AM — Trade 3: Short BANKNIFTY 54000 CE")
    trade("Sold BNF 54000 CE @ ₹200, SL hit @ ₹280 → Loss ₹80 x 25 = -₹2,000")
    risk.evaluate_pnl(-4125, 0)
    risk.evaluate_trade_result(-2000, {"security_id": "CE54000"})
    print(f"     {YELLOW}NOTE: Large loss (₹2,000 >= 80% of ₹{Config.MAX_SINGLE_TRADE_RISK:,.0f}) triggers cooldown!{RESET}")
    status_line(state, risk)
    pause()

    # Trade 4: Tries to trade during cooldown
    subheader("10:20 AM — Trader tries to place another order (during cooldown)")
    check_order(risk, qty=25, risk_amt=1500, positions=0)
    print(f"     {DIM}System forces a 5-minute pause to prevent revenge trading{RESET}")
    pause()

    # Expire cooldown manually for simulation
    state._state["is_cooling_down"] = False
    state._state["cooldown_until"] = None
    state._save()

    # Trade 4: Final loss hits limit
    subheader("10:30 AM — Trade 4: Long NIFTY Futures")
    trade("Bought NIFTY FUT @ 25,480, drops to 25,440 → Unrealized loss: -₹2,000")
    risk.evaluate_pnl(-4125, -2000)
    print(f"     {RED}{BOLD}TOTAL P&L: -₹6,125 exceeds -₹5,000 limit!{RESET}")
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
#  SCENARIO 2: Cooldowns — Consecutive Losses
# ═══════════════════════════════════════════════════════════════════

def scenario_cooldowns():
    header("SCENARIO 2: COOLDOWN SYSTEM (LOSS BUFFER)")
    print(f"  Config: CONSECUTIVE_LOSS_COUNT = {Config.CONSECUTIVE_LOSS_COUNT}")
    print(f"          COOLDOWN_AFTER_LOSS = {Config.COOLDOWN_AFTER_LOSS}s (large single loss)")
    print(f"          COOLDOWN_AFTER_CONSECUTIVE = {Config.COOLDOWN_AFTER_CONSECUTIVE_LOSSES}s")
    print(f"  Cooldowns act as a circuit breaker — forced pause to break tilt.\n")
    pause()

    _fresh_state()
    state = StateManager()
    risk = RiskEngine(state)

    losses = [
        (-300, "Short NIFTY 25600 CE — SL hit, loss ₹300"),
        (-450, "Long NIFTY 25500 PE — expired worthless, loss ₹450"),
        (-600, "Short BNF 54200 CE — SL hit, loss ₹600"),
    ]

    for i, (pnl, desc) in enumerate(losses, 1):
        subheader(f"Trade {i}")
        trade(desc)
        realized = sum(l[0] for l in losses[:i])
        risk.evaluate_pnl(realized, 0)
        risk.evaluate_trade_result(pnl, {"security_id": f"T{i}"})
        consecutive = state.consecutive_losses
        print(f"     Consecutive losses: {consecutive}/{Config.CONSECUTIVE_LOSS_COUNT}")

        if state.is_cooling_down:
            remaining = state.cooldown_remaining
            print(f"     {YELLOW}{BOLD}COOLDOWN TRIGGERED!{RESET} {remaining}s pause enforced")
            print(f"     Reason: {state.get('cooldown_reason')}")
            print()
            check_order(risk, qty=25, risk_amt=500, positions=0)
        else:
            print(f"     {GREEN}Trading still allowed{RESET}")
        print()
        pause()

    # Win resets streak
    state._state["is_cooling_down"] = False
    state._state["cooldown_until"] = None
    state._save()

    subheader("After cooldown — Trade 4: A winning trade")
    trade("Long NIFTY 25550 CE — profit ₹800!")
    risk.evaluate_pnl(-1350 + 800, 0)
    risk.evaluate_trade_result(800, {"security_id": "T4"})
    print(f"     Consecutive losses: {state.consecutive_losses} (RESET by win!)")
    print(f"     {GREEN}Streak broken — trader can continue with fresh head{RESET}")
    pause()


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO 3: Trailing Drawdown — Protecting Profits
# ═══════════════════════════════════════════════════════════════════

def scenario_trailing_drawdown():
    header("SCENARIO 3: TRAILING DRAWDOWN")
    print(f"  Config: PROFIT_LOCK_THRESHOLD = ₹{Config.PROFIT_LOCK_THRESHOLD:,.0f}")
    print(f"          PROFIT_LOCK_PERCENTAGE = {Config.PROFIT_LOCK_PERCENTAGE}%")
    print(f"          TRAILING_DRAWDOWN = {Config.TRAILING_DRAWDOWN_PERCENTAGE}%")
    print(f"  Once you're profitable enough, the system protects your gains.\n")
    pause()

    _fresh_state()
    state = StateManager()
    risk = RiskEngine(state)

    # Morning: Building profits
    subheader("Phase 1: Building Profits (9:15 - 11:00 AM)")

    profit_steps = [
        (2000, "9:20 — Scalped NIFTY CE for ₹2,000"),
        (5000, "9:45 — BNF straddle adjustment netted ₹3,000 more"),
        (8000, "10:15 — Another successful CE scalp, ₹3,000"),
        (10000, "10:30 — PE credit spread profit: ₹2,000"),
    ]

    for realized, desc in profit_steps:
        print(f"  {GREEN}+{RESET} {desc}")
        result = risk.evaluate_pnl(realized, 0)

        if result == "profit_lock_activated":
            floor = state.profit_lock_floor
            print(f"\n     {CYAN}{BOLD}PROFIT LOCK ACTIVATED!{RESET}")
            print(f"     Realized: ₹{realized:,.0f} >= Threshold: ₹{Config.PROFIT_LOCK_THRESHOLD:,.0f}")
            print(f"     Floor set: ₹{floor:,.0f} ({Config.PROFIT_LOCK_PERCENTAGE}% of ₹{realized:,.0f})")
            print(f"     {DIM}If realized P&L drops below ₹{floor:,.0f}, system locks out{RESET}")

        s = risk.get_risk_status()
        dd = s["trailing_drawdown"]
        if dd.get("high_water_mark", 0) > 0:
            print(f"     HWM: ₹{dd['high_water_mark']:,.0f}")
        print()

    pause()

    # Continue higher
    subheader("Phase 2: Peak Performance (11:00 - 12:30 PM)")

    peak_steps = [
        (13000, "11:15 — Another winner, running total ₹13,000"),
        (16000, "11:45 — BNF directional trade: ₹3,000 more"),
        (18000, "12:15 — Peak! Total realized: ₹18,000"),
    ]

    for realized, desc in peak_steps:
        print(f"  {GREEN}+{RESET} {desc}")
        risk.evaluate_pnl(realized, 0)
        s = risk.get_risk_status()
        dd = s["trailing_drawdown"]
        print(f"     HWM: ₹{dd['high_water_mark']:,.0f}  |  Drawdown: ₹{dd['current_drawdown']:,.0f}  |  DD Limit: ₹{dd['drawdown_limit']:,.0f}  |  Buffer: ₹{dd['buffer']:,.0f}")
        print()

    pause()

    # Afternoon: Giving back profits
    subheader("Phase 3: Giving Back Profits (1:00 - 2:30 PM)")
    print(f"  The market reverses. Trader starts losing.")
    print(f"  HWM is locked at ₹18,000. Drawdown limit = ₹{18000 * Config.TRAILING_DRAWDOWN_PERCENTAGE / 100:,.0f}\n")

    decline_steps = [
        (15000, "1:15 — Bad CE trade, realized drops to ₹15,000"),
        (13000, "1:45 — Another loss, down to ₹13,000"),
        (11000, "2:15 — Bleeding out, down to ₹11,000"),
        (9000,  "2:30 — Now ₹9,000... approaching the limit"),
    ]

    for realized, desc in decline_steps:
        pnl_color = GREEN if realized > 0 else RED
        print(f"  {RED}-{RESET} {desc}")
        result = risk.evaluate_pnl(realized, 0)
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
            print(f"     Trader keeps ₹{realized:,.0f} — system saved ₹{realized:,.0f} of profits!")
            break
        elif result == "lockout_profit_lock":
            print(f"\n     {RED}{BOLD}PROFIT LOCK FLOOR BREACHED!{RESET}")
            print(f"     P&L ₹{realized:,.0f} fell below floor ₹{state.profit_lock_floor:,.0f}")
            print(f"     System protected minimum ₹{state.profit_lock_floor:,.0f} of profits!")
            break
        print()

    pause()


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO 4: All Systems Working Together
# ═══════════════════════════════════════════════════════════════════

def scenario_combined():
    header("SCENARIO 4: ALL SYSTEMS TOGETHER")
    print(f"  A full trading day showing how all safety layers interact.")
    print(f"  Priority: Lockout > Cooldown > Per-Order Blocks\n")
    pause()

    _fresh_state()
    state = StateManager()
    risk = RiskEngine(state)

    # Phase 1: Slow start with losses
    subheader("Phase 1: Rocky Start (9:15 - 9:45 AM)")
    trade("Trade 1: Loss ₹800")
    risk.evaluate_pnl(-800, 0)
    risk.evaluate_trade_result(-800, {"security_id": "T1"})
    status_line(state, risk)

    trade("Trade 2: Loss ₹600")
    risk.evaluate_pnl(-1400, 0)
    risk.evaluate_trade_result(-600, {"security_id": "T2"})
    status_line(state, risk)

    trade("Trade 3: Loss ₹500")
    risk.evaluate_pnl(-1900, 0)
    risk.evaluate_trade_result(-500, {"security_id": "T3"})
    if state.is_cooling_down:
        print(f"  {YELLOW}COOLDOWN: {Config.CONSECUTIVE_LOSS_COUNT} consecutive losses → {state.cooldown_remaining}s pause{RESET}")
    status_line(state, risk)
    pause()

    # Clear cooldown for simulation
    state._state["is_cooling_down"] = False
    state._state["cooldown_until"] = None
    state._save()

    # Phase 2: Recovery
    subheader("Phase 2: Recovery (10:00 - 11:30 AM)")
    recovery_steps = [
        (-400, "Trade 4: Small win ₹1,500 → net realized -₹400"),
        (2000, "Trade 5: Big winner ₹2,400 → net realized ₹2,000"),
        (5000, "Trade 6: Another winner → net realized ₹5,000"),
        (8000, "Trade 7: On fire → net realized ₹8,000"),
    ]
    for realized, desc in recovery_steps:
        trade(desc)
        risk.evaluate_pnl(realized, 0)
        if realized > 0:
            risk.evaluate_trade_result(abs(realized - recovery_steps[max(0, recovery_steps.index((realized, desc))-1)][0]), {"security_id": "T"})
        s = risk.get_risk_status()
        dd = s["trailing_drawdown"]
        pl = s["profit_lock"]
        if dd.get("high_water_mark", 0) > 0:
            print(f"     HWM: ₹{dd['high_water_mark']:,.0f}  |  Loss buffer: ₹{s['limits']['loss_remaining']:,.0f}")
        if pl.get("active"):
            print(f"     {CYAN}Profit lock floor: ₹{pl['floor']:,.0f}{RESET}")
        print()
    pause()

    # Phase 3: Overtrading in afternoon
    subheader("Phase 3: Afternoon Overtrading (1:00 - 2:00 PM)")
    print(f"  Trader gets overconfident and starts taking big positions.\n")

    trade("Trade 8: Massive position — risk ₹3,000")
    allowed = check_order(risk, qty=100, risk_amt=3000, positions=0)
    if not allowed:
        print(f"  {RED}Order blocked! Single trade risk ₹3,000 > max ₹{Config.MAX_SINGLE_TRADE_RISK:,.0f}{RESET}")

    print()
    trade("Trade 8 (resized): Same trade, smaller — risk ₹1,800")
    allowed = check_order(risk, qty=50, risk_amt=1800, positions=0)
    print()
    pause()

    # Phase 4: Decline
    subheader("Phase 4: Giving Back Gains (2:00 - 3:00 PM)")
    decline = [
        (6000, "Losses bring realized to ₹6,000"),
        (4000, "More losses → ₹4,000"),
    ]
    for realized, desc in decline:
        trade(desc)
        result = risk.evaluate_pnl(realized, 0)
        status_line(state, risk)
        if state.is_locked_out:
            break
    pause()

    # Final status
    subheader("End of Day Summary")
    s = risk.get_risk_status()
    print(f"  Total Trades:       {s['trades']['total']}")
    print(f"  Winners:            {s['trades']['winners']}")
    print(f"  Losers:             {s['trades']['losers']}")
    print(f"  Win Rate:           {s['trades']['win_rate']:.0f}%")
    print(f"  Final Realized:     ₹{s['pnl']['realized']:,.0f}")
    print(f"  Peak P&L:           ₹{s['pnl']['peak']:,.0f}")
    if s["trailing_drawdown"].get("high_water_mark", 0) > 0:
        print(f"  High Water Mark:    ₹{s['trailing_drawdown']['high_water_mark']:,.0f}")
    print(f"  Locked Out:         {'YES' if s['lockout']['active'] else 'No'}")
    if s['lockout']['active']:
        print(f"  Lockout Reason:     {s['lockout']['reason']}")
    pause()


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO 5: Order Interceptor in Action
# ═══════════════════════════════════════════════════════════════════

def scenario_order_interceptor():
    header("SCENARIO 5: ORDER INTERCEPTOR")
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
        (25, 500, 1, "SELL NIFTY 25500 CE (closing position), risk=₹0"),
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
║    Trailing Drawdown:   {Config.TRAILING_DRAWDOWN_PERCENTAGE:>7.0f}%                       ║
║    Cooldown (loss):     {Config.COOLDOWN_AFTER_LOSS:>7}s                       ║
║    Cooldown (streak):   {Config.COOLDOWN_AFTER_CONSECUTIVE_LOSSES:>7}s after {Config.CONSECUTIVE_LOSS_COUNT} losses         ║
║                                                            ║
╠══════════════════════════════════════════════════════════════╣
║  Scenarios:                                                ║
║    1. Daily Loss Limit — bad day, system locks out         ║
║    2. Cooldown System  — consecutive losses trigger pause  ║
║    3. Trailing Drawdown — protecting profits from giveback ║
║    4. All Together     — full day with all systems active  ║
║    5. Order Interceptor — per-order blocking examples      ║
║    A. Run ALL scenarios                                    ║
║    Q. Quit                                                 ║
╚══════════════════════════════════════════════════════════════╝{RESET}
""")

    while True:
        choice = input(f"  {BOLD}Select scenario (1-5, A, Q): {RESET}").strip().upper()

        if choice == "1":
            scenario_daily_loss()
        elif choice == "2":
            scenario_cooldowns()
        elif choice == "3":
            scenario_trailing_drawdown()
        elif choice == "4":
            scenario_combined()
        elif choice == "5":
            scenario_order_interceptor()
        elif choice == "A":
            scenario_daily_loss()
            scenario_cooldowns()
            scenario_trailing_drawdown()
            scenario_combined()
            scenario_order_interceptor()
            header("ALL SCENARIOS COMPLETE")
            print(f"  All 5 safety systems demonstrated successfully.")
            print(f"  Your trading account is protected from:")
            print(f"    - Daily max loss breaches")
            print(f"    - Tilt/revenge trading (cooldowns)")
            print(f"    - Giving back profits (trailing drawdown)")
            print(f"    - Oversized orders (interceptor)")
            print(f"    - Over-leveraging (position limits)")
            print()
            break
        elif choice == "Q":
            break
        else:
            print(f"  Invalid choice. Enter 1-5, A, or Q.")

    # Cleanup
    shutil.rmtree(_sim_dir, ignore_errors=True)
    print(f"\n  {DIM}Simulation state cleaned up.{RESET}\n")


if __name__ == "__main__":
    main()
