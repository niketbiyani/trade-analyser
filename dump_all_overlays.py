import os
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

session_id = os.getenv("TRADINGVIEW_SESSIONID")
session_sign = os.getenv("TRADINGVIEW_SESSIONID_SIGN")
layout_id = os.getenv("TRADINGVIEW_LAYOUT_ID")

def dump_overlays():
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
        page.wait_for_timeout(8000) # Wait for overlays to show
        
        # Capture screenshot to verify it is showing
        temp_img = "static/uploads/tv_dump_overlays.jpg"
        page.screenshot(path=temp_img)
        print(f"Saved debug screenshot to: {temp_img}")
        
        # JS script to find all visible elements with position fixed/absolute or high z-index
        js_code = """
        () => {
            const results = [];
            const walk = (el) => {
                if (!el) return;
                const style = window.getComputedStyle(el);
                const isVisible = el.offsetWidth > 0 && el.offsetHeight > 0 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
                
                if (isVisible) {
                    const pos = style.position;
                    const zIndex = parseInt(style.zIndex, 10);
                    
                    if (pos === 'fixed' || pos === 'absolute' || (!isNaN(zIndex) && zIndex > 0)) {
                        // Describe element
                        results.push({
                            tagName: el.tagName,
                            id: el.id || '',
                            className: el.className || '',
                            position: pos,
                            zIndex: zIndex,
                            width: el.offsetWidth,
                            height: el.offsetHeight,
                            textContent: el.textContent ? el.textContent.trim().substring(0, 100) : '',
                            html: el.outerHTML.substring(0, 300)
                        });
                    }
                }
                for (let i = 0; i < el.children.length; i++) {
                    walk(el.children[i]);
                }
            };
            walk(document.body);
            return results;
        }
        """
        
        overlays = page.evaluate(js_code)
        print(f"\n--- Found {len(overlays)} visible positioned/z-indexed elements: ---")
        
        for idx, o in enumerate(overlays):
            # Print only elements that are large (like a modal dialog container) or buttons/icons
            # We want to ignore header toolbars (height 38) or side panels (width 45 or 52)
            w, h = o['width'], o['height']
            # If it's a large container or a button
            if (w > 100 and h > 100) or o['tagName'] in ['BUTTON', 'A', 'SVG'] or 'close' in o['className'].lower() or 'modal' in o['className'].lower():
                print(f"[{idx}] <{o['tagName']}> id='{o['id']}' class='{o['className']}' pos={o['position']} z={o['zIndex']} ({w}x{h}) text='{o['textContent']}'")
                print(f"    Snippet: {o['html'][:150]}...")
                
        browser.close()

if __name__ == "__main__":
    dump_overlays()
