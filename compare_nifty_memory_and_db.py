import os
import sys
import sqlite3
from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq as DhanHQ
from datetime import date
from collections import defaultdict

# Add project path to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import _fifo_pair, _is_option, _aggregate_partial_fills

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def compare_nifty():
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
    
    # Filter NIFTY option executions
    nifty_opts = []
    for t in raw:
        sym = (t.get("tradingSymbol") or "").upper()
        if "NIFTY" in sym and _is_option(t):
            nifty_opts.append(t)
            
    print(f"Total raw NIFTY options executions today: {len(nifty_opts)}")
    
    # Group by (date, security_id)
    groups = defaultdict(list)
    today_str = str(date.today())
    for t in nifty_opts:
        ts = (t.get("createTime") or t.get("orderCreateTime") or t.get("exchangeTime") or "")
        sid = str(t.get("securityId") or "")
        trade_date = ts[:10] if len(ts) >= 10 else today_str
        groups[(trade_date, sid)].append(t)
        
    # Pair in-memory
    mem_trades = []
    for (trade_date, sid), group in groups.items():
        group = _aggregate_partial_fills(group)
        paired = _fifo_pair(group)
        for p in paired:
            if p['status'] == 'CLOSED':
                mem_trades.append(p)
                
    mem_pnl = sum(p['pnl'] for p in mem_trades)
    print(f"In-Memory NIFTY FIFO Paired P&L: {mem_pnl:.2f} (Total Trades: {len(mem_trades)})")
    
    # 2. Fetch SQLite NIFTY Trades today
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    db_trades = cursor.execute("SELECT * FROM trades WHERE date=? AND underlying='NIFTY' AND status='CLOSED'", (today_str,)).fetchall()
    db_pnl = sum(r['pnl'] for r in db_trades if r['pnl'] is not None)
    print(f"SQLite NIFTY Paired P&L: {db_pnl:.2f} (Total Trades: {len(db_trades)})")
    
    # Scan for differences
    print("\n--- Scanning NIFTY for Trade Differences: ---")
    used_db_ids = set()
    for mt in sorted(mem_trades, key=lambda x: x['entry_time']):
        matched = False
        for dt in db_trades:
            if dt['id'] in used_db_ids:
                continue
            if dt['entry_time'] == mt['entry_time'] and dt['exit_time'] == mt['exit_time'] and dt['quantity'] == mt['qty']:
                used_db_ids.add(dt['id'])
                matched = True
                break
        if not matched:
            print(f"MISSING IN DB: Entry: {mt['entry_time']}, Exit: {mt['exit_time']}, Qty: {mt['qty']}, PnL: {mt['pnl']}")
            
    conn.close()

if __name__ == "__main__":
    compare_nifty()
