import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def inspect_promo_modal():
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
        # Open TradingView layout
        url = f"https://www.tradingview.com/chart/{layout_id}/"
        print(f"Loading URL: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(8000) # Wait for the popup to show up
        
        # Take a screenshot to verify what it currently sees
        temp_img = "static/uploads/tv_inspect_promo.jpg"
        os.makedirs(os.path.dirname(temp_img), exist_ok=True)
        page.screenshot(path=temp_img)
        print(f"Saved initial screenshot to {temp_img}")
        
        print("\n--- Inspecting Overlays and Modals ---")
        
        # 1. Let's find any element that has z-index or contains close/promo
        # We look for overlays or elements containing 'close' in class or aria-label
        close_candidates = page.locator("[class*='close' i], [id*='close' i], [data-name='close'], [aria-label*='close' i], button, div").all()
        
        print(f"Found {len(close_candidates)} total candidates. Listing visible ones:")
        count = 0
        for el in close_candidates:
            try:
                if el.is_visible():
                    tag = el.evaluate("el => el.tagName")
                    cls = el.evaluate("el => el.className") or ""
                    val_id = el.evaluate("el => el.id") or ""
                    aria = el.evaluate("el => el.getAttribute('aria-label')") or ""
                    data_name = el.evaluate("el => el.getAttribute('data-name')") or ""
                    text = el.evaluate("el => el.textContent") or ""
                    
                    # We are interested in buttons or divs that contain close or look like close button
                    if any(x in (cls + val_id + aria + data_name).lower() for x in ['close', 'dismiss', 'cancel', 'cross', 'x']):
                        print(f"Candidate {count}: <{tag}> id='{val_id}' class='{cls}' aria='{aria}' data-name='{data_name}' text='{text[:20]}'")
                        count += 1
                        if count >= 30:
                            break
            except Exception:
                pass
                
        # 2. Check overlap-manager-root
        overlap_manager = page.locator("#overlap-manager-root").all()
        print(f"\nOverlap manager root elements count: {len(overlap_manager)}")
        if overlap_manager:
            inner_html = page.locator("#overlap-manager-root").inner_html()
            print("Overlap manager root inner HTML summary:")
            print(inner_html[:1000] + ("..." if len(inner_html) > 1000 else ""))
            
        browser.close()

if __name__ == "__main__":
    inspect_promo_modal()
