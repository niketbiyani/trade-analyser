import os
import sys
import sqlite3
from datetime import datetime, timedelta

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def calculate_last_24h_pnl():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 24h cutoff
    cutoff_ts = (datetime.now() - timedelta(hours=24)).timestamp()
    
    # Sum PnL of all trades created/updated in the last 24 hours
    row = cursor.execute("SELECT SUM(pnl), COUNT(*) FROM trades WHERE created_at >= ? AND status='CLOSED'", (cutoff_ts,)).fetchone()
    print("--- 24 Hour SQLite Insertions/Updates PnL ---")
    print(f"Total Closed Trades: {row[1]}")
    print(f"Total P&L: {row[0]:.2f}")
    
    # Sum PnL of ALL trades with date='2026-08-13'
    row_today = cursor.execute("SELECT SUM(pnl), COUNT(*) FROM trades WHERE date='2026-08-13' AND status='CLOSED'").fetchone()
    print("\n--- Trades strictly dated 2026-08-13 ---")
    print(f"Total Closed Trades: {row_today[1]}")
    print(f"Total P&L: {row_today[0]:.2f}")
    
    # Let's see if there are any CLOSED SENSEX trades today that are not included or if there is a difference
    # Let's list the top 10 highest PnL trades today
    print("\nTop 10 highest P&L trades today:")
    highs = cursor.execute("SELECT id, underlying, strike, option_type, entry_time, exit_time, quantity, pnl FROM trades WHERE date='2026-08-13' ORDER BY pnl DESC LIMIT 10").fetchall()
    for h in highs:
        print(f"  ID: {h['id']}, {h['underlying']} {h['strike']} {h['option_type']}, PnL: {h['pnl']:.2f}, Qty: {h['quantity']}")
        
    conn.close()

if __name__ == "__main__":
    calculate_last_24h_pnl()
