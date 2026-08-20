import sqlite3
import os
import sys
from collections import defaultdict

def main():
    paths = [
        "/root/trade-analyser/analyser.db",
        "/root/Risk-Management/trade_journal.db",
        "./analyser.db",
        "./trade_journal.db"
    ]
    db_path = None
    for p in paths:
        if os.path.exists(p):
            db_path = p
            break
            
    if not db_path:
        print("Error: Could not locate analyser.db or trade_journal.db on the system.")
        sys.exit(1)
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    SIMULATED_QTY = 240
    
    for target_date, day_name in [("2026-08-18", "TUESDAY (Aug 18)"), ("2026-08-20", "TODAY (Aug 20)")]:
        print("=" * 70)
        print(f" SIMULATING {day_name} WITH FIXED SIZE: {SIMULATED_QTY} QTY")
        print("=" * 70)
        
        try:
            # Query all trades for the day
            rows = conn.execute(
                "SELECT underlying, option_type, strike, entry_time, entry_price, exit_time, exit_price, quantity, pnl, status, direction "
                "FROM trades WHERE date=? ORDER BY entry_time", (target_date,)
            ).fetchall()
            
            if not rows:
                print(" No trades found for this day.\n")
                continue
            
            # Group trades by (entry_time, strike, option_type, direction) to reconstruct logical orders
            logical_trades = defaultdict(list)
            for r in rows:
                # Group by entry time (HH:MM:SS), strike, option_type, and direction
                # This groups split orders (like 9x 1000 qty at 15:27:33) into 1 logical trade decision
                key = (r['entry_time'], r['underlying'], r['strike'], r['option_type'], r['direction'])
                logical_trades[key].append(r)
                
            total_actual_pnl = 0.0
            total_simulated_pnl = 0.0
            trade_index = 1
            
            print(f"{'Time':<10} | {'Instrument':<18} | {'Dir':<5} | {'Actual Qty':<10} | {'Avg Entry':<9} -> {'Avg Exit':<8} | {'Actual P&L':<12} | {'Sim P&L':<12}")
            print("-" * 105)
            
            for key, trades in sorted(logical_trades.items(), key=lambda x: x[0][0]):
                entry_time, underlying, strike, option_type, direction = key
                
                total_qty = sum(t['quantity'] for t in trades)
                actual_pnl = sum(t['pnl'] if t['pnl'] is not None else 0.0 for t in trades)
                
                # Weighted average entry and exit prices
                avg_entry = sum(t['entry_price'] * t['quantity'] for t in trades) / total_qty
                avg_exit = sum((t['exit_price'] or 0.0) * t['quantity'] for t in trades) / total_qty
                
                # Simulate P&L with fixed SIMULATED_QTY
                if direction == 'LONG':
                    simulated_pnl = (avg_exit - avg_entry) * SIMULATED_QTY
                else: # SHORT
                    simulated_pnl = (avg_entry - avg_exit) * SIMULATED_QTY
                
                total_actual_pnl += actual_pnl
                total_simulated_pnl += simulated_pnl
                
                inst_str = f"{underlying} {strike} {option_type}"
                dir_str = "BUY" if direction == 'LONG' else "SELL"
                
                print(f"{entry_time:<10} | {inst_str:<18} | {dir_str:<5} | {total_qty:<10} | {avg_entry:<9.2f} -> {avg_exit:<8.2f} | ₹{actual_pnl:<11,.2f} | ₹{simulated_pnl:<11,.2f}")
                trade_index += 1
                
            print("-" * 105)
            print(f"TOTAL ACTUAL P&L:    ₹{total_actual_pnl:,.2f}")
            print(f"TOTAL SIMULATED P&L: ₹{total_simulated_pnl:,.2f}")
            print(f"DIFFERENCE SAVED:    ₹{total_actual_pnl - total_simulated_pnl:,.2f}")
            
        except sqlite3.OperationalError as e:
            print(f" Database operational error: {e}")
        print("\n")

if __name__ == "__main__":
    main()
