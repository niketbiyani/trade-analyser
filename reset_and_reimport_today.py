import os
import sys
import sqlite3
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ
from datetime import date

# Add project path to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import _process_raw_trades, _is_option

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def reset_and_reimport():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    # 1. Fetch raw executions from Dhan API
    print("Fetching today's trade book from Dhan...")
    resp = dhan.get_trade_book()
    raw = resp.get("data", []) if resp.get("status") == "success" else []
    print(f"Retrieved {len(raw)} raw executions from Dhan.")
    
    # Filter for options
    opts = [t for t in raw if _is_option(t)]
    print(f"Filtered to {len(opts)} option executions.")
    
    # 2. Delete today's trades from SQLite
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Let's count them first
    before_count = cursor.execute("SELECT COUNT(*) FROM trades WHERE date='2026-08-13'").fetchone()[0]
    print(f"Current trades strictly dated 2026-08-13 in DB: {before_count}")
    
    print("Deleting today's trades to perform a clean re-import...")
    cursor.execute("DELETE FROM trades WHERE date='2026-08-13'")
    conn.commit()
    print("Deleted successfully.")
    
    conn.close()
    
    # 3. Process and insert today's trades cleanly using the fixed app logic
    print("Processing and inserting trades into SQLite...")
    imported, skipped = _process_raw_trades(opts)
    print(f"Clean import complete: Imported: {imported}, Skipped: {skipped}")
    
    # Verify final count
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    after_count = cursor.execute("SELECT COUNT(*) FROM trades WHERE date='2026-08-13'").fetchone()[0]
    print(f"Final trades strictly dated 2026-08-13 in DB: {after_count}")
    conn.close()

if __name__ == "__main__":
    reset_and_reimport()
