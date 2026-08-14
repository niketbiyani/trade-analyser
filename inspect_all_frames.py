import sqlite3
import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def inspect_frames():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1200, "height": 700},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        if session_id and session_sign:
            context.add_cookies([
                {"name": "sessionid", "value": session_id, "domain": ".tradingview.com", "path": "/"},
                {"name": "sessionid_sign", "value": session_sign, "domain": ".tradingview.com", "path": "/"}
            ])
            
        page = context.new_page()
        url = f"https://www.tradingview.com/chart/{layout_id}/"
        print(f"Loading URL: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(8000) # Wait for page to fully load
        
        # Save a screenshot
        temp_img = "static/uploads/tv_inspect_frames.jpg"
        os.makedirs(os.path.dirname(temp_img), exist_ok=True)
        page.screenshot(path=temp_img)
        print(f"Saved screenshot to: {temp_img}")
        
        print(f"\nTotal frames found: {len(page.frames)}")
        
        for idx, frame in enumerate(page.frames):
            print(f"\n--- Frame {idx} (Name: '{frame.name}', URL: '{frame.url[:80]}') ---")
            
            # Search for 'Freedom', 'sale', or 'explore' in this frame
            try:
                matches = frame.locator("text=/Freedom/i, text=/sale/i, text=/explore/i").all()
                print(f"  Matches for 'Freedom/sale/explore': {len(matches)}")
                for m in matches:
                    tag = m.evaluate("el => el.tagName")
                    cls = m.evaluate("el => el.className") or ""
                    text = m.evaluate("el => el.textContent") or ""
                    print(f"    <{tag}> class='{cls}' text='{text[:60]}'")
            except Exception as e:
                print(f"  Error searching text in Frame {idx}: {e}")
                
            # Search for close buttons in this frame
            try:
                close_selectors = [
                    "[class*='close' i]",
                    "[id*='close' i]",
                    "[data-name*='close' i]",
                    "[aria-label*='close' i]",
                    "[class*='dismiss' i]"
                ]
                found_close = []
                for sel in close_selectors:
                    elements = frame.locator(sel).all()
                    for el in elements:
                        if el.is_visible():
                            tag = el.evaluate("el => el.tagName")
                            cls = el.evaluate("el => el.className") or ""
                            aria = el.evaluate("el => el.getAttribute('aria-label')") or ""
                            data_name = el.evaluate("el => el.getAttribute('data-name')") or ""
                            text = el.evaluate("el => el.textContent") or ""
                            desc = f"<{tag}> class='{cls}' aria='{aria}' data-name='{data_name}' text='{text[:20]}'"
                            if desc not in found_close:
                                found_close.append(desc)
                                
                print(f"  Visible close candidates in Frame {idx}: {len(found_close)}")
                for fc in found_close[:10]:
                    print(f"    {fc}")
            except Exception as e:
                print(f"  Error searching close buttons in Frame {idx}: {e}")
                
        browser.close()

if __name__ == "__main__":
    inspect_frames()
