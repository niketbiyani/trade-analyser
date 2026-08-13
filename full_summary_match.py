import os
import sys
import sqlite3

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def print_db_summary():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    rows = cursor.execute(
        "SELECT id, underlying, option_type, strike, entry_time, exit_time, quantity, pnl, status, direction "
        "FROM trades WHERE date='2026-08-13'"
    ).fetchall()
    
    total_trades = len(rows)
    closed_trades = [r for r in rows if r['status'] == 'CLOSED']
    open_trades = [r for r in rows if r['status'] == 'OPEN']
    
    total_pnl = sum(r['pnl'] for r in closed_trades if r['pnl'] is not None)
    total_volume_shares = sum(r['quantity'] for r in rows)
    
    wins = [r for r in closed_trades if r['pnl'] > 0]
    losses = [r for r in closed_trades if r['pnl'] < 0]
    flat = [r for r in closed_trades if r['pnl'] == 0]
    
    print("=== Trade Analyser Database Summary for Today (2026-08-13) ===")
    print(f"Total Trades: {total_trades} (Closed: {len(closed_trades)}, Open: {len(open_trades)})")
    print(f"Total Realized P&L: {total_pnl:.2f}")
    print(f"Total Volume: {total_volume_shares} shares ({total_volume_shares / 20:.2f} lots)")
    print(f"Win Rate: {len(wins)} Wins / {len(losses)} Losses / {len(flat)} Flat (Win %: {len(wins)/len(closed_trades)*100:.1f}% if closed else 0%)")
    
    print("\n--- Breakdown by Strike/Type: ---")
    strike_stats = {}
    for r in rows:
        key = (r['underlying'], r['strike'], r['option_type'])
        if key not in strike_stats:
            strike_stats[key] = {'qty': 0, 'pnl': 0.0, 'trades': 0}
        strike_stats[key]['qty'] += r['quantity']
        if r['pnl'] is not None:
            strike_stats[key]['pnl'] += r['pnl']
        strike_stats[key]['trades'] += 1
        
    for key, stats in sorted(strike_stats.items(), key=lambda x: (x[0][1], x[0][2])):
        underlying, strike, otype = key
        print(f"  {underlying} {strike} {otype}: {stats['trades']} trades, Qty: {stats['qty']} shares ({stats['qty']/20:.2f} lots), PnL: {stats['pnl']:.2f}")
        
    conn.close()

if __name__ == "__main__":
    print_db_summary()
