import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def check_session():
    print("--- Verifying TradingView Credentials in .env ---")
    print(f"Layout ID: {layout_id}")
    print(f"Session ID: {session_id[:15] if session_id else 'None'}...")
    print(f"Session Sign: {session_sign[:15] if session_sign else 'None'}...")
    
    if not session_id or not session_sign:
        print("ERROR: TradingView session cookies are missing in .env!")
        return
        
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
        
        context.add_cookies([
            {"name": "sessionid", "value": session_id, "domain": ".tradingview.com", "path": "/"},
            {"name": "sessionid_sign", "value": session_sign, "domain": ".tradingview.com", "path": "/"}
        ])
        
        page = context.new_page()
        print("\nLoading TradingView profile page to verify session...")
        page.goto("https://www.tradingview.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        
        # We check if the page contains user menu or username
        html = page.content()
        
        # Look for user menu triggers or username
        logged_in = False
        username = "niketbiyani"
        if username.lower() in html.lower():
            print(f"SUCCESS: Session is VALID! Found username '{username}' on page.")
            logged_in = True
        else:
            # Let's inspect some profile elements
            user_btns = page.locator("button[aria-label*='user menu' i], [class*='user-menu' i], [class*='profile' i]").all()
            for btn in user_btns:
                try:
                    if btn.is_visible():
                        print(f"Found visible user profile button: aria='{btn.getAttribute('aria-label')}' class='{btn.evaluate('el => el.className')}'")
                        logged_in = True
                except Exception:
                    pass
            
            if not logged_in:
                print("FAILURE: Session is EXPIRED or INVALID! TradingView logged you out.")
                print("Your layout will not load and will get stuck on the blue spinner.")
                
        # Take a screenshot of the page to let user see
        test_img = "static/uploads/tv_session_check.jpg"
        page.screenshot(path=test_img)
        print(f"Saved session check screenshot to: {test_img}")
        
        # Now try to load the layout itself
        layout_url = f"https://www.tradingview.com/chart/{layout_id}/"
        print(f"\nAttempting to load layout URL: {layout_url}")
        page.goto(layout_url, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        
        # Check if the spinner is still showing or if charts loaded
        layout_html = page.content()
        if "spinner" in layout_html.lower() or "loading" in layout_html.lower():
            # Check if there are canvas elements
            canvas_count = page.locator(".layout__area--center canvas").count()
            print(f"Layout page elements: Center canvas count = {canvas_count}")
            
        layout_img = "static/uploads/tv_layout_check.jpg"
        page.screenshot(path=layout_img)
        print(f"Saved layout check screenshot to: {layout_img}")
        
        browser.close()

if __name__ == "__main__":
    check_session()
