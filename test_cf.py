import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def test_cf():
    print("--- Verifying Cloudflare/Page Load Status ---")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-component-update",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-default-apps",
                "--js-flags=--max-old-space-size=128",
                "--single-process"
            ]
        )
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
        try:
            print("Navigating to https://www.tradingview.com/ ...")
            page.goto("https://www.tradingview.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            
            title = page.title()
            print(f"Page Title: '{title}'")
            
            html = page.content()
            print(f"HTML Length: {len(html)}")
            
            if "cloudflare" in html.lower() or "challenge" in html.lower() or "just a moment" in title.lower():
                print("ALERT: The automated browser is being BLOCKED by Cloudflare!")
            else:
                print("No Cloudflare block detected on main page.")
                
            # Check if username is present
            if "niketbiyani" in html.lower():
                print("Username found in HTML!")
            else:
                print("Username NOT found in HTML.")
                
        except Exception as e:
            print(f"Error loading main page: {e}")
            
        try:
            layout_url = f"https://www.tradingview.com/chart/{layout_id}/"
            print(f"\nNavigating to layout URL: {layout_url} ...")
            page.goto(layout_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            
            title = page.title()
            print(f"Layout Title: '{title}'")
            
            # Check if loading spinner is present in DOM
            spinner_visible = page.locator("[class*='loading' i], [class*='spinner' i]").count()
            print(f"Spinner elements count: {spinner_visible}")
            
        except Exception as e:
            print(f"Error loading layout page: {e}")
            
        browser.close()

if __name__ == "__main__":
    test_cf()
