import os
import sys
import logging
from datetime import date
from dotenv import load_dotenv

# Add project path to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import import_from_dhan, get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ImportDiagnose")

def test_import():
    print("--- Running Test Import for Today (2026-08-13) ---")
    
    # Check current trades in db before import
    db = get_db()
    before_count = db.execute("SELECT COUNT(*) FROM trades WHERE date='2026-08-13'").fetchone()[0]
    print(f"SENSEX trades in database before import: {before_count}")
    
    try:
        res = import_from_dhan("2026-08-13", "2026-08-13")
        print("\n--- Import Result ---")
        print(f"Status: {res.get('status')}")
        print(f"Remarks: {res.get('remarks')}")
        print(f"Processed Count: {res.get('imported', 0)}")
        
        # Check current trades in db after import
        after_count = db.execute("SELECT COUNT(*) FROM trades WHERE date='2026-08-13'").fetchone()[0]
        print(f"SENSEX trades in database after import: {after_count}")
        
        # Print a sample of today's trades from DB
        rows = db.execute(
            "SELECT id, underlying, option_type, strike, entry_time, exit_time, status, quantity, pnl "
            "FROM trades WHERE date='2026-08-13'"
        ).fetchall()
        print(f"\nToday's trades in DB (Total {len(rows)}):")
        for r in rows:
            print(f"ID: {r['id']}, {r['underlying']} {r['strike']} {r['option_type']}, Entry: {r['entry_time']}, Exit: {r['exit_time']}, Qty: {r['quantity']}, PnL: {r['pnl']}, Status: {r['status']}")
            
    except Exception as e:
        print(f"\n[ERROR] Import failed with exception: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_import()
