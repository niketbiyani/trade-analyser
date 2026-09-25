import sqlite3
import os
import sys
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyser.db")

def run_analysis(month="2026-09"):
    if not os.path.exists(DB_PATH):
        print(f"Error: Database file not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("=" * 80)
    print(f"               SEPTEMBER PERFORMANCE & TRADE BEHAVIOR ANALYSIS")
    print("=" * 80)

    # 1. Monthly Overview
    cur.execute("""
        SELECT 
            COUNT(*) as total_trades,
            COUNT(DISTINCT date) as trading_days,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss_count,
            SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END) as scratch_count,
            SUM(pnl) as total_pnl,
            SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit,
            SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as gross_loss,
            AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
            AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss,
            MAX(pnl) as max_win,
            MIN(pnl) as max_loss
        FROM trades 
        WHERE date LIKE ? AND status = 'CLOSED'
    """, (f"{month}%",))
    
    summary = dict(cur.fetchone())
    total_trades = summary['total_trades'] or 0
    days = summary['trading_days'] or 0

    if total_trades == 0:
        print(f"No trades found for month pattern '{month}'.")
        print("\nAvailable date ranges in DB:")
        cur.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM trades")
        print(cur.fetchone())
        sys.exit(0)

    win_count = summary['win_count'] or 0
    loss_count = summary['loss_count'] or 0
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0
    gross_profit = summary['gross_profit'] or 0.0
    gross_loss = abs(summary['gross_loss'] or 0.0)
    pf = (gross_profit / gross_loss) if gross_loss > 0 else 999.0
    avg_trades_per_day = total_trades / days if days > 0 else 0

    print(f"\n--- OVERALL METRICS ({month}) ---")
    print(f"Trading Days:         {days} days")
    print(f"Total Closed Trades:  {total_trades} trades (Avg {avg_trades_per_day:.1f} trades/day)")
    print(f"Net Realized P&L:     ₹{summary['total_pnl']:,.2f}")
    print(f"Win Rate:             {win_rate:.1f}% ({win_count} Wins / {loss_count} Losses)")
    print(f"Profit Factor:        {pf:.2f}")
    print(f"Gross Profit:         ₹{gross_profit:,.2f}")
    print(f"Gross Loss:           ₹{gross_loss:,.2f}")
    print(f"Average Win:          ₹{summary['avg_win'] or 0:,.2f}")
    print(f"Average Loss:         ₹{summary['avg_loss'] or 0:,.2f}")
    print(f"Max Single Win:       ₹{summary['max_win'] or 0:,.2f}")
    print(f"Max Single Loss:      ₹{summary['max_loss'] or 0:,.2f}")

    # 2. Daily Breakdown & Over-Trading Flag
    print("\n" + "=" * 80)
    print("--- DAILY BREAKDOWN & OVER-TRADING ANALYSIS ---")
    print(f"{'Date':<12} | {'Trades':<8} | {'Win %':<7} | {'P&L (₹)':<12} | {'Max Win':<10} | {'Max Loss':<10} | {'Over-trading Status'}")
    print("-" * 80)

    cur.execute("""
        SELECT 
            date,
            COUNT(*) as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl) as day_pnl,
            MAX(pnl) as max_w,
            MIN(pnl) as max_l
        FROM trades
        WHERE date LIKE ? AND status = 'CLOSED'
        GROUP BY date
        ORDER BY date ASC
    """, (f"{month}%",))

    daily_rows = cur.fetchall()
    heavy_days = 0
    for row in daily_rows:
        cnt = row['count']
        w_pct = (row['wins'] / cnt * 100) if cnt > 0 else 0
        pnl_str = f"₹{row['day_pnl']:,.0f}"
        status_flag = "OK"
        if cnt > 35:
            status_flag = "HIGH VOLUME (>35)"
            heavy_days += 1
        elif cnt > 50:
            status_flag = "EXTREME OVER-TRADING"
            heavy_days += 1

        print(f"{row['date']:<12} | {cnt:<8} | {w_pct:>5.1f}% | {pnl_str:>12} | ₹{row['max_w']:<9,.0f} | ₹{row['max_l']:<9,.0f} | {status_flag}")

    print(f"\nDays exceeding target 35 trades: {heavy_days} / {days} trading days")

    # 3. Time of Day Analysis (Morning vs Midday vs Afternoon)
    print("\n" + "=" * 80)
    print("--- TIME OF DAY ANALYSIS (Execution Window) ---")
    print("Identifies if performance degrades during late-day catch-up / FOMO windows.")
    print("-" * 80)

    cur.execute("""
        SELECT 
            CASE 
                WHEN entry_time >= '09:15:00' AND entry_time < '10:30:00' THEN '1. Morning Drive (09:15-10:30)'
                WHEN entry_time >= '10:30:00' AND entry_time < '13:30:00' THEN '2. Midday Chop (10:30-13:30)'
                WHEN entry_time >= '13:30:00' AND entry_time <= '15:30:00' THEN '3. Afternoon Power Hour (13:30-15:30)'
                ELSE '4. Off-Hours / Other'
            END as time_slot,
            COUNT(*) as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl) as slot_pnl,
            AVG(pnl) as avg_pnl
        FROM trades
        WHERE date LIKE ? AND status = 'CLOSED' AND entry_time != ''
        GROUP BY time_slot
        ORDER BY time_slot ASC
    """, (f"{month}%",))

    for slot in cur.fetchall():
        scnt = slot['count']
        swin = (slot['wins'] / scnt * 100) if scnt > 0 else 0
        print(f"{slot['time_slot']:<40} | Trades: {scnt:<4} | Win %: {swin:>5.1f}% | Total P&L: ₹{slot['slot_pnl']:>10,.2f} | Avg/Trade: ₹{slot['avg_pnl']:>7,.2f}")

    # 4. Rapid-Fire / Revenge / FOMO Trade Clustering Analysis
    print("\n" + "=" * 80)
    print("--- RAPID-FIRE / FOMO CLUSTERING ANALYSIS ---")
    print("Detects trades entered within 90 seconds of a previous trade exit (Chasing / Over-reacting).")
    print("-" * 80)

    cur.execute("""
        SELECT id, date, underlying, option_type, strike, entry_time, exit_time, pnl, direction
        FROM trades
        WHERE date LIKE ? AND status = 'CLOSED' AND entry_time != ''
        ORDER BY date ASC, entry_time ASC
    """, (f"{month}%",))

    all_t = cur.fetchall()
    rapid_entries = 0
    rapid_pnl = 0.0

    for i in range(1, len(all_t)):
        prev_t = all_t[i-1]
        curr_t = all_t[i]
        
        if prev_t['date'] == curr_t['date'] and prev_t['exit_time'] and curr_t['entry_time']:
            try:
                t1 = datetime.strptime(f"{prev_t['date']} {prev_t['exit_time']}", "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(f"{curr_t['date']} {curr_t['entry_time']}", "%Y-%m-%d %H:%M:%S")
                diff = (t2 - t1).total_seconds()
                if 0 <= diff <= 90:
                    rapid_entries += 1
                    rapid_pnl += (curr_t['pnl'] or 0.0)
            except Exception:
                pass

    print(f"Rapid-Fire Re-Entries (within 90s of previous exit): {rapid_entries} trades")
    print(f"Cumulative P&L on Rapid-Fire Re-Entries:           ₹{rapid_pnl:,.2f}")
    if rapid_entries > 0:
        print(f"Avg P&L per Rapid Re-Entry:                         ₹{rapid_pnl / rapid_entries:,.2f}")

    # 5. Underlying Breakdown (NIFTY vs SENSEX vs BANKNIFTY)
    print("\n" + "=" * 80)
    print("--- PERFORMANCE BY UNDERLYING INDEX ---")
    print("-" * 80)

    cur.execute("""
        SELECT 
            underlying,
            COUNT(*) as count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl) as total_pnl,
            AVG(pnl) as avg_pnl
        FROM trades
        WHERE date LIKE ? AND status = 'CLOSED'
        GROUP BY underlying
        ORDER BY count DESC
    """, (f"{month}%",))

    for u_row in cur.fetchall():
        ucnt = u_row['count']
        uwin = (u_row['wins'] / ucnt * 100) if ucnt > 0 else 0
        print(f"Underlying: {u_row['underlying']:<10} | Trades: {ucnt:<5} | Win %: {uwin:>5.1f}% | Total P&L: ₹{u_row['total_pnl']:>10,.2f} | Avg/Trade: ₹{u_row['avg_pnl']:>7,.2f}")

    print("\n" + "=" * 80)
    print("                       KEY DIAGNOSTIC FINDINGS & TAKEAWAYS")
    print("=" * 80)

if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else "2026-09"
    run_analysis(m)
