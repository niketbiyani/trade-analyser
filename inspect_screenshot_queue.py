import os
import sys
import sqlite3
import subprocess

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def inspect_queue():
    print("--- Checking SQLite Screenshots Status ---")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Check total trades, closed trades, and how many have screenshots
    total_trades = cursor.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    closed_trades = cursor.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0]
    
    notes_with_screenshots = cursor.execute(
        "SELECT COUNT(*) FROM trade_notes WHERE image_path IS NOT NULL AND image_path != ''"
    ).fetchone()[0]
    
    print(f"Total Trades in DB: {total_trades}")
    print(f"CLOSED Trades: {closed_trades}")
    print(f"Trade Notes with screenshots: {notes_with_screenshots}")
    
    # List a few recent closed trades and their image_paths
    print("\n--- Recent Closed Trades & Screenshot Status ---")
    rows = cursor.execute(
        "SELECT t.id, t.date, t.underlying, t.entry_time, t.exit_time, n.image_path FROM trades t "
        "LEFT JOIN trade_notes n ON t.date=n.date AND t.underlying=n.underlying "
        "AND t.option_type=n.option_type AND t.strike=n.strike AND t.entry_time=n.entry_time "
        "WHERE t.status='CLOSED' ORDER BY t.date DESC, t.entry_time DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"ID: {r['id']}, Date: {r['date']}, Symbol: {r['underlying']}, Time: {r['entry_time']}-{r['exit_time']}, Image: {r['image_path']}")
        
    conn.close()
    
    print("\n--- Checking Running Chrome/Chromium/Node Processes ---")
    try:
        ps_res = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            check=True
        )
        lines = ps_res.stdout.split("\n")
        chrome_lines = [l for l in lines if "chrome" in l.lower() or "playwright" in l.lower() or "node" in l.lower()]
        print(f"Found {len(chrome_lines)} related processes running:")
        for l in chrome_lines[:30]:
            print(l)
    except Exception as e:
        print(f"Failed to run ps aux: {e}")

if __name__ == "__main__":
    inspect_queue()
