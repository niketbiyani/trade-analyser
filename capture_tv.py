import logging
import os
import time

logger = logging.getLogger("trade-analyser.capture_tv")

def capture_screenshot(symbol: str, interval: str, output_path: str, session_id: str = None) -> bool:
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
            browser = p.chromium.launch(headless=True)
            # Create a context with desktop HD resolution
            context = browser.new_context(viewport={"width": 1200, "height": 700})
            
            # Inject TradingView login session cookie if provided
            if session_id:
                logger.info("Injecting TradingView sessionid cookie")
                context.add_cookies([{
                    "name": "sessionid",
                    "value": session_id,
                    "domain": ".tradingview.com",
                    "path": "/"
                }])
            
            page = context.new_page()
            
            # Open TradingView chart URL directly
            url = f"https://www.tradingview.com/chart/?symbol={symbol}&interval={tv_interval}"
            logger.info("Navigating to URL: %s", url)
            page.goto(url)
            
            # Wait for data loading (6s is safe for fetching index/options data)
            page.wait_for_timeout(6000)
            
            # CSS snippet to hide UI Chrome headers, sidebars, and panels for a clean chart image
            clean_css = """
            .layout__area--left, 
            .layout__area--right, 
            #header-toolbar-chart, 
            .left-panel, 
            .widget-bar, 
            .chart-controls-bar, 
            .tv-side-panel, 
            .bottom-widgetbar-content { 
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
            page.wait_for_timeout(1500) # wait for layout recalculation to settle
            
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
