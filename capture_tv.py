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
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage"
                ]
            )
            # Create a context with desktop HD resolution and 20s timeouts
            context = browser.new_context(viewport={"width": 1200, "height": 700})
            context.set_default_timeout(20000)
            context.set_default_navigation_timeout(20000)
            
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
            
            # Wait for the chart canvas to be fully rendered in the DOM before we proceed
            try:
                logger.info("Waiting for TradingView chart canvas to load...")
                page.wait_for_selector(".layout__area--center .chart-container canvas", timeout=15000)
                logger.info("TradingView chart canvas loaded successfully.")
            except Exception as wait_err:
                logger.warning("Timeout waiting for chart canvas to render: %s", wait_err)
            
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
            [id*="consent" i],
            .tv-floating-toolbar,
            [class*="drawing-toolbar" i],
            [class*="drawingToolbar" i],
            [class*="quick-tool" i],
            [class*="favorites-bar" i] { 
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
            
            page.wait_for_timeout(500)
            
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
                            dialog_inputs = page.locator("[class*='dialog'] input, [class*='popup'] input").all()
                            if len(dialog_inputs) >= 2:
                                dialog_inputs[0].click(force=True)
                                dialog_inputs[0].fill(trade_date)
                                page.wait_for_timeout(200)
                                dialog_inputs[1].click(force=True)
                                dialog_inputs[1].fill(entry_time[:5])
                                page.wait_for_timeout(300)
                            else:
                                page.keyboard.type(trade_date)
                                page.keyboard.press("Tab")
                                page.keyboard.type(entry_time[:5])
                            page.locator("text=Go to").last.click(timeout=2000)
                            page.wait_for_timeout(2000)
                            # Click Pane 1 again to deselect the Go To highlight anchor
                            widgets[0].locator("canvas").first.click(position={"x": 100, "y": 100}, force=True)
                            page.wait_for_timeout(300)
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
                            dialog_inputs = page.locator("[class*='dialog'] input, [class*='popup'] input").all()
                            if len(dialog_inputs) >= 2:
                                dialog_inputs[0].click(force=True)
                                dialog_inputs[0].fill(trade_date)
                                page.wait_for_timeout(200)
                                dialog_inputs[1].click(force=True)
                                dialog_inputs[1].fill(entry_time[:5])
                                page.wait_for_timeout(300)
                            else:
                                page.keyboard.type(trade_date)
                                page.keyboard.press("Tab")
                                page.keyboard.type(entry_time[:5])
                            page.locator("text=Go to").last.click(timeout=2000)
                            page.wait_for_timeout(2000)
                            # Click Pane 2 again to deselect the Go To highlight anchor
                            widgets[1].locator("canvas").first.click(position={"x": 100, "y": 100}, force=True)
                            page.wait_for_timeout(300)
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
                        dialog_inputs = page.locator("[class*='dialog'] input, [class*='popup'] input").all()
                        if len(dialog_inputs) >= 2:
                            dialog_inputs[0].click(force=True)
                            dialog_inputs[0].fill(trade_date)
                            page.wait_for_timeout(200)
                            dialog_inputs[1].click(force=True)
                            dialog_inputs[1].fill(entry_time[:5])
                            page.wait_for_timeout(300)
                        else:
                            page.keyboard.type(trade_date)
                            page.keyboard.press("Tab")
                            page.keyboard.type(entry_time[:5])
                        page.locator("text=Go to").last.click(timeout=2000)
                        page.wait_for_timeout(3000)
                        # Click chart canvas again to deselect highlight anchor
                        widgets[0].locator("canvas").first.click(position={"x": 100, "y": 100}, force=True)
                        page.wait_for_timeout(300)
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
            
            # Move mouse over Pane 2 canvas, then move it out to the hidden left panel zone (10, 500) to trigger mouseout events and clear crosshairs
            page.mouse.move(1200, 500)
            page.wait_for_timeout(100)
            page.mouse.move(10, 500)
            page.mouse.click(10, 500)
            page.wait_for_timeout(200)
            
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

if __name__ == "__main__":
    import sys
    # Format of args: capture_tv.py symbol interval output_path [session_id] [session_sign] [layout_id] [trade_date] [entry_time]
    if len(sys.argv) < 4:
        print("Usage: python3 capture_tv.py <symbol> <interval> <output_path> [session_id] [session_sign] [layout_id] [trade_date] [entry_time]")
        sys.exit(1)
        
    symbol = sys.argv[1]
    interval = sys.argv[2]
    output_path = sys.argv[3]
    session_id = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "None" else None
    session_sign = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "None" else None
    layout_id = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] != "None" else None
    trade_date = sys.argv[7] if len(sys.argv) > 7 and sys.argv[7] != "None" else None
    entry_time = sys.argv[8] if len(sys.argv) > 8 and sys.argv[8] != "None" else None
    
    # Configure simple logs for stdout
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    
    success = capture_screenshot(
        symbol, interval, output_path,
        session_id, session_sign, layout_id,
        trade_date, entry_time
    )
    sys.exit(0 if success else 1)
