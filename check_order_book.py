import os
import sys
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ

def check_order_book():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    resp = dhan.get_order_list()
    if resp.get("status") != "success":
        print(f"Failed to fetch order book: {resp}")
        return
        
    data = resp.get("data", [])
    print(f"Total orders in Dhan order book today: {len(data)}")
    
    if not data:
        return
        
    def order_time(o):
        return o.get("createTime") or o.get("orderTime") or ""
        
    sorted_orders = sorted(data, key=order_time)
    print(f"Earliest order time: {order_time(sorted_orders[0])}")
    print(f"Latest order time: {order_time(sorted_orders[-1])}")
    
    # Check if there are orders before 10:55:06 AM IST
    before_1055 = [o for o in sorted_orders if order_time(o) < "2026-08-13 10:55:06"]
    print(f"Number of orders placed before 10:55:06 AM IST today: {len(before_1055)}")
    
    if before_1055:
        print("\nFirst 5 orders placed before 10:55 AM IST:")
        for o in before_1055[:5]:
            print(f"  Time: {order_time(o)}, ID: {o.get('orderId')}, Symbol: {o.get('tradingSymbol')}, Side: {o.get('transactionType')}, Status: {o.get('orderStatus')}, Qty: {o.get('quantity')}")
            
if __name__ == "__main__":
    check_order_book()
