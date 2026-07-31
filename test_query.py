import os
import sys
import sqlite3

# Ensure we can import DB_PATH
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from app import DB_PATH

print("DB Path:", DB_PATH)
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Get trades for today
rows = conn.execute("SELECT id, date, underlying, option_type, strike, status, entry_time FROM trades ORDER BY date DESC LIMIT 10").fetchall()
print(f"\nLast 10 trades in database:")
for r in rows:
    print(dict(r))

# Get count of trades on 2026-07-31
count_total = conn.execute("SELECT count(*) FROM trades WHERE date='2026-07-31'").fetchone()[0]
count_closed = conn.execute("SELECT count(*) FROM trades WHERE date='2026-07-31' AND status='CLOSED'").fetchone()[0]
print(f"\n2026-07-31 stats:")
print("Total trades:", count_total)
print("Closed trades (status='CLOSED'):", count_closed)
