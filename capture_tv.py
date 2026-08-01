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
            
            # Inject CSS and dismiss cookie banner across all page frames (handles iframes)
            for frame in page.frames:
                try:
                    frame.add_style_tag(content=clean_css)
                except Exception:
                    pass
                for selector in ["text=Accept all", "button:has-text('Accept')", "[class*='cookie' i] button"]:
                    try:
                        btn = frame.locator(selector).first
                        if btn.is_visible():
                            btn.click(timeout=1000)
                            logger.info("Dismissed cookie consent banner in frame with: %s", selector)
                            break
                    except Exception:
                        pass
            
            page.wait_for_timeout(1000)
            
            # If any pane is currently maximized (e.g. RSI), restore the split layout
            try:
                restore_btn = page.locator("[title*='Restore' i], [aria-label*='Restore' i]").first
                if restore_btn.is_visible():
                    logger.info("Maximized pane detected. Restoring split layout...")
                    restore_btn.click()
                    page.wait_for_timeout(800)
            except Exception as restore_err:
                logger.error("Error checking/restoring maximized pane layout: %s", restore_err)
            
            # Target active chart containers within layout__area--center to exclude sidebars/watchlists
            widgets = page.locator(".layout__area--center > .chart-container").all()
            
            # Set the symbols on the layout
            if len(widgets) > 1:
                logger.info("Multi-chart layout detected (%d widgets). setting symbols and scrolling split panes sequentially.", len(widgets))
                try:
                    # ── PANE 1 (Left Option Pane) ──
                    # Focus first chart by clicking its canvas directly
                    widgets[0].locator("canvas").first.click(position={"x": 100, "y": 100}, force=True)
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
                    page.wait_for_timeout(2000)
                    
                    # Scroll Pane 1 to trade execution time
                    if trade_date and entry_time:
                        logger.info("Scrolling Pane 1 to trade execution time: %s %s", trade_date, entry_time)
                        try:
                            page.keyboard.press("Alt+g")
                            page.wait_for_timeout(800)
                            page.keyboard.type(trade_date)
                            page.wait_for_timeout(300)
                            page.keyboard.press("Tab")
                            page.wait_for_timeout(200)
                            page.keyboard.type(entry_time[:5])
                            page.wait_for_timeout(500)
                            page.locator("text=Go to").last.click(timeout=2000)
                            page.wait_for_timeout(2000)
                        except Exception as scroll_err1:
                            logger.error("Error scrolling Pane 1: %s", scroll_err1)

                    # ── PANE 2 (Right Option Pane) ──
                    # Focus second pane by clicking its canvas directly
                    logger.info("Clicking Pane 2 canvas directly to activate Pane 2")
                    widgets[1].locator("canvas").first.click(position={"x": 100, "y": 100}, force=True)
                    page.wait_for_timeout(300)
                    
                    # Change symbol on Pane 2 to the same Option symbol
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
                    
                    # Scroll Pane 2 to trade execution time
                    if trade_date and entry_time:
                        logger.info("Scrolling Pane 2 to trade execution time: %s %s", trade_date, entry_time)
                        try:
                            page.keyboard.press("Alt+g")
                            page.wait_for_timeout(800)
                            page.keyboard.type(trade_date)
                            page.wait_for_timeout(300)
                            page.keyboard.press("Tab")
                            page.wait_for_timeout(200)
                            page.keyboard.type(entry_time[:5])
                            page.wait_for_timeout(500)
                            page.locator("text=Go to").last.click(timeout=2000)
                            page.wait_for_timeout(2000)
                        except Exception as scroll_err2:
                            logger.error("Error scrolling Pane 2: %s", scroll_err2)
                            
                    # Focus back on Pane 1
                    widgets[0].locator("canvas").first.click(position={"x": 100, "y": 100}, force=True)
                    page.wait_for_timeout(300)
                    
                except Exception as pane_err:
                    logger.error("Error setting symbols and scrolling split pane layout: %s", pane_err)
            else:
                # Single chart layout
                logger.info("Single chart layout. Changing active symbol to: %s", symbol)
                try:
                    if len(widgets) > 0:
                        widgets[0].locator("canvas").first.click(position={"x": 100, "y": 100}, force=True)
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

                # Scroll single chart to trade execution time
                if trade_date and entry_time:
                    logger.info("Scrolling single chart to trade execution time: %s %s", trade_date, entry_time)
                    try:
                        page.keyboard.press("Alt+g")
                        page.wait_for_timeout(800)
                        page.keyboard.type(trade_date)
                        page.wait_for_timeout(300)
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(200)
                        page.keyboard.type(entry_time[:5])
                        page.wait_for_timeout(500)
                        page.locator("text=Go to").last.click(timeout=2000)
                        page.wait_for_timeout(3000)
                    except Exception as scroll_err:
                        logger.error("Error navigating single chart to trade date/time: %s", scroll_err)
            
            # Dismiss cookie consent dialog on all frames one last time before screenshot
            for frame in page.frames:
                try:
                    frame.add_style_tag(content=clean_css)
                except Exception:
                    pass
                for selector in ["text=Accept all", "button:has-text('Accept')", "[class*='cookie' i] button"]:
                    try:
                        btn = frame.locator(selector).first
                        if btn.is_visible():
                            btn.click(timeout=800)
                            logger.info("Dismissed cookie consent banner late check in frame with: %s", selector)
                            break
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
