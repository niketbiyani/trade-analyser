import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def inspect_floating():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        if session_id and session_sign:
            context.add_cookies([
                {"name": "sessionid", "value": session_id, "domain": ".tradingview.com", "path": "/"},
                {"name": "sessionid_sign", "value": session_sign, "domain": ".tradingview.com", "path": "/"}
            ])
            
        page = context.new_page()
        url = f"https://www.tradingview.com/chart/{layout_id}/"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        
        print("\n--- Inspecting Floating/Body Level Elements ---")
        
        # Query all immediate children of body that are visible and NOT layout area center
        children = page.locator("body > *").all()
        for idx, child in enumerate(children):
            tag = child.evaluate("el => el.tagName")
            cls = child.evaluate("el => el.className")
            val_id = child.evaluate("el => el.id")
            
            # Print if it looks like a toolbar or dialog or has a class
            if cls and any(kw in cls.lower() for kw in ["toolbar", "widget", "menu", "dialog", "popup", "favorite", "drawing", "floating"]):
                print(f"Body child {idx}: <{tag}> class='{cls}' id='{val_id}'")
            elif val_id and any(kw in val_id.lower() for kw in ["toolbar", "widget", "menu", "dialog", "popup", "favorite", "drawing"]):
                print(f"Body child {idx}: <{tag}> class='{cls}' id='{val_id}'")
                
        # Find all divs containing drawings toolbar or similar
        all_divs = page.locator("div").all()
        print(f"\nTotal divs on page: {len(all_divs)}")
        for div in all_divs:
            try:
                cls = div.evaluate("el => el.className")
                # Check for favorite drawings indicators
                if cls and any(kw in cls.lower() for kw in ["favorite", "drawing", "quick-tool", "quicktool"]):
                    print(f"Matching Div: class='{cls}'")
            except Exception:
                pass
                
        browser.close()

if __name__ == "__main__":
    inspect_floating()
