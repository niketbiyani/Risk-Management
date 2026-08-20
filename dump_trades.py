import sqlite3
import os
import sys

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
        
    print(f"Reading trades from database: {db_path}\n")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    for target_date, day_name in [("2026-08-18", "TUESDAY (Aug 18)"), ("2026-08-20", "TODAY (Aug 20)")]:
        print("=" * 60)
        print(f" {day_name}")
        print("=" * 60)
        
        # Try trade-analyser table format first, fallback to risk journal format
        try:
            rows = conn.execute(
                "SELECT underlying, option_type, strike, entry_time, entry_price, exit_time, exit_price, quantity, pnl, status "
                "FROM trades WHERE date=? ORDER BY entry_time", (target_date,)
            ).fetchall()
            
            if not rows:
                print(" No trades found for this day.\n")
                continue
                
            for idx, r in enumerate(rows, 1):
                pnl_str = f"₹{r['pnl']:,.2f}" if r['pnl'] is not None else "OPEN"
                print(f"{idx}. [{r['entry_time']}] {r['underlying']} {r['strike']} {r['option_type']} "
                      f"| Qty: {r['quantity']} | Entry: ₹{r['entry_price']} -> Exit: ₹{r['exit_price']} "
                      f"| P&L: {pnl_str} ({r['status']})")
        except sqlite3.OperationalError:
            # Fallback to trade_journal schema
            try:
                rows = conn.execute(
                    "SELECT instrument, type, qty, entry_price, exit_price, pnl FROM trades WHERE date=? ORDER BY id",
                    (target_date,)
                ).fetchall()
                if not rows:
                    print(" No trades found for this day.\n")
                    continue
                for idx, r in enumerate(rows, 1):
                    pnl_str = f"₹{r['pnl']:,.2f}" if r['pnl'] is not None else "OPEN"
                    print(f"{idx}. {r['instrument']} ({r['type']}) | Qty: {r['qty']} "
                          f"| Entry: ₹{r['entry_price']} -> Exit: ₹{r['exit_price']} | P&L: {pnl_str}")
            except Exception as e:
                print(f" Failed to read trades table: {e}")
        print()

if __name__ == "__main__":
    main()
