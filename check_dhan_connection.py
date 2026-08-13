import os
import sys
import logging
from datetime import date
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("DhanDiagnose")

def check_connection():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    print("--- Dhan Connection Diagnostic ---")
    print(f"DHAN_CLIENT_ID: {'Set' if client_id else 'Not Set'}")
    print(f"DHAN_ACCESS_TOKEN: {'Set' if access_token else 'Not Set'}")
    
    if not client_id or not access_token:
        print("\n[ERROR] Client ID or Access Token is missing from your .env file!")
        return
        
    try:
        print("\n1. Connecting to Dhan API...")
        ctx = DhanContext(client_id, access_token)
        dhan = DhanHQ(ctx)
        print("✓ Client initialized.")
        
        print("\n2. Fetching Trade Book (Today)...")
        resp_tb = dhan.get_trade_book()
        print(f"Trade Book Response Type: {type(resp_tb)}")
        print("Trade Book Raw Response:")
        print(resp_tb)
        
        print("\n3. Fetching Trade History (Today)...")
        today_str = str(date.today())
        resp_th = dhan.get_trade_history(
            from_date=today_str,
            to_date=today_str,
            page_number=0
        )
        print(f"Trade History Response Type: {type(resp_th)}")
        print("Trade History Raw Response:")
        print(resp_th)
        
    except Exception as e:
        print(f"\n[ERROR] Exception during API call: {e}")

if __name__ == "__main__":
    check_connection()
