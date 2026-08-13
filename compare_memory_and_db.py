import os
import sys
import sqlite3
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ
from datetime import date
from collections import defaultdict

# Add project path to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import _fifo_pair, _process_raw_trades, _is_option, _aggregate_partial_fills

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def compare_memory_and_db():
    load_dotenv(override=True)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    
    ctx = DhanContext(client_id, access_token)
    dhan = DhanHQ(ctx)
    
    # 1. Fetch raw executions
    resp = dhan.get_trade_book()
    raw = resp.get("data", []) if resp.get("status") == "success" else []
    
    # Deduplicate raw like app.py does
    seen_raw = set()
    deduped = []
    for t in raw:
        key = (t.get("orderId") or "", t.get("exchangeTradeId") or t.get("exchangeOrderId") or "")
        if key[0] or key[1]:
            if key in seen_raw:
                continue
            seen_raw.add(key)
        deduped.append(t)
    raw = deduped
    
    # Filter options
    opts = [t for t in raw if _is_option(t)]
    
    # Group by (date, security_id)
    groups = defaultdict(list)
    today_str = str(date.today())
    for t in opts:
        # Get ts
        ts = (t.get("createTime") or t.get("orderCreateTime") or t.get("exchangeTime") or "")
        sid = str(t.get("securityId") or "")
        trade_date = ts[:10] if len(ts) >= 10 else today_str
        groups[(trade_date, sid)].append(t)
        
    # Pair in-memory
    mem_trades = []
    for (trade_date, sid), group in groups.items():
        # Aggregate partial fills
        group = _aggregate_partial_fills(group)
        # Pair
        paired = _fifo_pair(group)
        for p in paired:
            if p['status'] == 'CLOSED':
                mem_trades.append(p)
                
    mem_pnl = sum(p['pnl'] for p in mem_trades)
    print(f"In-Memory FIFO Paired P&L: {mem_pnl:.2f} (Total Trades: {len(mem_trades)})")
    
    # 2. Fetch SQLite Trades today
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    db_trades = cursor.execute("SELECT * FROM trades WHERE date='2026-08-13' AND status='CLOSED'").fetchall()
    db_pnl = sum(r['pnl'] for r in db_trades if r['pnl'] is not None)
    print(f"SQLite Paired P&L: {db_pnl:.2f} (Total Trades: {len(db_trades)})")
    
    # Let's find any trade in memory that is NOT in SQLite, or has a different PnL!
    print("\n--- Scanning for Trade Differences: ---")
    used_db_ids = set()
    for mt in sorted(mem_trades, key=lambda x: x['entry_time']):
        # Find match in DB
        matched = False
        for dt in db_trades:
            if dt['id'] in used_db_ids:
                continue
            # Match by entry time, exit time, qty, and strike
            if dt['entry_time'] == mt['entry_time'] and dt['exit_time'] == mt['exit_time'] and dt['quantity'] == mt['qty']:
                if abs(dt['pnl'] - mt['pnl']) > 0.01:
                    print(f"PnL MISMATCH: Entry: {mt['entry_time']}, Exit: {mt['exit_time']}, Qty: {mt['qty']}. Memory PnL={mt['pnl']}, SQLite PnL={dt['pnl']}")
                used_db_ids.add(dt['id'])
                matched = True
                break
        if not matched:
            print(f"MISSING IN DB: Entry: {mt['entry_time']}, Exit: {mt['exit_time']}, Qty: {mt['qty']}, PnL: {mt['pnl']}")
            
    conn.close()

if __name__ == "__main__":
    compare_memory_and_db()
