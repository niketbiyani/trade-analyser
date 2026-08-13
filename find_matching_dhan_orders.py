import os
import sys
import sqlite3
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def find_matching_dhan_orders():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    # 1. Fetch raw trade book executions from Dhan
    print("--- Scanning Dhan Trade Book for 77800 CE Executions ---")
    resp = dhan.get_trade_book()
    data = resp.get("data", []) if resp.get("status") == "success" else []
    
    # We look for SENSEX-Aug2026-77800-CE trades today
    raw_77800_ce = []
    for t in data:
        sym = t.get("tradingSymbol", "")
        if "77800" in sym and "CE" in sym:
            raw_77800_ce.append(t)
            
    print(f"Total raw executions for 77800 CE today: {len(raw_77800_ce)}")
    for i, t in enumerate(raw_77800_ce):
        print(f"[{i}] OrderId: {t.get('orderId')}, ExchOrderId: {t.get('exchangeOrderId')}, ExchTradeId: {t.get('exchangeTradeId')}, Side: {t.get('transactionType')}, Qty: {t.get('tradedQuantity')}, Price: {t.get('tradedPrice')}, Time: {t.get('createTime') or t.get('exchangeTime')}")

    # 2. Check which of these OrderIds exist in the SQLite database
    print("\n--- Checking SQLite Database for these OrderIds ---")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    for t in raw_77800_ce:
        oid = t.get('orderId')
        # Check if this orderId is in SQLite (either as dhan_order_id or in the text)
        row = cursor.execute("SELECT id, quantity, pnl, status FROM trades WHERE dhan_order_id=?", (oid,)).fetchone()
        if row:
            print(f"OrderId: {oid} FOUND in SQLite as Trade ID {row[0]} (Qty: {row[1]}, PnL: {row[2]}, Status: {row[3]})")
        else:
            # Maybe check if it's stored in a different format
            print(f"OrderId: {oid} NOT FOUND in SQLite!")
            
    conn.close()

if __name__ == "__main__":
    find_matching_dhan_orders()
