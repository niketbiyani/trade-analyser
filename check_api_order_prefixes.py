import os
import sys
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ

def check_prefixes():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    # 1. Fetch trade book
    resp = dhan.get_trade_book()
    data = resp.get("data", []) if resp.get("status") == "success" else []
    
    prefixes = set()
    for t in data:
        oid = t.get("orderId", "")
        if oid:
            prefixes.add(oid[:2])
            
    print("--- Dhan API Order ID Prefixes ---")
    print(f"Total raw execution records: {len(data)}")
    print(f"Distinct 2-digit Order ID prefixes returned by Dhan API: {sorted(list(prefixes))}")
    
if __name__ == "__main__":
    check_prefixes()
