import os
import sys
import sqlite3
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def diagnose_today_trades():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    # 1. Fetch raw execution trades from Dhan, sort by time
    print("--- Dhan Raw Trade Book (First 10 chronologically) ---")
    resp = dhan.get_trade_book()
    data = resp.get("data", []) if resp.get("status") == "success" else []
    
    def trade_key(t):
        return t.get("createTime") or t.get("exchangeTime") or ""
        
    sorted_raw = sorted(data, key=trade_key)
    print(f"Total raw execution records today: {len(sorted_raw)}")
    for i, t in enumerate(sorted_raw[:10]):
        print(f"[{i}] Time: {trade_key(t)}, Symbol: {t.get('tradingSymbol')}, Side: {t.get('transactionType')}, Qty: {t.get('tradedQuantity')}, Price: {t.get('tradedPrice')}")
        
    print("\n--- Dhan Raw Trade Book (Last 10 chronologically) ---")
    for i, t in enumerate(sorted_raw[-10:]):
        print(f"[{len(sorted_raw)-10+i}] Time: {trade_key(t)}, Symbol: {t.get('tradingSymbol')}, Side: {t.get('transactionType')}, Qty: {t.get('tradedQuantity')}, Price: {t.get('tradedPrice')}")

    # 2. Query all today's trades from SQLite, sort by entry_time
    print("\n--- SQLite Today's Trades (First 10) ---")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    rows = cursor.execute(
        "SELECT id, underlying, option_type, strike, entry_time, exit_time, status, quantity, pnl "
        "FROM trades WHERE date='2026-08-13' ORDER BY entry_time ASC"
    ).fetchall()
    
    print(f"Total trades in SQLite today: {len(rows)}")
    for r in rows[:10]:
        print(f"ID: {r['id']}, Symbol: {r['underlying']} {r['strike']} {r['option_type']}, Entry: {r['entry_time']}, Exit: {r['exit_time']}, Qty: {r['quantity']}, PnL: {r['pnl']}")
        
    print("\n--- SQLite Today's Trades (Last 10) ---")
    for r in rows[-10:]:
        print(f"ID: {r['id']}, Symbol: {r['underlying']} {r['strike']} {r['option_type']}, Entry: {r['entry_time']}, Exit: {r['exit_time']}, Qty: {r['quantity']}, PnL: {r['pnl']}")
        
    conn.close()

if __name__ == "__main__":
    diagnose_today_trades()
