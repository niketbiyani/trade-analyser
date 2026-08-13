import os
import sys
import sqlite3

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def find_carry_forward_and_open():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("--- 1. Checking for Carry-Forward Trades Closed Today ---")
    # A carry-forward trade closed today would have exit_time containing today's date (or exit_date if there's one)
    # Let's check all trades in the database where exit_time is today, but entry date is not today.
    # Note: In SQLite, trades have a 'date' column (entry date) and exit_time is HH:MM:SS.
    # Wait, how is exit_time formatted? Is it just HH:MM:SS?
    # Let's check the schema of the trades table first.
    schema = cursor.execute("PRAGMA table_info(trades)").fetchall()
    print("Trades Schema:")
    for col in schema:
        print(f"  {col['name']}: {col['type']}")
        
    # Let's check if there are any trades from today (date='2026-08-13') that are OPEN
    open_today = cursor.execute("SELECT * FROM trades WHERE date='2026-08-13' AND status='OPEN'").fetchall()
    print(f"\nOPEN trades from today: {len(open_today)}")
    for t in open_today:
        print(f"  ID: {t['id']}, {t['underlying']} {t['strike']} {t['option_type']}, Entry: {t['entry_time']}, Qty: {t['quantity']}")
        
    # Let's check if there are any trades from today where pnl is NULL or None
    null_pnl = cursor.execute("SELECT * FROM trades WHERE date='2026-08-13' AND pnl IS NULL").fetchall()
    print(f"\nTrades with NULL P&L today: {len(null_pnl)}")
    for t in null_pnl:
        print(f"  ID: {t['id']}, {t['underlying']} {t['strike']} {t['option_type']}, Entry: {t['entry_time']}, Qty: {t['quantity']}, Status: {t['status']}")
        
    conn.close()

if __name__ == "__main__":
    find_carry_forward_and_open()
