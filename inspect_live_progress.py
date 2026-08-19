import os
import sys
import sqlite3
import subprocess

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def check_progress():
    print("--- Inspecting systemd trade-analyser service logs (Last 30 lines) ---")
    try:
        res = subprocess.run(
            ["journalctl", "-u", "trade-analyser", "-n", "30", "--no-pager"],
            capture_output=True,
            text=True,
            check=True
        )
        print(res.stdout)
    except Exception as e:
        print(f"Failed to fetch systemd logs: {e}")
        
    print("\n--- Checking Active chrome/node/capture_tv processes ---")
    try:
        ps_res = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            check=True
        )
        lines = ps_res.stdout.split("\n")
        matching = [l for l in lines if any(x in l.lower() for x in ["chrome", "playwright", "node", "capture_tv.py"])]
        print(f"Found {len(matching)} active processes:")
        for m in matching:
            print(m)
    except Exception as e:
        print(f"Failed to run ps: {e}")
        
    print("\n--- Fetching Queue Size from Database / API ---")
    # We query the database to see how many trades are closed today and how many have screenshots
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    import datetime
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    
    total_closed = cursor.execute("SELECT COUNT(*) FROM trades WHERE date=? AND status='CLOSED'", (today_str,)).fetchone()[0]
    with_screenshots = cursor.execute(
        "SELECT COUNT(*) FROM trades t "
        "JOIN trade_notes n ON t.date=n.date AND t.underlying=n.underlying "
        "AND t.option_type=n.option_type AND t.strike=n.strike AND t.entry_time=n.entry_time "
        "WHERE t.date=? AND t.status='CLOSED' AND n.image_path IS NOT NULL AND n.image_path != ''",
        (today_str,)
    ).fetchone()[0]
    
    print(f"Today ({today_str}): Closed Trades: {total_closed}, With Screenshots: {with_screenshots}, Missing: {total_closed - with_screenshots}")
    
    conn.close()

if __name__ == "__main__":
    check_progress();
