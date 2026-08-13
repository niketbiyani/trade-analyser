import os
import sys
import sqlite3
from datetime import datetime, timedelta

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def check_inserted_today():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Let's get the timestamp for 24 hours ago
    cutoff_time = datetime.now() - timedelta(hours=24)
    cutoff_ts = cutoff_time.timestamp()
    
    print(f"Cutoff Time (24h ago): {cutoff_time} (TS: {cutoff_ts})")
    
    # Query all trades created in the last 24 hours
    rows = cursor.execute(
        "SELECT id, date, underlying, option_type, strike, entry_time, exit_time, quantity, pnl, status, dhan_order_id, created_at "
        "FROM trades WHERE created_at >= ? ORDER BY date ASC, entry_time ASC",
        (cutoff_ts,)
    ).fetchall()
    
    print(f"\nTotal trades inserted/updated in the last 24 hours: {len(rows)}")
    
    # Group by date and calculate total PnL
    by_date = {}
    for r in rows:
        d = r['date']
        if d not in by_date:
            by_date[d] = {'pnl': 0.0, 'count': 0, 'volume': 0}
        if r['pnl'] is not None:
            by_date[d]['pnl'] += r['pnl']
        by_date[d]['count'] += 1
        by_date[d]['volume'] += r['quantity']
        
    print("\nBreakdown of last 24h insertions by entry date:")
    for d, stats in sorted(by_date.items()):
        print(f"  Entry Date {d}: {stats['count']} trades, Vol: {stats['volume']} shares, PnL: {stats['pnl']:.2f}")
        
    # Check if there are any carry-forward trades (where entry date is before today, but exit is today)
    print("\n--- Details of all trades created/updated in last 24h: ---")
    for r in rows:
        print(f"ID: {r['id']}, Date: {r['date']}, {r['underlying']} {r['strike']} {r['option_type']}, Entry: {r['entry_time']}, Exit: {r['exit_time']}, Qty: {r['quantity']}, PnL: {r['pnl']}, Status: {r['status']}, OrderId: {r['dhan_order_id']}")
        
    conn.close()

if __name__ == "__main__":
    check_inserted_today()
