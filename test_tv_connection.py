import os
import sys
from dotenv import load_dotenv

# Ensure we can import capture_tv
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

print("--- TradingView Connection Test ---")
print(f"TRADINGVIEW_SESSIONID: {'Set (starts with ' + session_id[:8] + ')' if session_id else 'Not Set'}")
print(f"TRADINGVIEW_SESSIONID_SIGN: {'Set (starts with ' + session_sign[:8] + ')' if session_sign else 'Not Set'}")
print(f"TRADINGVIEW_LAYOUT_ID: {layout_id or 'Not Set'}")

output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads", "tv_test.jpg")
os.makedirs(os.path.dirname(output_path), exist_ok=True)
print(f"Output path: {output_path}")

from capture_tv import capture_screenshot
print("Launching browser and loading chart. Please wait...")

success = capture_screenshot("NSE:NIFTY", "15s", output_path, session_id, session_sign, layout_id)

if success:
    print("\n✓ SUCCESS!")
    print(f"A test screenshot of NSE:NIFTY (15s) has been successfully saved to: {output_path}")
    print("You can verify it by visiting: http://<your_vps_ip>/static/uploads/tv_test.jpg in your browser (replace <your_vps_ip> with your actual VPS IP address).")
else:
    print("\n✗ FAILED!")
    print("Failed to capture TradingView screenshot. Please verify your cookie values in .env and check that Playwright is installed.")
