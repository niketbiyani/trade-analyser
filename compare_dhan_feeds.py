import os
import sys
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ
from datetime import date

def compare_tb_and_th():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    today_str = str(date.today())
    
    # 1. Trade Book
    tb_resp = dhan.get_trade_book()
    tb_data = tb_resp.get("data", []) if tb_resp.get("status") == "success" else []
    tb_sorted = sorted(tb_data, key=lambda t: t.get("createTime") or t.get("exchangeTime") or "")
    
    print("--- 1. Dhan Trade Book ---")
    print(f"Total Trade Book records today: {len(tb_sorted)}")
    if tb_sorted:
        print(f"Earliest Trade Book time: {tb_sorted[0].get('createTime') or tb_sorted[0].get('exchangeTime')}")
        print(f"Latest Trade Book time: {tb_sorted[-1].get('createTime') or tb_sorted[-1].get('exchangeTime')}")
        
    # 2. Trade History (Page 0, 1, 2)
    th_data = []
    page = 0
    while True:
        th_resp = dhan.get_trade_history(
            from_date=today_str,
            to_date=today_str,
            page_number=page
        )
        batch = th_resp.get("data", []) if th_resp.get("status") == "success" else []
        if not batch:
            break
        th_data.extend(batch)
        page += 1
        
    th_sorted = sorted(th_data, key=lambda t: t.get("createTime") or t.get("exchangeTime") or "")
    
    print("\n--- 2. Dhan Trade History ---")
    print(f"Total Trade History records today: {len(th_sorted)}")
    if th_sorted:
        print(f"Earliest Trade History time: {th_sorted[0].get('createTime') or th_sorted[0].get('exchangeTime')}")
        print(f"Latest Trade History time: {th_sorted[-1].get('createTime') or th_sorted[-1].get('exchangeTime')}")
        
if __name__ == "__main__":
    compare_tb_and_th()
