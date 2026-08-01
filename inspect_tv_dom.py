import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def inspect():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Set authentication cookies
        if session_id and session_sign:
            context.add_cookies([
                {"name": "sessionid", "value": session_id, "domain": ".tradingview.com", "path": "/"},
                {"name": "sessionid_sign", "value": session_sign, "domain": ".tradingview.com", "path": "/"}
            ])
            
        page = context.new_page()
        url = f"https://www.tradingview.com/chart/{layout_id}/"
        print(f"Loading URL: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(8000) # wait for layout to load completely
        
        print("\n--- DOM Inspection ---")
        
        # Check all child elements of .layout__area--center
        center_area = page.locator(".layout__area--center")
        if center_area.count() > 0:
            print("Found .layout__area--center!")
            # Get class names of immediate children
            children = page.locator(".layout__area--center >> xpath=child::*").all()
            print(f"Number of immediate children: {len(children)}")
            for idx, child in enumerate(children):
                tag = child.evaluate("el => el.tagName")
                cls = child.evaluate("el => el.className")
                val_id = child.evaluate("el => el.id")
                print(f"Child {idx}: <{tag}> class='{cls}' id='{val_id}'")
                
                # Check nested children
                nested = child.locator("xpath=descendant::*").all()
                print(f"  Descendants count: {len(nested)}")
                # Find elements with class containing 'widget' or 'chart'
                for n_child in nested:
                    n_tag = n_child.evaluate("el => el.tagName")
                    n_cls = n_child.evaluate("el => el.className")
                    if n_cls and ("chart" in n_cls.lower() or "widget" in n_cls.lower() or "container" in n_cls.lower()):
                        print(f"  - <{n_tag}> class='{n_cls}'")
        else:
            print("Could NOT find .layout__area--center!")
            
        browser.close()

if __name__ == "__main__":
    inspect()
