import os
import sys
import sqlite3
import csv
from datetime import datetime, timedelta
from collections import defaultdict

tv_data_str = """Symbol,Side,Type,Qty,Remaining Qty,Filled Qty,Limit Price,Stop Price,Avg Fill Price,Update Time,Order ID,Expiry,Instrument,Exchange,Product,Exchange Order no
BSE:SENSEX260813P77700,Sell,Limit,5,0,5,0.05,,0.05,2026-08-13 11:00:48,23926081340225-S,DAY,OPTIDX,BSE,Normal,1786614904826272127
BSE:SENSEX260813P78100,Buy,Market,5,0,5,,,20.65,2026-08-13 11:00:44,229260813768625-B,DAY,OPTIDX,BSE,Normal,1786615055090663346
BSE:SENSEX260813P78100,Sell,Market,5,0,5,,,103.4,2026-08-13 10:58:46,229260813764625-S,DAY,OPTIDX,BSE,Normal,1786615055090243201
BSE:SENSEX260813P77700,Buy,Market,5,0,5,,,0.3,2026-08-13 10:58:33,2492608131005425-B,DAY,OPTIDX,BSE,Normal,1786614904826222870
BSE:SENSEX260813C78000,Sell,Market,50,0,50,,,25.288,2026-08-13 10:56:07,3192608131031025-S,DAY,OPTIDX,BSE,Normal,1786614963499056521
BSE:SENSEX260813C78000,Buy,Limit,50,0,50,15.3,,15.3,2026-08-13 10:56:02,3192608131030625-B,DAY,OPTIDX,BSE,Normal,1786614809116927143
BSE:SENSEX260813C77800,Sell,Limit,50,0,50,85.75,,101.138,2026-08-13 10:55:08,329260813757225-S,DAY,OPTIDX,BSE,Normal,1786614838903474511
BSE:SENSEX260813C77800,Sell,Limit,50,0,50,85.75,,101.448,2026-08-13 10:55:08,329260813757125-S,DAY,OPTIDX,BSE,Normal,1786614838903474287
BSE:SENSEX260813C77800,Sell,Limit,50,0,50,85.75,,101.894,2026-08-13 10:55:08,229260813753825-S,DAY,OPTIDX,BSE,Normal,1786614838903473308
BSE:SENSEX260813C77800,Sell,Limit,50,0,50,85.75,,101.60799,2026-08-13 10:55:08,34926081338925-S,DAY,OPTIDX,BSE,Normal,1786614838903473465
BSE:SENSEX260813C77800,Sell,Limit,50,0,50,85.75,,101.42301,2026-08-13 10:55:08,23926081339525-S,DAY,OPTIDX,BSE,Normal,1786614838903474178
BSE:SENSEX260813C77800,Buy,Limit,50,0,50,88.45,,88.45,2026-08-13 10:55:04,324260813752564-B,DAY,OPTIDX,BSE,Normal,1786614838903296356
BSE:SENSEX260813C77800,Buy,Limit,50,0,50,88.45,,88.45,2026-08-13 10:55:04,324260813752464-B,DAY,OPTIDX,BSE,Normal,1786614838903296306
BSE:SENSEX260813C77800,Buy,Limit,50,0,50,88.45,,88.45,2026-08-13 10:55:04,324260813752364-B,DAY,OPTIDX,BSE,Normal,1786614838903296278
BSE:SENSEX260813C77800,Buy,Limit,50,0,50,88.45,,88.45,2026-08-13 10:55:04,324260813752264-B,DAY,OPTIDX,BSE,Normal,1786614838903296218
BSE:SENSEX260813C77800,Buy,Limit,50,0,50,88.45,,88.45,2026-08-13 10:55:04,324260813752164-B,DAY,OPTIDX,BSE,Normal,1786614838903296164
BSE:SENSEX260813C77800,Sell,Market,50,0,50,,,82.412,2026-08-13 10:54:38,339260813759525-S,DAY,OPTIDX,BSE,Normal,1786614838903242635
BSE:SENSEX260813C77800,Sell,Market,50,0,50,,,82.104,2026-08-13 10:54:38,339260813759425-S,DAY,OPTIDX,BSE,Normal,1786614838903243269
BSE:SENSEX260813C77800,Sell,Market,50,0,50,,,82.763,2026-08-13 10:54:38,339260813759325-S,DAY,OPTIDX,BSE,Normal,1786614838903242281
BSE:SENSEX260813C77800,Sell,Market,50,0,50,,,82.927,2026-08-13 10:54:38,339260813759225-S,DAY,OPTIDX,BSE,Normal,1786614838903242902
BSE:SENSEX260813C77800,Sell,Market,50,0,50,,,82.874,2026-08-13 10:54:38,339260813759125-S,DAY,OPTIDX,BSE,Normal,1786614838903242037
BSE:SENSEX260813C77800,Buy,Market,50,0,50,,,78.921,2026-08-13 10:54:33,214260813746264-B,DAY,OPTIDX,BSE,Normal,1786614838903195917
BSE:SENSEX260813C77800,Buy,Market,50,0,50,,,78.998,2026-08-13 10:54:33,214260813746164-B,DAY,OPTIDX,BSE,Normal,1786614838903195876
BSE:SENSEX260813C77800,Buy,Market,50,0,50,,,78.976,2026-08-13 10:54:33,214260813746064-B,DAY,OPTIDX,BSE,Normal,1786614838903195853
BSE:SENSEX260813C77800,Buy,Market,50,0,50,,,78.966,2026-08-13 10:54:33,214260813745964-B,DAY,OPTIDX,BSE,Normal,1786614838903195805
BSE:SENSEX260813C77800,Buy,Market,50,0,50,,,78.641,2026-08-13 10:54:33,214260813745864-B,DAY,OPTIDX,BSE,Normal,1786614838903195708
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,8.75,,10.074,2026-08-13 10:53:31,229260813749525-S,DAY,OPTIDX,BSE,Intraday,1786614805087038968
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,8.75,,10.024,2026-08-13 10:53:31,229260813749425-S,DAY,OPTIDX,BSE,Intraday,1786614805087038753
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,8.75,,10.354,2026-08-13 10:53:31,229260813749325-S,DAY,OPTIDX,BSE,Intraday,1786614805087037549
BSE:SENSEX260813C79000,Sell,Limit,5,0,5,0.2,,0.2,2026-08-13 10:53:31,34926081338825-S,DAY,OPTIDX,BSE,Intraday,1786614323569492820
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,8.75,,10.1,2026-08-13 10:53:31,34926081338725-S,DAY,OPTIDX,BSE,Intraday,1786614805087038221
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,8.75,,10.189,2026-08-13 10:53:31,23926081339425-S,DAY,OPTIDX,BSE,Intraday,1786614805087038131
BSE:SENSEX260813P76800,Sell,Limit,5,0,5,0.1,,0.15,2026-08-13 10:53:31,23926081339325-S,DAY,OPTIDX,BSE,Intraday,1786614509302229307
BSE:SENSEX260813P77800,Buy,Limit,50,0,50,16.3,,16.3,2026-08-13 10:53:23,314260813776965-B,DAY,OPTIDX,BSE,Intraday,1786614643312840843
BSE:SENSEX260813P77800,Buy,Limit,50,0,50,16.3,,16.3,2026-08-13 10:53:23,314260813776865-B,DAY,OPTIDX,BSE,Intraday,1786614643312840806
BSE:SENSEX260813P77800,Buy,Limit,50,0,50,16.3,,16.3,2026-08-13 10:53:23,314260813776765-B,DAY,OPTIDX,BSE,Intraday,1786614643312840775
BSE:SENSEX260813P77800,Buy,Limit,50,0,50,16.3,,16.3,2026-08-13 10:53:23,314260813776665-B,DAY,OPTIDX,BSE,Intraday,1786614643312840720
BSE:SENSEX260813P77800,Buy,Limit,50,0,50,16.3,,16.3,2026-08-13 10:53:23,314260813776565-B,DAY,OPTIDX,BSE,Intraday,1786614643312840629
BSE:SENSEX260813P77900,Buy,Market,5,0,5,,,63.76,2026-08-13 10:52:52,229260813748325-B,DAY,OPTIDX,BSE,Intraday,1786614610075937213
BSE:SENSEX260813P77900,Sell,Market,5,0,5,,,53.43,2026-08-13 10:52:05,339260813752825-S,DAY,OPTIDX,BSE,Intraday,1786614610075714555
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,41.1,,51.369,2026-08-13 10:50:31,229260813743325-S,DAY,OPTIDX,BSE,Intraday,1786614418918851809
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,41.1,,50.626,2026-08-13 10:50:31,229260813743225-S,DAY,OPTIDX,BSE,Intraday,1786614418918850841
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,41.1,,50.988,2026-08-13 10:50:31,34926081338525-S,DAY,OPTIDX,BSE,Intraday,1786614418918852062
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,41.1,,51.35,2026-08-13 10:50:31,34926081338425-S,DAY,OPTIDX,BSE,Intraday,1786614418918851038
BSE:SENSEX260813P77800,Sell,Limit,50,0,50,41.1,,50.126,2026-08-13 10:50:31,23926081338925-S,DAY,OPTIDX,BSE,Intraday,1786614418918852901
BSE:SENSEX260813P77800,Buy,Market,50,0,50,,,37.04,2026-08-13 10:50:16,314260813727863-B,DAY,OPTIDX,BSE,Intraday,1786614418918728347
"""

db_path = "/root/trade-analyser/analyser.db"
if not os.path.exists(db_path):
    db_path = "analyser.db"

def parse_option_from_symbol(sym):
    # BSE:SENSEX260813P77700 -> strike 77700, type PE
    # BSE:SENSEX260813C78000 -> strike 78000, type CE
    sym = sym.split(':')[-1]
    if 'P' in sym:
        parts = sym.split('P')
        strike = float(parts[-1])
        return strike, 'PE'
    elif 'C' in sym:
        parts = sym.split('C')
        strike = float(parts[-1])
        return strike, 'CE'
    return 0.0, 'CE'

def check_missing_volume():
    # 1. Group TV orders by (strike, type, time, side) and sum quantity
    tv_volumes = defaultdict(int)
    lines = tv_data_str.strip().split('\n')
    reader = csv.DictReader(lines)
    
    for row in reader:
        strike, otype = parse_option_from_symbol(row['Symbol'])
        side = row['Side'].upper()
        qty = int(row['Filled Qty']) * 20 # Lot size multiplier: TV reports lot count (e.g. 5 lots, 50 lots), SQLite reports shares count (lot size of SENSEX is 20!).
        # Wait, let's verify if lot size is 20: 
        # Yes! BSE SENSEX lot size is 20. So 50 lots in TV = 1000 shares in SQLite!
        # Let's verify this: "UNMATCHED ORDER: Side: BUY, Qty: 50, IST Time: 15:24:33"
        # And in SQLite we have: "ID: 2049, Qty: 1000" which is exactly 50 lots * 20!
        # So yes, TradingView Qty is indeed in LOTS, not shares!
        
        update_time_str = row['Update Time']
        update_dt = datetime.strptime(update_time_str, "%Y-%m-%d %H:%M:%S")
        ist_dt = update_dt + timedelta(hours=4, minutes=30)
        ist_time_str = ist_dt.strftime("%H:%M:%S")
        
        key = (strike, otype, ist_time_str, side)
        tv_volumes[key] += qty

    # 2. Get SQLite trades and reconstruct execution logs
    # Since SQLite trades are paired, each trade has an Entry fill and an Exit fill.
    db_volumes = defaultdict(int)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    trades = cursor.execute("SELECT * FROM trades WHERE date='2026-08-13'").fetchall()
    
    for t in trades:
        strike = float(t['strike'])
        otype = t['option_type']
        qty = int(t['quantity'])
        
        # Entry time fill
        entry_side = 'BUY' if t['direction'] == 'LONG' else 'SELL'
        key_entry = (strike, otype, t['entry_time'], entry_side)
        db_volumes[key_entry] += qty
        
        # Exit time fill (only if closed)
        if t['status'] == 'CLOSED':
            exit_side = 'SELL' if t['direction'] == 'LONG' else 'BUY'
            key_exit = (strike, otype, t['exit_time'], exit_side)
            db_volumes[key_exit] += qty

    # 3. Compare them!
    print("--- Comparing TradingView Volume (Lots * 20) vs SQLite Volume (Shares) ---")
    missing_any = False
    for key, tv_qty in sorted(tv_volumes.items(), key=lambda x: x[0][2]):
        strike, otype, time_str, side = key
        db_qty = db_volumes.get(key, 0)
        
        # Check if the database is missing any quantity
        if db_qty < tv_qty:
            print(f"MISSING VOLUME: {otype} Strike {strike} at {time_str} {side}: TV={tv_qty} shares, SQLite={db_qty} shares. Difference = {tv_qty - db_qty} shares!")
            missing_any = True
        else:
            print(f"MATCHED: {otype} Strike {strike} at {time_str} {side}: TV={tv_qty} shares, SQLite={db_qty} shares.")
            
    if not missing_any:
        print("\nSUCCESS: Absolutely NO TradingView volumes are missing from your database! Every execution in the TV list is fully accounted for in SQLite.")
        
    conn.close()

if __name__ == "__main__":
    check_missing_volume()
