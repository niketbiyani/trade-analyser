import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def inspect_spinner():
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
        page.goto(url, wait_until="commit")
        
        print("\n--- Monitoring DOM for loading/spinner elements ---")
        
        # Monitor every 500ms for 12 seconds
        for step in range(24):
            page.wait_for_timeout(500)
            # Find any visible elements containing "loading", "spinner", "loader", "progress" in class or id
            js_find_spinners = """
            () => {
                const els = document.querySelectorAll('*');
                const results = [];
                els.forEach(el => {
                    const style = window.getComputedStyle(el);
                    const isVisible = el.offsetWidth > 0 && el.offsetHeight > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    if (isVisible) {
                        const id = el.id || '';
                        const cls = el.className || '';
                        const text = el.textContent || '';
                        const matches = [id, cls].some(s => /loading|spinner|loader|progress|overlay/i.test(s));
                        if (matches) {
                            results.push({
                                tagName: el.tagName,
                                id: id,
                                className: cls,
                                width: el.offsetWidth,
                                height: el.offsetHeight,
                                text: text.substring(0, 30)
                            });
                        }
                    }
                });
                return results;
            }
            """
            try:
                spinners = page.evaluate(js_find_spinners)
                if spinners:
                    print(f"[{step*0.5:.1f}s] Found {len(spinners)} visible spinner-like elements:")
                    for s in spinners[:10]:
                        print(f"  <{s['tagName']}> id='{s['id']}' class='{s['className']}' ({s['width']}x{s['height']})")
                else:
                    # Check if layout area center has loaded
                    center = page.locator(".layout__area--center").all()
                    print(f"[{step*0.5:.1f}s] No spinner elements found. Center layout visible: {len(center) > 0}")
            except Exception as e:
                print(f"Error evaluating page state: {e}")
                
        browser.close()

if __name__ == "__main__":
    inspect_spinner()
