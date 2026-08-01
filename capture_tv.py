import logging
import os
import time

logger = logging.getLogger("trade-analyser.capture_tv")

def capture_screenshot(symbol: str, interval: str, output_path: str, session_id: str = None, session_id_sign: str = None, layout_id: str = None, trade_date: str = None, entry_time: str = None) -> bool:
    """Launch headless Playwright browser to load and capture a clean TradingView chart."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright is not installed. Please run: pip install playwright && playwright install chromium")
        return False

    logger.info("Starting TV capture for %s (%s) -> %s", symbol, interval, output_path)
    
    # Normalize interval format for TradingView URL:
    # 15s -> 15S, 5s -> 5S, 1m -> 1, 3m -> 3, 5m -> 5
    interval_upper = interval.upper()
    if interval_upper.endswith("S"):
        tv_interval = interval_upper # e.g. 15S
    elif interval_upper.endswith("M"):
        tv_interval = interval_upper[:-1] # e.g. 1, 3, 5
    else:
        tv_interval = "15S" # fallback default

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            # Create a context with desktop HD resolution
            context = browser.new_context(viewport={"width": 1200, "height": 700})
            
            # Inject TradingView login session cookies if provided
            if session_id:
                logger.info("Injecting TradingView sessionid cookie")
                context.add_cookies([{
                    "name": "sessionid",
                    "value": session_id,
                    "domain": ".tradingview.com",
                    "path": "/"
                }])
            if session_id_sign:
                logger.info("Injecting TradingView sessionid_sign cookie")
                context.add_cookies([{
                    "name": "sessionid_sign",
                    "value": session_id_sign,
                    "domain": ".tradingview.com",
                    "path": "/"
                }])
            
            page = context.new_page()
            
            # Open TradingView chart URL directly
            if layout_id:
                url = f"https://www.tradingview.com/chart/{layout_id}/?symbol={symbol}"
            else:
                url = f"https://www.tradingview.com/chart/?symbol={symbol}&interval={tv_interval}"
            logger.info("Navigating to URL: %s", url)
            page.goto(url, wait_until="domcontentloaded")
            
            # Wait for base layout loading (4s is safe)
            page.wait_for_timeout(4000)
            
            # CSS snippet to hide UI Chrome headers, sidebars, panels, and cookie consent overlays
            clean_css = """
            .layout__area--left, 
            .layout__area--right, 
            #header-toolbar-chart, 
            .left-panel, 
            .widget-bar, 
            .chart-controls-bar, 
            .tv-side-panel, 
            .bottom-widgetbar-content,
            #onetrust-consent-sdk,
            .ot-sdk-container,
            [class*="cookie" i],
            [class*="consent" i],
            [id*="cookie" i],
            [id*="consent" i] { 
                display: none !important; 
            }
            body, html, .layout__area--center {
                width: 100% !important;
                height: 100% !important;
                margin: 0 !important;
                padding: 0 !important;
            }
            """
            page.add_style_tag(content=clean_css)
            page.wait_for_timeout(1000)
            
            # Dismiss cookie consent dialog if it appears
            try:
                page.locator("button:has-text('Accept all')").click(timeout=1500)
                logger.info("Dismissed cookie consent banner")
            except Exception:
                pass
            
            # Target active chart containers
            widgets = page.locator(".chart-widget, [class*='chart-widget'], .chart-container").all()
            
            # Set the symbols on the layout
            if len(widgets) > 1:
                logger.info("Multi-chart layout detected (%d widgets). setting symbols individually.", len(widgets))
                try:
                    # Focus first widget and change to Option symbol
                    widgets[0].click()
                    page.wait_for_timeout(300)
                    page.locator("#header-toolbar-symbol-search").click()
                    page.wait_for_timeout(800) # wait for search modal
                    
                    # Force "All" tab to prevent Options Chain dialog from opening
                    try:
                        page.locator("[role='tab']:has-text('All'), button:has-text('All')").first().click(timeout=1000)
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
                        
                    page.keyboard.type(symbol)
                    page.wait_for_timeout(500)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(1500)
                    
                    # Focus second widget and change to the SAME Option symbol
                    widgets[1].click()
                    page.wait_for_timeout(300)
                    page.locator("#header-toolbar-symbol-search").click()
                    page.wait_for_timeout(800)
                    
                    try:
                        page.locator("[role='tab']:has-text('All'), button:has-text('All')").first().click(timeout=1000)
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
                        
                    page.keyboard.type(symbol)
                    page.wait_for_timeout(500)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(1500)
                    
                    # Click back on the main option widget
                    widgets[0].click()
                    page.wait_for_timeout(300)
                except Exception as pane_err:
                    logger.error("Error setting symbols on split pane layout: %s", pane_err)
            else:
                # Single chart layout
                logger.info("Single chart layout. Changing active symbol to: %s", symbol)
                try:
                    if len(widgets) > 0:
                        widgets[0].click()
                        page.wait_for_timeout(300)
                    page.locator("#header-toolbar-symbol-search").click()
                    page.wait_for_timeout(800)
                    
                    try:
                        page.locator("[role='tab']:has-text('All'), button:has-text('All')").first().click(timeout=1000)
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
                        
                    page.keyboard.type(symbol)
                    page.wait_for_timeout(500)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(2000)
                except Exception as sym_err:
                    logger.error("Error setting symbol on single layout: %s", sym_err)

            # Scroll chart to trade execution time using Alt+G date navigation
            if trade_date and entry_time:
                logger.info("Scrolling chart to trade execution time: %s %s", trade_date, entry_time)
                try:
                    if len(widgets) > 0:
                        widgets[0].click()
                        page.wait_for_timeout(300)
                    page.keyboard.press("Alt+g")
                    page.wait_for_timeout(800) # wait for Go To modal to open and focus date input
                    
                    # Type the date: YYYY-MM-DD
                    page.keyboard.type(trade_date)
                    page.wait_for_timeout(300)
                    
                    # Press Tab to focus time input
                    page.keyboard.press("Tab")
                    page.wait_for_timeout(200)
                    
                    # Type the time (HH:MM)
                    page.keyboard.type(entry_time[:5])
                    page.wait_for_timeout(500)
                    
                    # Click the "Go to" submit button in the modal
                    page.locator("button:has-text('Go to'), [class*='dialog'] button:has-text('Go to')").last.click()
                    page.wait_for_timeout(3000) # wait for scrolling to settle
                except Exception as scroll_err:
                    logger.error("Error navigating to trade date/time: %s", scroll_err)
            
            # Dismiss cookie consent dialog one last time before screenshot in case it popped up late
            try:
                page.locator("button:has-text('Accept all')").click(timeout=800)
                logger.info("Dismissed cookie consent banner late check")
            except Exception:
                pass
                
            # Press Escape twice to close any lingering modals/search boxes
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)
            page.keyboard.press("Escape")
            
            # Final layout settlement wait
            page.wait_for_timeout(1000)
            
            # Make sure parent directories exist
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Take the viewport screenshot
            page.screenshot(path=output_path)
            logger.info("Successfully saved TV screenshot: %s", output_path)
            
            browser.close()
            return True
            
    except Exception as e:
        logger.error("TV Capture failed: %s", e)
        return False
