import os
import sys
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ

def count_raw_underlyings():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    if not client_id or not access_token:
        print("Credentials missing!")
        return
        
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    resp = dhan.get_trade_book()
    if resp.get("status") != "success":
        print(f"Failed to fetch trade book: {resp}")
        return
        
    data = resp.get("data", [])
    print(f"Total raw execution records in trade book: {len(data)}")
    
    underlying_counts = {}
    for r in data:
        sym = r.get("tradingSymbol", "")
        # Try to parse the underlying from symbol
        # SENSEX-Aug2026-... or NIFTY-Aug2026-...
        parts = sym.split("-")
        underlying = parts[0] if parts else "UNKNOWN"
        underlying_counts[underlying] = underlying_counts.get(underlying, 0) + 1
        
    print("\nBreakdown of raw execution counts by symbol prefix:")
    for ul, cnt in underlying_counts.items():
        print(f"  {ul}: {cnt} records")
        
if __name__ == "__main__":
    count_raw_underlyings()
