import sqlite3
import os

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def find_order_id():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    order_ids = ['324260813513764', '249260813725025', '324260813513664', '339260813570325']
    print("--- Searching Database for Specific Order IDs ---")
    for oid in order_ids:
        rows = cursor.execute("SELECT * FROM trades WHERE dhan_order_id LIKE ?", (f"%{oid}%",)).fetchall()
        print(f"\nSearching for '{oid}': Found {len(rows)} rows.")
        for r in rows:
            print(f"  ID: {r['id']}, Date: {r['date']}, Strike: {r['strike']}, Qty: {r['quantity']}, PnL: {r['pnl']}, Status: {r['status']}, dhan_order_id: {r['dhan_order_id']}")
            
    conn.close()

if __name__ == "__main__":
    find_order_id()
