import sqlite3
import os

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def inspect_nifty():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Query Nifty option trades from the last few days
    rows = cursor.execute(
        "SELECT id, date, underlying, strike, option_type, quantity, entry_time, exit_time, status, pnl, dhan_order_id "
        "FROM trades WHERE underlying='NIFTY' ORDER BY date DESC, entry_time DESC LIMIT 50"
    ).fetchall()
    
    print(f"--- Last 50 Nifty Option Trades in SQLite ---")
    print(f"Total Nifty trades found: {len(rows)}")
    for r in rows:
        print(f"ID: {r['id']}, Date: {r['date']}, {r['underlying']} {r['strike']} {r['option_type']}, Qty: {r['quantity']} (Lots: {r['quantity']/25 if r['quantity'] else 0}), Entry: {r['entry_time']}, Exit: {r['exit_time']}, PnL: {r['pnl']}, Status: {r['status']}, OrderId: {r['dhan_order_id']}")
        
    conn.close()

if __name__ == "__main__":
    inspect_nifty()
