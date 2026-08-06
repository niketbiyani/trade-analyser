import sqlite3
import os

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    # Fallback to local scratch
    db_path = "analyser.db"

def inspect_db():
    if not os.path.exists(db_path):
        print("Database not found!")
        return
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("--- Yesterday's (2026-08-05) Trades & Notes ---")
    rows = cursor.execute(
        "SELECT t.id, t.date, t.underlying, t.option_type, t.strike, t.expiry, t.entry_time, n.image_path "
        "FROM trades t "
        "LEFT JOIN trade_notes n ON t.date=n.date AND t.underlying=n.underlying "
        "AND t.option_type=n.option_type AND t.strike=n.strike AND t.entry_time=n.entry_time "
        "WHERE t.date='2026-08-05' AND t.underlying='SENSEX'"
    ).fetchall()
    
    for r in rows:
        print(f"ID: {r['id']}, Date: {r['date']}, Expiry: {r['expiry']}, Strike: {r['strike']}, Option: {r['option_type']}, Images: {r['image_path']}")
        
    print("\n--- Today's (2026-08-06) Trades & Notes ---")
    rows_today = cursor.execute(
        "SELECT t.id, t.date, t.underlying, t.option_type, t.strike, t.expiry, t.entry_time, n.image_path "
        "FROM trades t "
        "LEFT JOIN trade_notes n ON t.date=n.date AND t.underlying=n.underlying "
        "AND t.option_type=n.option_type AND t.strike=n.strike AND t.entry_time=n.entry_time "
        "WHERE t.date='2026-08-06' AND t.underlying='SENSEX'"
    ).fetchall()
    
    for r in rows_today:
        print(f"ID: {r['id']}, Date: {r['date']}, Expiry: {r['expiry']}, Strike: {r['strike']}, Option: {r['option_type']}, Images: {r['image_path']}")
        
    conn.close()

if __name__ == "__main__":
    inspect_db()
