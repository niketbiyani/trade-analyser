import os
import sys
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ
from datetime import date

# Add project path to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def inspect_raw_dhan():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    print("Fetching today's trade book from Dhan...")
    resp = dhan.get_trade_book()
    raw = resp.get("data", []) if resp.get("status") == "success" else []
    print(f"Retrieved {len(raw)} raw executions from Dhan.")
    
    # Filter Nifty option executions
    nifty_trades = []
    for t in raw:
        sym = (t.get("tradingSymbol") or "").upper()
        if "NIFTY" in sym:
            nifty_trades.append(t)
            
    print(f"\nFound {len(nifty_trades)} NIFTY-related raw executions:")
    for idx, t in enumerate(nifty_trades[:30]):
        print(f"[{idx}] OrderId: {t.get('orderId')}, ExchOrderId: {t.get('exchangeOrderId')}, Symbol: {t.get('tradingSymbol')}, Side: {t.get('transactionType')}, Qty: {t.get('tradedQuantity') or t.get('quantity')}, Price: {t.get('tradedPrice') or t.get('price')}, Time: {t.get('createTime') or t.get('exchangeTime')}")

if __name__ == "__main__":
    inspect_raw_dhan()
