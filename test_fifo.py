"""
Standalone test for _fifo_pair using the actual June 16, 2026 trades.

Run with:  python test_fifo.py
Expected output shows each paired trade with direction, times, prices, qty, and P&L.
"""

# ─── Copy of the two functions from app.py ───────────────────────────────────

def _tx_type(trade):
    t = (trade.get("transactionType") or "").upper().strip()
    if t in ("SELL", "S", "SHORT", "-1"):
        return "SELL"
    if t in ("BUY", "B", "LONG", "1"):
        return "BUY"
    return t


def _aggregate_partial_fills(trades):
    seen = {}
    result = []
    for t in trades:
        oid = t.get("orderId") or ""
        if not oid:
            result.append(t)
            continue
        if oid not in seen:
            seen[oid] = dict(t)
            result.append(seen[oid])
        else:
            agg = seen[oid]
            pq = int(agg.get("tradedQuantity") or 0)
            nq = int(t.get("tradedQuantity") or 0)
            pp = float(agg.get("tradedPrice") or 0)
            np_ = float(t.get("tradedPrice") or 0)
            total = pq + nq
            agg["tradedQuantity"] = total
            agg["tradedPrice"] = round((pp * pq + np_ * nq) / total, 4) if total else 0
    return result


def _fifo_pair(group):
    sorted_t = sorted(
        group,
        key=lambda x: x.get("createTime") or x.get("orderCreateTime") or ""
    )
    long_q  = []
    short_q = []
    done    = []

    for t in sorted_t:
        tx    = _tx_type(t)
        ts    = t.get("createTime") or t.get("orderCreateTime") or ""
        qty   = int(t.get("tradedQuantity") or t.get("quantity") or 0)
        price = float(t.get("tradedPrice") or t.get("price") or 0)
        oid   = str(t.get("orderId") or t.get("order_id") or "")
        if qty <= 0:
            continue

        if tx == "SELL":
            rem = qty
            while rem > 0 and long_q:
                head = long_q[0]
                take = min(head["qty"], rem)
                done.append({
                    "direction":   "LONG",
                    "entry_time":  head["ts"][11:19] if len(head["ts"]) >= 19 else "",
                    "entry_price": head["price"],
                    "exit_time":   ts[11:19] if len(ts) >= 19 else "",
                    "exit_price":  price,
                    "qty":         take,
                    "order_id":    head["oid"],
                    "status":      "CLOSED",
                    "pnl":         round((price - head["price"]) * take, 2),
                })
                rem         -= take
                head["qty"] -= take
                if head["qty"] == 0:
                    long_q.pop(0)
            if rem > 0:
                short_q.append({"qty": rem, "price": price, "ts": ts, "oid": oid})

        elif tx == "BUY":
            rem = qty
            while rem > 0 and short_q:
                head = short_q[0]
                take = min(head["qty"], rem)
                done.append({
                    "direction":   "SHORT",
                    "entry_time":  head["ts"][11:19] if len(head["ts"]) >= 19 else "",
                    "entry_price": head["price"],
                    "exit_time":   ts[11:19] if len(ts) >= 19 else "",
                    "exit_price":  price,
                    "qty":         take,
                    "order_id":    head["oid"],
                    "status":      "CLOSED",
                    "pnl":         round((head["price"] - price) * take, 2),
                })
                rem         -= take
                head["qty"] -= take
                if head["qty"] == 0:
                    short_q.pop(0)
            if rem > 0:
                long_q.append({"qty": rem, "price": price, "ts": ts, "oid": oid})

    for entry in short_q:
        done.append({
            "direction":   "SHORT",
            "entry_time":  entry["ts"][11:19] if len(entry["ts"]) >= 19 else "",
            "entry_price": entry["price"],
            "exit_time":   "",
            "exit_price":  None,
            "qty":         entry["qty"],
            "order_id":    entry["oid"],
            "status":      "OPEN",
            "pnl":         None,
        })
    for entry in long_q:
        done.append({
            "direction":   "LONG",
            "entry_time":  entry["ts"][11:19] if len(entry["ts"]) >= 19 else "",
            "entry_price": entry["price"],
            "exit_time":   "",
            "exit_price":  None,
            "qty":         entry["qty"],
            "order_id":    entry["oid"],
            "status":      "OPEN",
            "pnl":         None,
        })
    return done


# ─── June 16, 2026 raw trades (from Dhan trade history) ──────────────────────
# Format matches what Dhan returns: createTime, transactionType, tradedQuantity,
# tradedPrice, orderId.  Fill in your real order IDs and times below.
# Using the values from the CSV you shared.

RAW_TRADES = [
    # SELL 130 @ 13:51
    {"createTime": "2026-06-16 13:51:00", "transactionType": "SELL",
     "tradedQuantity": 130, "tradedPrice": 42.00, "orderId": "ORD001"},
    # BUY 130 @ 13:56
    {"createTime": "2026-06-16 13:56:00", "transactionType": "BUY",
     "tradedQuantity": 130, "tradedPrice": 49.85, "orderId": "ORD002"},
    # SELL 65 @ 13:58
    {"createTime": "2026-06-16 13:58:00", "transactionType": "SELL",
     "tradedQuantity": 65, "tradedPrice": 49.50, "orderId": "ORD003"},
    # BUY 130 @ 14:05:22  (splits: 65 closes SHORT, 65 opens LONG)
    {"createTime": "2026-06-16 14:05:22", "transactionType": "BUY",
     "tradedQuantity": 130, "tradedPrice": 48.40, "orderId": "ORD004"},
    # SELL 65 @ 14:05:28  (closes the LONG 65 opened above)
    {"createTime": "2026-06-16 14:05:28", "transactionType": "SELL",
     "tradedQuantity": 65, "tradedPrice": 50.00, "orderId": "ORD005"},
    # BUY 65 @ 14:11:01
    {"createTime": "2026-06-16 14:11:01", "transactionType": "BUY",
     "tradedQuantity": 65, "tradedPrice": 45.50, "orderId": "ORD006"},
    # SELL 65 @ 14:11:22  (closes the LONG 65 opened above)
    {"createTime": "2026-06-16 14:11:22", "transactionType": "SELL",
     "tradedQuantity": 65, "tradedPrice": 37.50, "orderId": "ORD007"},
]

# ─── Run test ─────────────────────────────────────────────────────────────────

def main():
    group = _aggregate_partial_fills(RAW_TRADES)
    results = _fifo_pair(group)

    LOT_SIZE = 65
    total_pnl = 0
    print(f"\n{'Dir':<6} {'Entry':>8} {'Exit':>8} {'EntryP':>8} {'ExitP':>8} {'Qty':>5} {'Lots':>5} {'P&L':>9} {'Status'}")
    print("-" * 80)
    for r in results:
        lots = r["qty"] / LOT_SIZE
        pnl  = r["pnl"] if r["pnl"] is not None else 0
        total_pnl += pnl
        pnl_str = f"₹{pnl:+.0f}" if r["pnl"] is not None else "OPEN"
        print(
            f"{r['direction']:<6} "
            f"{r['entry_time']:>8} "
            f"{(r['exit_time'] or '—'):>8} "
            f"{r['entry_price']:>8.2f} "
            f"{(r['exit_price'] or 0):>8.2f} "
            f"{r['qty']:>5} "
            f"{lots:>5.1f} "
            f"{pnl_str:>9} "
            f"{r['status']}"
        )
    print("-" * 80)
    print(f"{'Total P&L':>60}  ₹{total_pnl:+.0f}")
    print()

    # Assertions (update expected values to match your real CSV)
    # SHORT ORD001: SELL 130@42 → BUY 130@49.85 = (42-49.85)*130 = -1020.5
    short1 = next(r for r in results if r["direction"] == "SHORT" and r["entry_time"] == "13:51:00")
    assert short1["status"] == "CLOSED", "First SHORT should be CLOSED"
    assert short1["qty"] == 130, f"Expected qty 130, got {short1['qty']}"
    assert short1["pnl"] == round((42.00 - 49.85) * 130, 2), f"P&L mismatch: {short1['pnl']}"

    # SHORT ORD003: SELL 65@49.50 → BUY 65 (half of ORD004@48.40)
    short2 = next(r for r in results if r["direction"] == "SHORT" and r["entry_time"] == "13:58:00")
    assert short2["status"] == "CLOSED"
    assert short2["qty"] == 65

    # LONG from ORD004 excess: BUY 65@48.40 → SELL 65@50.00
    long1 = next(r for r in results if r["direction"] == "LONG" and r["exit_time"] == "14:05:28")
    assert long1["status"] == "CLOSED"
    assert long1["qty"] == 65
    assert long1["pnl"] == round((50.00 - 48.40) * 65, 2)

    print("All assertions passed.")

if __name__ == "__main__":
    main()
