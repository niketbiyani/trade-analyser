"""Trade Analyser — post-session option trade review on the underlying index chart.
Single-file Flask app. Port 5556 by default.
"""

import logging
import math
import os
import random
import socket
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────

APP_VERSION = "v58"

PORT    = int(os.getenv("PORT", "5556"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyser.db")

LOT_SIZES = {
    "NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20,
    "FINNIFTY": 65, "MIDCPNIFTY": 75,
}

DHAN_INDEX_IDS = {
    "NIFTY":  {"security_id": "13", "exchange_segment": "IDX_I",  "instrument_type": "INDEX"},
    "SENSEX": {"security_id": "51", "exchange_segment": "BSE_EQ", "instrument_type": "INDEX"},
}

# Fallback candidates for NIFTY tried in order when primary returns empty data.
NIFTY_FALLBACKS = [
    {"security_id": "13", "exchange_segment": "IDX_I",  "instrument_type": "INDEX"},
    {"security_id": "13", "exchange_segment": "NSE_EQ", "instrument_type": "INDEX"},
    {"security_id": "13", "exchange_segment": "NSE",    "instrument_type": "INDEX"},
]

# Fallback candidates tried in order when SENSEX returns empty data.
SENSEX_FALLBACKS = [
    {"security_id": "51", "exchange_segment": "BSE_EQ",  "instrument_type": "INDEX"},
    {"security_id": "1",  "exchange_segment": "BSE_EQ",  "instrument_type": "INDEX"},
    {"security_id": "51", "exchange_segment": "BSE",     "instrument_type": "INDEX"},
    {"security_id": "1",  "exchange_segment": "BSE",     "instrument_type": "INDEX"},
    {"security_id": "19", "exchange_segment": "BSE_EQ",  "instrument_type": "INDEX"},
    {"security_id": "16", "exchange_segment": "BSE_EQ",  "instrument_type": "INDEX"},
]


CHART_TIMEOUT = 10

# ── Database ────────────────────────────────────────────────────────

_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_db(_conn)
    return _conn


def _init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT    NOT NULL,
                underlying      TEXT    NOT NULL,
                option_type     TEXT    NOT NULL,
                strike          REAL    NOT NULL,
                expiry          TEXT    DEFAULT '',
                entry_time      TEXT    DEFAULT '',
                entry_price     REAL    DEFAULT 0,
                exit_time       TEXT    DEFAULT '',
                exit_price      REAL,
                quantity        INTEGER DEFAULT 0,
                lot_size        INTEGER DEFAULT 1,
                lots            REAL    DEFAULT 0,
                pnl             REAL,
                status          TEXT    DEFAULT 'CLOSED',
                notes           TEXT    DEFAULT '',
                security_id     TEXT    DEFAULT '',
                exchange_segment TEXT   DEFAULT 'NSE_FNO',
                dhan_order_id   TEXT    DEFAULT '',
                created_at      REAL    DEFAULT 0,
                direction       TEXT    DEFAULT 'SHORT'
            );
            CREATE INDEX IF NOT EXISTS idx_date ON trades(date);
            CREATE INDEX IF NOT EXISTS idx_underlying ON trades(underlying);
            CREATE TABLE IF NOT EXISTS trade_notes (
                date         TEXT NOT NULL,
                underlying   TEXT NOT NULL,
                option_type  TEXT NOT NULL,
                strike       REAL NOT NULL,
                entry_time   TEXT NOT NULL,
                notes        TEXT DEFAULT '',
                updated_at   REAL DEFAULT 0,
                PRIMARY KEY (date, underlying, option_type, strike, entry_time)
            );
        """)
    # migrate existing DBs that lack the direction column
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN direction TEXT DEFAULT 'SHORT'")
        conn.execute("UPDATE trades SET direction='SHORT' WHERE direction IS NULL")
        conn.commit()
    except Exception:
        pass  # column already exists
    # fix LONG trades stored with swapped entry/exit times from older pairing logic
    try:
        rows = conn.execute(
            "SELECT id, entry_time, entry_price, exit_time, exit_price, quantity"
            " FROM trades WHERE direction='LONG' AND status='CLOSED'"
            " AND exit_time != '' AND entry_time > exit_time"
        ).fetchall()
        for row in rows:
            pnl = round((row["exit_price"] - row["entry_price"]) * row["quantity"], 2)
            conn.execute(
                "UPDATE trades SET entry_time=?, entry_price=?, exit_time=?, exit_price=?, pnl=? WHERE id=?",
                (row["exit_time"], row["exit_price"], row["entry_time"], row["entry_price"], pnl, row["id"]),
            )
        if rows:
            conn.commit()
            logger.info("Migrated %d LONG trades: corrected entry/exit time order", len(rows))
    except Exception:
        pass


# ── Dhan client ────────────────────────────────────────────────────────────────────


def _dhan_client():
    from dhanhq import DhanContext, dhanhq as DhanHQ  # noqa: PLC0415
    load_dotenv(override=True)
    client_id    = os.getenv("DHAN_CLIENT_ID", "")
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN not set in .env")
    return DhanHQ(DhanContext(client_id, access_token))


def _to_dhan_date(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%m-%Y")


def _extract_batch(resp) -> list[dict]:
    if resp is None:
        return []
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        for key in ("data", "records", "trades", "tradeData"):
            val = resp.get(key)
            if isinstance(val, list) and val:
                return [r for r in val if isinstance(r, dict)]
    return []


def _is_no_records_error(resp) -> bool:
    """True when Dhan returns TRADE_RESOURCE_ERROR meaning 'no records on this date'."""
    if not isinstance(resp, dict):
        return False
    remarks = resp.get("remarks", {})
    if not isinstance(remarks, dict):
        return False
    return "RESOURCE_ERROR" in (remarks.get("error_type") or "")


def _parse_csv_trades(content: str) -> tuple[list[dict], str]:
    """Parse a Dhan trade history CSV into raw trade dicts (same shape as API records).

    Handles multiple column naming conventions and datetime formats including:
    - Dhan API export (tradingSymbol, createTime, ...)
    - Dhan app export (Stock Name, Timestamp, Price (₹), ...)
    """
    import csv
    import io
    import re as _re

    def _nc(name: str) -> str:
        """Normalise a column name: lower, strip non-ASCII, collapse non-alphanum to _."""
        s = name.lower().strip()
        s = _re.sub(r'[^\x00-\x7f]', '', s)   # drop non-ASCII (₹, etc.)
        s = _re.sub(r'[^a-z0-9]+', '_', s)     # non-alphanum → _
        return s.strip('_')

    def _find(sample: dict, candidates: list) -> str | None:
        norm = {_nc(k): k for k in sample}
        for c in candidates:
            if _nc(c) in norm:
                return norm[_nc(c)]
        return None

    def _parse_date(val: str) -> str | None:
        val = val.strip()
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d",
                    "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y", "%b %d %Y"):
            try:
                return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None

    def _split_dt(val: str) -> tuple[str | None, str]:
        """Split any datetime string into (YYYY-MM-DD, HH:MM:SS)."""
        val = val.strip()
        # ISO: YYYY-MM-DD...
        if _re.match(r'^\d{4}-\d{2}-\d{2}', val):
            d = val[:10]
            t = val[11:19] if len(val) >= 19 else (val[11:] + ":00")[:8]
            return d, t
        # Generic: find HH:MM[:SS] token, treat remainder as date
        tokens = val.split()
        time_tok, date_toks = None, []
        for tok in tokens:
            if _re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', tok):
                time_tok = tok
            else:
                date_toks.append(tok)
        d = _parse_date(" ".join(date_toks))
        t = time_tok if time_tok else "09:15:00"
        if len(t) == 5:
            t += ":00"
        return d, t

    try:
        rows = list(csv.DictReader(io.StringIO(content)))
    except Exception as e:
        return [], f"CSV parse error: {e}"

    if not rows:
        return [], "CSV has no data rows"

    s = rows[0]
    c_sym    = _find(s, ["tradingSymbol","trading_symbol","stock_name","stock name",
                         "symbol","instrument","scrip","description","name"])
    c_tx     = _find(s, ["transactionType","transaction_type","transaction",
                         "trade_type","buy_sell","buysell","side","b_s","type"])
    c_qty    = _find(s, ["tradedQuantity","traded_quantity","quantity","qty",
                         "trade_qty","executed_qty"])
    c_price  = _find(s, ["tradedPrice","traded_price","price","rate",
                         "avg_price","trade_price","executed_price"])
    c_date   = _find(s, ["trade_date","tradeDate","date","order_date","exchange_date"])
    c_time   = _find(s, ["trade_time","tradeTime","time","order_time","exchange_time"])
    c_seg    = _find(s, ["exchangeSegment","exchange_segment","segment","exchange"])
    c_oid    = _find(s, ["orderId","order_id","orderid","trade_id","tradeid",
                         "trade_no","trade_no."])
    c_opt    = _find(s, ["drvOptionType","option_type","optionType","call_put","put_call"])
    c_strike = _find(s, ["drvStrikePrice","strike_price","strike","strikeprice"])
    c_expiry = _find(s, ["drvExpiryDate","expiry_date","expiry","expirydate"])
    c_create = _find(s, ["createTime","create_time","created_time","timestamp",
                         "datetime","order_date_time"])
    c_sid    = _find(s, ["securityId","security_id","sec_id"])

    if not c_sym:
        cols = list(s.keys())
        return [], (f"Cannot detect instrument/symbol column. "
                    f"Columns found: {cols}. "
                    f"Expected one of: Stock Name, tradingSymbol, symbol")

    result = []
    for row in rows:
        def g(col, _row=row):
            return (_row.get(col) or "").strip() if col else ""

        # Build createTime
        create_val = g(c_create)
        if create_val:
            d, t = _split_dt(create_val)
        elif g(c_date):
            d = _parse_date(g(c_date))
            t = g(c_time).strip() or "09:15:00"
            if len(t) == 5:
                t += ":00"
        else:
            continue
        if not d:
            continue
        create_time = f"{d} {t}"

        sym = g(c_sym)
        if not sym:
            continue

        tx = g(c_tx).upper()
        if tx in ("B", "BUY", "LONG", "1"):     tx = "BUY"
        elif tx in ("S", "SELL", "SHORT", "-1"): tx = "SELL"

        try:
            qty = int(float(g(c_qty).replace(",", "")))
        except ValueError:
            continue
        if qty <= 0:
            continue

        try:
            price = float(g(c_price).replace(",", ""))
        except ValueError:
            price = 0.0

        seg = g(c_seg) or ("BSE_FNO" if "SENSEX" in sym.upper() else "NSE_FNO")
        oid = g(c_oid)
        sid = g(c_sid) or sym  # trading symbol as security ID for grouping

        # Auto-detect option type from symbol when no explicit column
        opt_type = g(c_opt)
        if not opt_type:
            su = sym.upper()
            if su.endswith("CALL") or " CALL " in su: opt_type = "CALL"
            elif su.endswith("PUT") or " PUT " in su:  opt_type = "PUT"
            elif su.endswith("CE"):                     opt_type = "CE"
            elif su.endswith("PE"):                     opt_type = "PE"

        # Extract strike from symbol when no explicit column (e.g. "SENSEX 11 JUN 74300 CALL")
        strike = g(c_strike)
        if not strike:
            m = _re.search(r'\b(\d{4,6})\b', sym)
            strike = m.group(1) if m else ""

        result.append({
            "tradingSymbol":   sym,
            "transactionType": tx,
            "tradedQuantity":  qty,
            "tradedPrice":     price,
            "createTime":      create_time,
            "orderId":         oid,
            "exchangeSegment": seg,
            "drvOptionType":   opt_type,
            "drvStrikePrice":  strike,
            "drvExpiryDate":   g(c_expiry),
            "securityId":      sid,
        })

    return result, ""


def _aggregate_partial_fills(trades: list[dict]) -> list[dict]:
    """Merge partial fills that share the same orderId into one record (summed qty, weighted avg price)."""
    seen: dict = {}
    result: list[dict] = []
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


def _process_raw_trades(raw: list[dict], extra_diag: dict | None = None) -> dict:
    """Dedup, filter options, pair SELL/BUY, insert into DB. Returns result dict."""
    diag: dict = dict(extra_diag or {})
    today_str = str(date.today())

    seen: set = set()
    deduped = []
    for t in raw:
        key = (t.get("orderId") or "", t.get("exchangeTradeId") or t.get("exchangeOrderId") or "")
        if key[0] or key[1]:
            if key in seen:
                continue
            seen.add(key)
        deduped.append(t)
    raw = deduped

    opts = [t for t in raw if _is_option(t)]
    logger.info("Total raw=%d (after dedup)  options=%d", len(raw), len(opts))

    if raw and not opts:
        logger.warning("No options detected. Sample record: %s", raw[0])
        diag["sample_non_option"] = raw[0]
    elif opts:
        logger.info("Sample option: %s", opts[0])

    groups: dict[tuple, list] = defaultdict(list)
    for t in opts:
        ts  = t.get("createTime") or t.get("exchangeTime") or t.get("orderCreateTime") or ""
        sid = str(t.get("securityId") or t.get("security_id") or "")
        trade_date = ts[:10] if len(ts) >= 10 else today_str
        groups[(trade_date, sid)].append(t)

    imported = skipped = 0
    db = get_db()

    for (trade_date, sid), group in groups.items():
        group = _aggregate_partial_fills(group)
        sells = sorted(
            [t for t in group if _tx_type(t) == "SELL"],
            key=lambda x: x.get("createTime") or x.get("orderCreateTime") or "",
        )
        buys = sorted(
            [t for t in group if _tx_type(t) == "BUY"],
            key=lambda x: x.get("createTime") or x.get("orderCreateTime") or "",
        )

        for sell in sells:
            ts_str      = sell.get("createTime") or sell.get("exchangeTime") or sell.get("orderCreateTime") or ""
            entry_time  = ts_str[11:19] if len(ts_str) >= 19 else ""
            entry_price = float(sell.get("tradedPrice") or sell.get("price") or 0)
            qty         = int(sell.get("tradedQuantity") or sell.get("quantity") or 0)
            opt_raw     = (sell.get("drvOptionType") or "").upper()
            opt_type    = "CE" if opt_raw in ("CALL", "CE") else "PE"
            if opt_raw not in ("CALL", "PUT", "CE", "PE"):
                sym_u = (sell.get("tradingSymbol") or sell.get("customSymbol") or "").upper()
                opt_type = "CE" if (sym_u.endswith("CE") or " CALL " in sym_u) else "PE"
            strike      = float(sell.get("drvStrikePrice") or sell.get("strikePrice") or 0)
            expiry      = str(sell.get("drvExpiryDate") or sell.get("expiryDate") or "")
            sym         = sell.get("tradingSymbol") or sell.get("customSymbol") or ""
            exseg       = sell.get("exchangeSegment") or "NSE_FNO"
            underlying  = _underlying(sym, exseg)
            lot_size    = LOT_SIZES.get(underlying, 1)
            lots        = round(qty / lot_size, 2) if lot_size else float(qty)
            order_id    = str(sell.get("orderId") or sell.get("order_id") or "")

            direction = "SHORT"
            exit_t = next(
                (
                    b for b in buys
                    if int(b.get("tradedQuantity") or b.get("quantity") or 0) == qty
                    and (b.get("createTime") or b.get("orderCreateTime") or "") > ts_str
                ),
                None,
            )
            # Fallback: if no BUY after this SELL, check for unpaired BUY before it.
            # Handles hedge legs that were bought first and sold to unwind.
            if exit_t is None:
                before = [
                    b for b in buys
                    if int(b.get("tradedQuantity") or b.get("quantity") or 0) == qty
                    and (b.get("createTime") or b.get("orderCreateTime") or "") < ts_str
                ]
                if before:
                    exit_t = max(before, key=lambda b: b.get("createTime") or b.get("orderCreateTime") or "")
                    direction = "LONG"
            if exit_t:
                buys.remove(exit_t)

            exit_ts    = (exit_t.get("createTime") or exit_t.get("exchangeTime") or "") if exit_t else ""
            exit_time  = exit_ts[11:19] if len(exit_ts) >= 19 else ""
            exit_price = float(exit_t.get("tradedPrice") or exit_t.get("price") or 0) if exit_t else None
            pnl        = round((entry_price - (exit_price or 0)) * qty, 2) if exit_price is not None else None
            status     = "CLOSED" if exit_t else "OPEN"
            # For LONG (hedge): exit_t is the opening BUY, sell is the closing SELL.
            # Swap so entry = opening BUY (earlier), exit = closing SELL (later).
            if direction == "LONG" and exit_t:
                entry_time, exit_time   = exit_time,  entry_time
                entry_price, exit_price = exit_price, entry_price
                pnl = round((exit_price - entry_price) * qty, 2) if exit_price is not None else None

            with _db_lock:
                existing = db.execute(
                    "SELECT id, status, direction FROM trades"
                    " WHERE date=? AND security_id=? AND entry_time=?",
                    (trade_date, sid, entry_time),
                ).fetchone()
                if not existing:
                    # fallback: match OPEN rows by trade identity when security_id differs
                    # (e.g. first import from CSV uses symbol as ID, second from Dhan uses numeric ID)
                    existing = db.execute(
                        "SELECT id, status, direction FROM trades"
                        " WHERE date=? AND underlying=? AND option_type=? AND strike=?"
                        " AND entry_time=? AND status='OPEN'",
                        (trade_date, underlying, opt_type, strike, entry_time),
                    ).fetchone()
                if existing:
                    updates = {}
                    if existing["status"] == "OPEN" and exit_t:
                        updates = dict(exit_time=exit_time, exit_price=exit_price,
                                       pnl=pnl, status="CLOSED", direction=direction)
                    elif (existing["direction"] or "SHORT") != direction:
                        updates = dict(direction=direction)
                    if updates:
                        cols = ", ".join(f"{k}=?" for k in updates)
                        db.execute(f"UPDATE trades SET {cols} WHERE id=?",
                                   (*updates.values(), existing["id"]))
                        db.commit()
                        imported += 1
                    else:
                        skipped += 1
                    continue

                db.execute(
                    """
                    INSERT INTO trades
                        (date, underlying, option_type, strike, expiry,
                         entry_time, entry_price, exit_time, exit_price,
                         quantity, lot_size, lots, pnl, status,
                         security_id, exchange_segment, dhan_order_id, created_at, direction)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        trade_date, underlying, opt_type, strike, expiry,
                        entry_time, entry_price, exit_time, exit_price,
                        qty, lot_size, lots, pnl, status,
                        sid, exseg, order_id, datetime.now().timestamp(), direction,
                    ),
                )
                db.commit()
                imported += 1

    return {
        "imported": imported,
        "skipped": skipped,
        "total_raw": len(raw),
        "total_options": len(opts),
        "diag": diag,
    }


# ── Dhan import ──────────────────────────────────────────────────────────────────


def _underlying(trading_symbol: str, exchange_segment: str) -> str:
    seg = (exchange_segment or "").upper()
    if "BSE" in seg:
        return "SENSEX"
    sym = (trading_symbol or "").upper().replace("-", "").replace(" ", "")
    for name in ("MIDCPNIFTY", "FINNIFTY", "NIFTY", "SENSEX"):
        if sym.startswith(name):
            return name
    prefix = "".join(ch for ch in sym if not ch.isdigit()).rstrip()
    return prefix or "NIFTY"


def _is_option(trade: dict) -> bool:
    seg = (trade.get("exchangeSegment") or "").upper()
    seg_ok = "FNO" in seg or "F&O" in seg or "FO" in seg
    opt = (trade.get("drvOptionType") or "").upper()
    opt_ok = opt in ("CALL", "PUT", "CE", "PE")
    inst = (trade.get("instrumentType") or trade.get("drvInstrumentType") or "").upper()
    inst_ok = inst in ("OPTIDX", "OPTSTK")
    sym = (trade.get("tradingSymbol") or trade.get("customSymbol") or "").upper()
    sym_ok = (sym.endswith("CE") or sym.endswith("PE") or
              sym.endswith("CALL") or sym.endswith("PUT") or
              " CALL " in sym or " PUT " in sym)
    return seg_ok and (opt_ok or inst_ok or sym_ok)


def _tx_type(trade: dict) -> str:
    t = (trade.get("transactionType") or "").upper().strip()
    if t in ("SELL", "S", "SHORT", "-1"):
        return "SELL"
    if t in ("BUY", "B", "LONG", "1"):
        return "BUY"
    return t


def _do_import(from_date: str, to_date: str) -> dict:
    dhan = _dhan_client()
    raw: list[dict] = []
    today_str = str(date.today())
    diag: dict = {}

    if from_date < today_str or to_date < today_str:
        page = 0
        tried_p1_fallback = False
        while True:
            resp = dhan.get_trade_history(
                from_date=_to_dhan_date(from_date),
                to_date=_to_dhan_date(to_date),
                page_number=page,
            )
            batch = _extract_batch(resp)
            logger.info("get_trade_history page=%d: %d records", page, len(batch))
            if not batch:
                if page == 0 and not tried_p1_fallback:
                    tried_p1_fallback = True
                    resp1 = dhan.get_trade_history(
                        from_date=_to_dhan_date(from_date),
                        to_date=_to_dhan_date(to_date),
                        page_number=1,
                    )
                    batch1 = _extract_batch(resp1)
                    if batch1:
                        raw.extend(batch1)
                        page = 2
                        continue
                    else:
                        if not _is_no_records_error(resp) and not _is_no_records_error(resp1):
                            diag["history_raw_p0"] = str(resp)[:400]
                        else:
                            diag["note"] = "No trades found on Dhan for this date range."
                break
            raw.extend(batch)
            if len(batch) < 50:
                break
            page += 1

    if from_date <= today_str <= to_date:
        try:
            resp_tb = dhan.get_trade_book()
            tb_batch = _extract_batch(resp_tb)
            logger.info("get_trade_book (today): %d records", len(tb_batch))
            if not tb_batch:
                diag["tradebook_raw"] = str(resp_tb)[:400]
                if _is_auth_error(resp_tb):
                    raise ValueError("Trade book auth error: invalid token")
            raw.extend(tb_batch)
        except Exception as e:
            logger.warning("get_trade_book failed: %s", e)
            diag["tradebook_error"] = str(e)
            if "auth error" in str(e).lower() or "invalid token" in str(e).lower():
                raise

    return _process_raw_trades(raw, diag)


def import_from_dhan(from_date: str, to_date: str) -> dict:
    try:
        return _do_import(from_date, to_date)
    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ("unauthorized", "401", "invalid token",
                                   "token expired", "authentication", "access denied")):
            logger.info("Auth error — refreshing token and retrying...")
            import token_manager  # noqa: PLC0415
            if token_manager.refresh_token():
                return _do_import(from_date, to_date)
        raise


# ── Chart data ──────────────────────────────────────────────────────────────────────


def _with_timeout(fn, *args, **kwargs):
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(CHART_TIMEOUT)
    try:
        return fn(*args, **kwargs)
    finally:
        socket.setdefaulttimeout(old)


def _is_auth_error(resp) -> bool:
    if not isinstance(resp, dict):
        return False
    status = (resp.get("status") or "").lower()
    if status not in ("failure", "error", "fail"):
        return False
    remarks = str(resp.get("remarks") or resp.get("message") or "").lower()
    return any(w in remarks for w in ("unauthorized", "token", "401", "auth", "access"))


def _raw_dhan_chart(security_id: str, exchange_segment: str,
                    instrument_type: str, day: str, from_date: str = None) -> tuple[dict, str]:
    """Fetch 1m candles for a day or date range.

    from_date: if given, fetches [from_date, day] range; otherwise single day.
    Tries two Dhan endpoints in order:
    1. /charts/historical with type=1 — true historical minute data (any past date)
    2. /charts/intraday — real-time feed, only last 5 trading days during market hours
    """
    fd = from_date or day
    try:
        dhan = _dhan_client()
    except Exception as e:
        return {}, str(e)

    def _candle_count(resp):
        if not isinstance(resp, dict) or resp.get("status") != "success":
            return 0
        data = resp.get("data", resp)
        if not isinstance(data, dict):
            return 0
        return len(data.get("timestamp") or data.get("timestamps") or [])

    # Approach 1: historical minute data (type="1") — works for any past date
    try:
        resp = _with_timeout(
            dhan.dhan_http.post,
            "/charts/historical",
            {
                "securityId":      security_id,
                "exchangeSegment": exchange_segment,
                "instrument":      instrument_type,
                "expiryCode":      0,
                "fromDate":        fd,
                "toDate":          day,
                "type":            "1",
            },
        )
        logger.info("Hist-1m raw [%s %s]: %s", security_id, exchange_segment, str(resp)[:300])
        if _candle_count(resp) > 0:
            logger.info("Dhan hist-1m [%s %s %s]: %d candles", security_id, exchange_segment, day, _candle_count(resp))
            return resp, ""
    except Exception as e:
        logger.warning("Historical minute API error: %s", e)

    # Approach 2: intraday feed (last 5 trading days, works during/after market hours)
    try:
        resp = _with_timeout(
            dhan.intraday_minute_data,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=f"{fd} 09:00:00",
            to_date=f"{day} 15:30:00",
        )
        logger.info("Intraday raw [%s %s]: %s", security_id, exchange_segment, str(resp)[:300])
        if _is_auth_error(resp):
            logger.info("Chart auth error — refreshing token and retrying")
            try:
                import token_manager  # noqa: PLC0415
                if token_manager.refresh_token():
                    dhan = _dhan_client()
                    resp = _with_timeout(
                        dhan.intraday_minute_data,
                        security_id=security_id,
                        exchange_segment=exchange_segment,
                        instrument_type=instrument_type,
                        from_date=f"{fd} 09:00:00",
                        to_date=f"{day} 15:30:00",
                    )
            except Exception as e:
                logger.warning("Token refresh failed during chart load: %s", e)
        return resp, ""
    except Exception as e:
        return {}, str(e)


def _parse_dhan_candles(resp, trade_date: str) -> list[dict]:
    if not resp:
        return []
    data = resp
    if isinstance(resp, dict) and "data" in resp:
        data = resp["data"]
    if not isinstance(data, dict):
        logger.warning("Unexpected chart response type=%s  preview=%s",
                       type(data).__name__, str(data)[:200])
        return []
    timestamps = (data.get("timestamp") or data.get("timestamps")
                  or data.get("time") or [])
    opens  = data.get("open")  or data.get("openPrice")  or []
    highs  = data.get("high")  or data.get("highPrice")  or []
    lows   = data.get("low")   or data.get("lowPrice")   or []
    closes = data.get("close") or data.get("closePrice") or []
    if not timestamps:
        logger.info("Chart response has no timestamps. data keys: %s", list(data.keys()))
        return []
    logger.info("First timestamps raw (type=%s): %s", type(timestamps[0]).__name__, timestamps[:3])
    candles = []
    for i, ts_raw in enumerate(timestamps):
        try:
            # Handle integer Unix epoch (Dhan intraday returns actual UTC)
            # Add 19800s (5.5h IST offset) to match the IST-as-UTC convention
            # used by tsFor() in the frontend (Date.UTC treating IST times as UTC)
            if isinstance(ts_raw, (int, float)):
                ts = datetime.fromtimestamp(int(ts_raw) + 19800)
            else:
                ts_str = str(ts_raw).strip()
                if len(ts_str) <= 8:
                    ts_str = f"{trade_date} {ts_str}"
                ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
            candles.append({
                "time":  int(ts.timestamp()),
                "open":  round(float(opens[i]),  2),
                "high":  round(float(highs[i]),  2),
                "low":   round(float(lows[i]),   2),
                "close": round(float(closes[i]), 2),
            })
        except (IndexError, ValueError, TypeError):
            continue
    return candles



def _fetch_day_candles(idx: dict, day: str) -> list[dict]:
    """Fetch candles for a single day using the given index config."""
    raw_resp, dhan_err = _raw_dhan_chart(
        idx["security_id"], idx["exchange_segment"], idx["instrument_type"], day
    )
    if dhan_err:
        logger.error("Dhan chart error [%s %s %s]: %s",
                     idx["security_id"], idx["exchange_segment"], day, dhan_err)
        return []
    return _parse_dhan_candles(raw_resp, day)


def _fetch_warmup_candles(idx: dict, from_day: str, to_day: str) -> tuple[list[dict], str]:
    """Fetch up to 3 previous trading days of 1-minute warmup candles.

    Uses a single batch call to /charts/intraday for the full warmup date range —
    one API call, no rate-limit loops, correct 1-minute candle granularity.

    /charts/historical is intentionally NOT used here: that endpoint returns
    daily candles (one per day), not 1-minute candles, which breaks indicators.

    Falls back to per-day intraday calls (with inter-call sleep) if batch fails.
    """
    # Primary: single batch intraday call for the full warmup range
    batch: list[dict] = []
    try:
        dhan = _dhan_client()
        resp = _with_timeout(
            dhan.intraday_minute_data,
            security_id=idx["security_id"],
            exchange_segment=idx["exchange_segment"],
            instrument_type=idx["instrument_type"],
            from_date=f"{from_day} 09:00:00",
            to_date=f"{to_day} 15:30:00",
        )
        logger.info("Warmup batch [%s %s→%s]: %s",
                    idx["security_id"], from_day, to_day, str(resp)[:150])
        if _is_auth_error(resp):
            try:
                import token_manager  # noqa: PLC0415
                if token_manager.refresh_token():
                    dhan = _dhan_client()
                    resp = _with_timeout(
                        dhan.intraday_minute_data,
                        security_id=idx["security_id"],
                        exchange_segment=idx["exchange_segment"],
                        instrument_type=idx["instrument_type"],
                        from_date=f"{from_day} 09:00:00",
                        to_date=f"{to_day} 15:30:00",
                    )
            except Exception as e:
                logger.warning("Token refresh failed during warmup: %s", e)
        batch = _parse_dhan_candles(resp, to_day)
    except Exception as e:
        logger.warning("Warmup batch error: %s", e)

    if batch:
        # Group parsed candles by trading date, keep 3 most recent days.
        # Use utcfromtimestamp: candle["time"] is IST-as-UTC epoch so UTC
        # datetime == IST clock time, giving the correct trading date.
        day_groups: dict[str, list] = {}
        for c in batch:
            dt = datetime.utcfromtimestamp(c["time"]).strftime("%Y-%m-%d")
            day_groups.setdefault(dt, []).append(c)
        trading_days = sorted(day_groups.keys())
        keep_days = trading_days[-3:]
        result: list[dict] = []
        for d in keep_days:
            result.extend(day_groups[d])
        summary = " | ".join(f"{d}: {len(day_groups[d])} candles" for d in keep_days)
        logger.info("Warmup batch OK: %d days, %d candles", len(keep_days), len(result))
        return result, summary

    # Fallback: individual per-day calls with inter-call pause to avoid rate-limiting
    logger.info("Warmup batch returned 0 candles — falling back to per-day calls")
    current   = datetime.strptime(to_day, "%Y-%m-%d")
    cutoff    = datetime.strptime(from_day, "%Y-%m-%d")
    all_candles: list[dict] = []
    days_found = 0
    log_parts: list[str] = []
    first_call = True
    while current >= cutoff and days_found < 3:
        day_str = current.strftime("%Y-%m-%d")
        if not first_call:
            time.sleep(0.3)
        first_call = False
        candles = _fetch_day_candles(idx, day_str)
        if candles:
            all_candles = candles + all_candles
            days_found += 1
            log_parts.append(f"{day_str}: {len(candles)} candles")
            logger.info("Warmup fallback day %d: %d candles from %s",
                        days_found, len(candles), day_str)
        else:
            log_parts.append(f"{day_str}: no data")
        current -= timedelta(days=1)
    if not all_candles:
        logger.warning("Warmup: no data found between %s and %s", from_day, to_day)
    return all_candles, " | ".join(log_parts) if log_parts else "no warmup data"


def chart_candles(underlying: str, trade_date: str) -> tuple[list[dict], str, str, str]:
    u = underlying.upper()
    # Look back 10 calendar days for warmup — guarantees 3 trading days even across
    # long weekends and multi-day holidays (e.g. Diwali). Weekends + 1 holiday = 3 skip days;
    # a 4-day weekend = 4 skip days → need 3*1 + 4 = 7+ calendar days. 10 is safe.
    warmup_from = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    warmup_to   = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    # Build candidate list with per-underlying fallbacks
    if u == "SENSEX":
        candidates = SENSEX_FALLBACKS[:]
    elif u == "NIFTY":
        candidates = NIFTY_FALLBACKS[:]
    else:
        candidates = []
    primary = DHAN_INDEX_IDS.get(u)
    if primary and (not candidates or candidates[0] != primary):
        candidates = [primary] + [c for c in candidates if c != primary]

    if not candidates:
        return [], "1m", f"No index config for {u}", ""

    # Try each candidate until we get data for trade_date
    working_idx = None
    trade_candles: list[dict] = []
    for idx in candidates:
        trade_candles = _fetch_day_candles(idx, trade_date)
        if trade_candles:
            working_idx = idx
            if idx != DHAN_INDEX_IDS.get(u):
                logger.info("%s working config: sid=%s seg=%s — update DHAN_INDEX_IDS",
                            u, idx["security_id"], idx["exchange_segment"])
                DHAN_INDEX_IDS[u] = idx
            break

    if not trade_candles:
        return [], "1m", (
            f"No 1m chart data for {u} {trade_date} from Dhan. "
            "Check /api/debug-chart?underlying=" + u + "&date=" + trade_date
        ), ""

    # Fetch up to 3 previous trading days as warmup so RSI/MACD are converged
    # by the time the trading-day candles start (~1125 bars vs ~375 for 1 day).
    prev_candles, warmup_log = _fetch_warmup_candles(working_idx, warmup_from, warmup_to)

    all_candles = sorted(prev_candles + trade_candles, key=lambda c: c["time"])
    return all_candles, "1m", "", warmup_log


def _make_test_candles(trade_date: str) -> list[dict]:
    dt = datetime.strptime(trade_date, "%Y-%m-%d")
    base_time = datetime(dt.year, dt.month, dt.day, 9, 15, 0)
    price = 24500.0
    rng = random.Random(42)
    candles = []
    for i in range(375):
        noise = rng.gauss(0, 12)
        wave  = math.sin(i / 40.0) * 40
        o = round(price + wave + noise, 2)
        c = round(o + rng.gauss(0, 8), 2)
        h = round(max(o, c) + abs(rng.gauss(0, 6)), 2)
        l = round(min(o, c) - abs(rng.gauss(0, 6)), 2)
        t = int(base_time.timestamp()) + i * 60
        candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
        price = c
    return candles


# ── Flask routes ──────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return _page()


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "version": APP_VERSION})


@app.route("/api/test-chart")
def api_test_chart():
    d = request.args.get("date") or str(date.today())
    candles = _make_test_candles(d)
    return jsonify({"candles": candles, "interval": "1m", "error": ""})


@app.route("/api/debug-chart")
def api_debug_chart():
    u   = (request.args.get("underlying") or "NIFTY").upper()
    d   = request.args.get("date") or str(date.today())
    idx = DHAN_INDEX_IDS.get(u)
    if not idx:
        return jsonify({"ok": False, "error": f"No index config for {u}"})
    try:
        raw_resp, dhan_err = _raw_dhan_chart(
            idx["security_id"], idx["exchange_segment"], idx["instrument_type"], day=d,
        )
        candles = _parse_dhan_candles(raw_resp, d) if not dhan_err else []
        inner   = raw_resp.get("data", raw_resp) if isinstance(raw_resp, dict) else raw_resp
        return jsonify({
            "ok":           True,
            "dhan_error":   dhan_err,
            "candle_count": len(candles),
            "resp_type":    type(raw_resp).__name__,
            "resp_keys":    list(raw_resp.keys()) if isinstance(raw_resp, dict) else None,
            "data_keys":    list(inner.keys()) if isinstance(inner, dict) else None,
            "raw_preview":  str(raw_resp)[:800],
            "first_candle": candles[0] if candles else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "type": type(e).__name__})


@app.route("/api/debug-dhan")
def api_debug_dhan():
    from_date = request.args.get("from_date") or str(date.today())
    to_date   = request.args.get("to_date")   or str(date.today())
    today_str = str(date.today())
    out: dict = {"ok": True}
    try:
        dhan  = _dhan_client()
        resp  = dhan.get_trade_history(
            from_date=_to_dhan_date(from_date),
            to_date=_to_dhan_date(to_date),
            page_number=0,
        )
        batch = _extract_batch(resp)
        out["history"] = {
            "response_type":     type(resp).__name__,
            "response_keys":     list(resp.keys()) if isinstance(resp, dict) else None,
            "record_count":      len(batch),
            "first_record":      batch[0] if batch else None,
            "first_record_keys": list(batch[0].keys()) if batch else None,
            "raw_preview":       str(resp)[:500],
        }
        if from_date <= today_str <= to_date:
            try:
                resp_tb  = dhan.get_trade_book()
                tb_batch = _extract_batch(resp_tb)
                out["trade_book"] = {
                    "response_type": type(resp_tb).__name__,
                    "record_count":  len(tb_batch),
                    "first_record":  tb_batch[0] if tb_batch else None,
                    "raw_preview":   str(resp_tb)[:500],
                }
            except Exception as e:
                out["trade_book"] = {"error": str(e)}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "error_type": type(e).__name__})
    return jsonify(out)


@app.route("/api/import", methods=["POST"])
def api_import():
    data = request.json or {}
    try:
        result = import_from_dhan(
            data.get("from_date") or str(date.today()),
            data.get("to_date")   or str(date.today()),
        )
        return jsonify({"ok": True, **result})
    except Exception as e:
        logger.error("import: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/import-csv", methods=["POST"])
def api_import_csv():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    try:
        content = f.read().decode("utf-8-sig")  # strip BOM if present
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not read file: {e}"}), 400
    raw, parse_err = _parse_csv_trades(content)
    if parse_err:
        return jsonify({"ok": False, "error": parse_err}), 400
    try:
        result = _process_raw_trades(raw)
        return jsonify({"ok": True, **result})
    except Exception as e:
        logger.error("import-csv: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/refresh-token", methods=["POST"])
def api_refresh_token():
    import token_manager  # noqa: PLC0415
    success = token_manager.refresh_token()
    return jsonify({"ok": success,
                    "message": "Token refreshed" if success else "Token refresh failed"})


@app.route("/api/trades")
def api_trades():
    d  = request.args.get("date") or str(date.today())
    u  = (request.args.get("underlying") or "").upper()
    ot = (request.args.get("option_type") or "").upper()
    db = get_db()
    q = (
        "SELECT t.*, COALESCE(n.notes, t.notes, '') AS notes"
        " FROM trades t"
        " LEFT JOIN trade_notes n"
        "   ON n.date=t.date AND n.underlying=t.underlying"
        "   AND n.option_type=t.option_type AND n.strike=t.strike AND n.entry_time=t.entry_time"
        " WHERE t.date=?"
    )
    p = [d]
    if u and u != "ALL":
        q += " AND t.underlying=?"; p.append(u)
    if ot and ot not in ("ALL", "BOTH", ""):
        q += " AND t.option_type=?"; p.append(ot)
    return jsonify([dict(r) for r in db.execute(q + " ORDER BY t.entry_time", p).fetchall()])


@app.route("/api/trade/<int:tid>", methods=["DELETE"])
def api_delete_trade(tid: int):
    with _db_lock:
        db = get_db()
        db.execute("DELETE FROM trades WHERE id=?", (tid,))
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/trades/date/<trade_date>", methods=["DELETE"])
def api_delete_date(trade_date: str):
    with _db_lock:
        db = get_db()
        db.execute("DELETE FROM trades WHERE date=?", (trade_date,))
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/trade/<int:tid>/close", methods=["PUT"])
def api_close_trade(tid: int):
    data = request.json or {}
    exit_price = float(data.get("exit_price", 0))
    exit_time  = str(data.get("exit_time") or "15:30:00")
    with _db_lock:
        db  = get_db()
        row = db.execute("SELECT entry_price, quantity FROM trades WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Not found"}), 404
        pnl = round((row["entry_price"] - exit_price) * row["quantity"], 2)
        db.execute(
            "UPDATE trades SET exit_price=?, exit_time=?, pnl=?, status='CLOSED' WHERE id=?",
            (exit_price, exit_time, pnl, tid),
        )
        db.commit()
    return jsonify({"ok": True, "pnl": pnl})


@app.route("/api/trade/<int:tid>/notes", methods=["PUT"])
def api_notes(tid: int):
    notes = (request.json or {}).get("notes", "")
    with _db_lock:
        db = get_db()
        db.execute("UPDATE trades SET notes=? WHERE id=?", (notes, tid))
        row = db.execute(
            "SELECT date, underlying, option_type, strike, entry_time FROM trades WHERE id=?", (tid,)
        ).fetchone()
        if row:
            db.execute(
                "INSERT INTO trade_notes (date, underlying, option_type, strike, entry_time, notes, updated_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(date, underlying, option_type, strike, entry_time)"
                " DO UPDATE SET notes=excluded.notes, updated_at=excluded.updated_at",
                (row["date"], row["underlying"], row["option_type"],
                 row["strike"], row["entry_time"], notes, datetime.now().timestamp()),
            )
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/chart")
def api_chart():
    u = request.args.get("underlying") or "NIFTY"
    d = request.args.get("date")       or str(date.today())
    candles, interval, err, warmup_log = chart_candles(u, d)
    return jsonify({"candles": candles, "interval": interval, "error": err, "warmup_log": warmup_log})


@app.route("/api/dates")
def api_dates():
    rows = get_db().execute(
        "SELECT DISTINCT date FROM trades ORDER BY date DESC LIMIT 90"
    ).fetchall()
    return jsonify([r["date"] for r in rows])


# ── HTML page ─────────────────────────────────────────────────────────────────────────────────


def _page() -> str:
    ver = APP_VERSION
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Trade Analyser {ver}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0 }}
:root {{
  --bg: #0d1117; --surface: #161b22; --s2: #21262d; --border: #30363d;
  --text: #e0e0e0; --dim: #8b949e; --ce: #4fc3f7; --pe: #ffb74d;
  --green: #3fb950; --red: #f85149; --acc: #7c4dff;
}}
html, body {{ height: 100%; overflow: hidden }}
body {{ display: flex; flex-direction: column; background: var(--bg); color: var(--text);
       font: 13px/1.4 'Trebuchet MS', Roboto, Ubuntu, sans-serif }}
#bar {{ display: flex; align-items: center; gap: 10px; padding: 8px 14px;
       background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0;
       flex-wrap: wrap }}
#bar h1 {{ font-size: 13px; font-weight: 700; letter-spacing: 1.5px; margin-right: 4px; color: var(--acc) }}
.date-nav {{ display: flex; gap: 4px; align-items: center }}
.date-nav button {{ background: var(--s2); border: 1px solid var(--border); color: var(--text);
                   padding: 3px 9px; cursor: pointer; border-radius: 3px; font: inherit }}
.date-nav button:hover {{ border-color: var(--acc) }}
#dp {{ background: var(--s2); border: 1px solid var(--border); color: var(--text);
      padding: 3px 8px; border-radius: 3px; font: inherit; cursor: pointer }}
.chips {{ display: flex; gap: 4px }}
.chip {{ padding: 3px 11px; border-radius: 10px; border: 1px solid var(--border);
        color: var(--dim); cursor: pointer; font-size: 11px; user-select: none; transition: .15s }}
.chip.on {{ color: var(--text); border-color: var(--acc); background: rgba(124,77,255,.12) }}
.chip.ce.on {{ color: var(--ce); border-color: var(--ce); background: rgba(79,195,247,.1) }}
.chip.pe.on {{ color: var(--pe); border-color: var(--pe); background: rgba(255,183,77,.1) }}
#ivl {{ font-size: 10px; color: var(--dim); border: 1px solid var(--border); padding: 2px 6px; border-radius: 3px }}
#impBtn {{ margin-left: auto; background: var(--acc); border: none; color: #fff;
          padding: 5px 14px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 12px }}
#impBtn:hover {{ opacity: .85 }}
.hbtn {{ background: var(--s2); border: 1px solid var(--border); color: var(--dim);
        padding: 5px 10px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 11px }}
.hbtn:hover {{ border-color: var(--acc); color: var(--text) }}
.vbadge {{ font-size: 9px; color: #444; border: 1px solid #2a2a2a; padding: 1px 5px;
           border-radius: 2px; letter-spacing: 1px }}
#main {{ flex: 1; display: flex; flex-direction: column; min-height: 0 }}
#chartsArea {{ flex: 1; display: flex; flex-direction: column; min-height: 0; position: relative }}
#chartBox {{ flex: 1; min-height: 220px; position: relative; overflow: hidden }}
#chartEl {{ position: absolute; inset: 0 }}
#chartMsg {{ position: absolute; inset: 0; display: flex; align-items: center;
            justify-content: center; color: var(--dim); font-size: 12px;
            background: var(--bg); pointer-events: none; flex-direction: column; gap: 8px }}
#chartMsg.hide {{ display: none }}
#chartMsgSub {{ font-size: 10px; color: #333; max-width: 600px; text-align: center;
               word-break: break-word; padding: 0 16px }}
.pane-btn {{ position:absolute; right:70px; z-index:10; background:rgba(20,20,20,0.75);
  border:1px solid #333; color:#666; font-size:10px; padding:1px 5px; border-radius:3px;
  cursor:pointer; user-select:none; line-height:1.6; }}
.pane-btn:hover {{ color:#ccc; border-color:#555; }}
.ind-label {{ position: absolute; top: 5px; left: 10px; font-size: 10px; color: #484f58;
              pointer-events: none; z-index: 1; letter-spacing: 0.3px; font-weight: 500 }}
#panel {{ height: 185px; border-top: 1px solid var(--border);
         display: flex; flex-direction: column; background: var(--surface) }}
#ph {{ display: flex; align-items: center; gap: 12px; padding: 6px 14px;
      border-bottom: 1px solid var(--border); flex-shrink: 0 }}
#ph b {{ color: var(--dim); font-size: 10px; letter-spacing: 1px }}
#psummary {{ margin-left: auto; font-size: 11px; color: var(--dim) }}
#pbody {{ flex: 1; overflow-y: auto }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px }}
th {{ position: sticky; top: 0; background: var(--s2); color: var(--dim); font-weight: 500;
     padding: 5px 12px; text-align: left; border-bottom: 1px solid var(--border) }}
td {{ padding: 5px 12px; border-bottom: 1px solid rgba(255,255,255,.035) }}
tr.sel td {{ background: rgba(124,77,255,.08) }}
tr:not(.sel):hover td {{ background: rgba(255,255,255,.02) }}
.tag {{ display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 10px; font-weight: 700 }}
.tag.ce {{ background: rgba(79,195,247,.12); color: var(--ce) }}
.tag.pe {{ background: rgba(255,183,77,.12); color: var(--pe) }}
.pos {{ color: var(--green) }} .neg {{ color: var(--red) }}
.ni {{ background: none; border: none; color: var(--text); font: inherit; width: 100%; outline: none }}
.ni:focus {{ border-bottom: 1px solid var(--acc) }}
.ni::placeholder {{ color: #333 }}
.delbtn {{ background: none; border: none; color: #333; cursor: pointer; font-size: 14px; padding: 0 2px; line-height: 1 }}
.delbtn:hover {{ color: var(--red) }}
#empty {{ text-align: center; color: var(--dim); padding: 36px; font-size: 12px }}
#ov {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
      z-index: 99; align-items: center; justify-content: center }}
#ov.show {{ display: flex }}
#modal {{ background: var(--surface); border: 1px solid var(--border);
         border-radius: 8px; padding: 24px; width: 380px }}
#modal h2 {{ font-size: 14px; margin-bottom: 16px }}
.fr label {{ display: block; font-size: 11px; color: var(--dim); margin-bottom: 3px }}
.fr input[type=date] {{ width: 100%; background: var(--s2); border: 1px solid var(--border);
                       color: var(--text); padding: 6px 9px; border-radius: 4px;
                       font: inherit; margin-bottom: 12px }}
.mfooter {{ display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px }}
.mtabs {{ display:flex; gap:4px; margin-bottom:14px }}
.mtab {{ background:var(--s2); border:1px solid var(--border); color:var(--dim); padding:4px 12px; border-radius:3px; cursor:pointer; font:inherit; font-size:12px }}
.mtab.on {{ color:var(--text); border-color:var(--acc); background:rgba(124,77,255,.12) }}
input[type=file] {{ width:100%; background:var(--s2); border:1px solid var(--border); color:var(--text); padding:6px 9px; border-radius:4px; font:inherit; margin-bottom:8px; cursor:pointer }}
.btn  {{ padding: 6px 16px; border-radius: 4px; border: none; cursor: pointer; font: inherit; font-size: 12px }}
.btnp {{ background: var(--acc); color: #fff }}
.btns {{ background: var(--s2); color: var(--text); border: 1px solid var(--border) }}
#mres {{ margin-top: 10px; font-size: 12px; min-height: 18px; word-break: break-word }}
#mdiag {{ margin-top: 8px; font-size: 10px; color: var(--dim); max-height: 120px;
         overflow-y: auto; background: var(--s2); border-radius: 4px; padding: 6px 8px;
         white-space: pre-wrap; display: none }}
::-webkit-scrollbar {{ width: 5px; height: 5px }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px }}
</style>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.0/dist/lightweight-charts.standalone.production.js"></script>
</head>
<body>
<div id="bar">
  <h1>TRADE ANALYSER</h1>
  <span class="vbadge">{ver}</span>
  <span id="jss" class="vbadge" style="color:#555">JS?</span>
  <div class="date-nav">
    <button onclick="shiftDay(-1)">&#8592;</button>
    <input type="date" id="dp" onchange="onDate()">
    <button onclick="shiftDay(1)">&#8594;</button>
  </div>
  <div class="chips" id="uChips">
    <div class="chip on" data-v="NIFTY"  onclick="setU(this)">NIFTY</div>
    <div class="chip"    data-v="SENSEX" onclick="setU(this)">SENSEX</div>
  </div>
  <div class="chips" id="tChips">
    <div class="chip ce on" data-v="CE" onclick="togT(this)">CE</div>
    <div class="chip pe on" data-v="PE" onclick="togT(this)">PE</div>
  </div>
  <div class="chips" id="dChips">
    <div class="chip on" data-v="SHORT" onclick="togD(this)">Short</div>
    <div class="chip on" data-v="LONG"  onclick="togD(this)">Long</div>
  </div>
  <span id="ivl">&#8212;</span>
  <button class="hbtn" onclick="doRefreshToken()">&#8635; Token</button>
  <button id="impBtn" onclick="openImp()">&#8595; Import from Dhan</button>
</div>
<div id="main">
  <div id="chartsArea">
    <div id="chartBox">
      <div id="chartEl"></div>
      <div id="chartLegend" style="position:absolute;top:22px;left:10px;font-size:12px;font-weight:500;color:#C3BCDB;pointer-events:none;z-index:2;white-space:nowrap"></div>
      <div id="chartMsg">
        <span id="chartMsgMain">Initialising chart&#8230;</span>
        <span id="chartMsgSub"></span>
      </div>
      <div class="ind-label">EMA&thinsp;<span style="color:#2196F3">20</span>&ensp;<span style="color:#FF9800">50</span></div>
      <div id="paneBtn0" class="pane-btn" style="top:4px"    onclick="togglePaneExpand(0)">&#x26F6;</div>
      <div id="paneBtn1" class="pane-btn" style="top:67.5%"  onclick="togglePaneExpand(1)">&#x26F6;</div>
      <div id="paneBtn2" class="pane-btn" style="top:83.7%"  onclick="togglePaneExpand(2)">&#x26F6;</div>
    </div>
  </div>
  <div id="panel">
    <div id="ph">
      <b>TRADES</b>
      <span id="pcnt" style="font-size:11px;color:var(--dim)"></span>
      <span id="psummary"></span>
      <button class="hbtn" style="margin-left:auto;font-size:10px" onclick="wipeDate()" title="Delete all trades for this date and reimport">&#128465; Wipe &amp; reimport</button>
    </div>
    <div id="pbody">
      <div id="empty">No trades for this date &#8212; import from Dhan or pick another day.</div>
      <table id="tbl" style="display:none">
        <thead><tr>
          <th>Time</th><th>Type</th><th>Strike</th>
          <th>Entry &#8377;</th><th>Exit &#8377;</th><th>Lots</th><th>P&amp;L</th><th>Notes</th><th></th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </div>
</div>
<div id="ov" onclick="if(event.target===this)closeImp()">
  <div id="modal">
    <h2>Import Trades</h2>
    <div class="mtabs">
      <button class="mtab on" id="mtab-api" onclick="switchTab('api')">Dhan API</button>
      <button class="mtab" id="mtab-csv" onclick="switchTab('csv')">CSV File</button>
    </div>
    <div id="mpanel-api">
      <div class="fr">
        <label>From Date</label>
        <input type="date" id="mFrom">
        <label>To Date</label>
        <input type="date" id="mTo">
      </div>
      <div id="mres"></div>
      <div id="mdiag"></div>
      <div class="mfooter">
        <button class="btn btns" onclick="closeImp()">Cancel</button>
        <button class="btn btnp" id="mBtn" onclick="doImport()">Import</button>
      </div>
    </div>
    <div id="mpanel-csv" style="display:none">
      <div class="fr">
        <label>Trade History CSV (Dhan export)</label>
        <input type="file" id="mCsvFile" accept=".csv">
      </div>
      <div style="font-size:10px;color:var(--dim);margin-bottom:12px">Dhan app &#8594; Trade History &#8594; Download CSV</div>
      <div id="mres2" style="font-size:12px;min-height:18px;word-break:break-word"></div>
      <div id="mdiag2" style="margin-top:8px;font-size:10px;color:var(--dim);max-height:120px;overflow-y:auto;background:var(--s2);border-radius:4px;padding:6px 8px;white-space:pre-wrap;display:none"></div>
      <div class="mfooter">
        <button class="btn btns" onclick="closeImp()">Cancel</button>
        <button class="btn btnp" id="mBtn2" onclick="doImportCsv()">Upload &amp; Import</button>
      </div>
    </div>
  </div>
</div>

<!-- Error catcher: must be a separate script block before the main one -->
<script>
window.onerror = function(msg, src, line, col, err) {{
  var d = document.createElement('div');
  d.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#b71c1c;color:#fff;' +
    'font:11px monospace;padding:8px 12px;z-index:9999;white-space:pre-wrap;word-break:break-all;';
  d.textContent = 'JS ERROR (line ' + line + '): ' + msg +
    (err && err.stack ? '\\n' + err.stack.slice(0, 400) : '');
  document.body.appendChild(d);
  return false;
}};
window.addEventListener('unhandledrejection', function(e) {{
  window.onerror('Unhandled rejection: ' + String(e.reason), '', 0, 0, e.reason);
}});
</script>

<!-- Main app -->
<script>
(function(){{ var e=document.getElementById('jss'); if(e){{ e.textContent='JS OK'; e.style.color='#4caf50'; }} }})();

var _chartInst=null;
var chart=null, series=null;
var _ema20s=null, _ema50s=null, _rsiSeries=null;
var _macdHist=null, _macdLine=null, _macdSignal=null;
var _markersPlugin=null;
var _PANE_FACTORS=[5,1.2,1.2], _expandedPane=-1;
var _candleMap={{}}, _rsiMap={{}}, _macdMap={{}};
var curDate='', curU='NIFTY';
var typeOn=new Set(['CE','PE']);
var dirOn=new Set(['SHORT','LONG']);
var allTrades=[], candles=[], curInterval='1m';
var selId=null, isolateId=null;
var _savedNotes=[];

function setChartMsg(main,sub) {{
  document.getElementById('chartMsg').classList.remove('hide');
  document.getElementById('chartMsgMain').textContent=main||'';
  document.getElementById('chartMsgSub').textContent=sub||'';
}}
function hideChartMsg() {{ document.getElementById('chartMsg').classList.add('hide'); }}

function _chartOpts(el) {{
  return {{
    width:  el.clientWidth  || 800,
    height: el.clientHeight || 400,
    layout: {{ background: {{ color: '#0d1117' }}, textColor: '#8b949e' }},
    grid:   {{ vertLines: {{ color: '#21262d' }}, horzLines: {{ color: '#21262d' }} }},
    crosshair: {{ mode: 0 }},
    rightPriceScale: {{ borderColor: '#30363d' }},
    timeScale: {{ borderColor: '#30363d', timeVisible: true, secondsVisible: false }},
  }};
}}
function _watchResize(inst, el) {{
  new ResizeObserver(function() {{
    var sz = el.getBoundingClientRect();
    if (sz.width > 0 && sz.height > 0) inst.resize(sz.width, sz.height);
  }}).observe(el);
}}

function initChart() {{
  try {{
    var el = document.getElementById('chartEl');
    _chartInst = LightweightCharts.createChart(el, _chartOpts(el));

    // ── Pane 0: candlestick + EMA ──────────────────────────────────────────
    series  = _chartInst.addSeries(LightweightCharts.CandlestickSeries, {{
      upColor:'#3fb950', downColor:'#f85149',
      borderUpColor:'#3fb950', borderDownColor:'#f85149',
      wickUpColor:'#3fb950', wickDownColor:'#f85149'
    }});
    // v5: setMarkers() removed from series — use the createSeriesMarkers plugin instead
    _markersPlugin = LightweightCharts.createSeriesMarkers(series, []);
    _ema20s = _chartInst.addSeries(LightweightCharts.LineSeries, {{
      color:'#2196F3', lineWidth:1, lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false
    }});
    _ema50s = _chartInst.addSeries(LightweightCharts.LineSeries, {{
      color:'#FF9800', lineWidth:1, lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false
    }});

    // ── Pane 1: RSI ────────────────────────────────────────────────────────
    _rsiSeries = _chartInst.addSeries(LightweightCharts.LineSeries, {{
      color:'#58a6ff', lineWidth:1, lastValueVisible:true, priceLineVisible:false
    }}, 1);
    _rsiSeries.createPriceLine({{ price:70, color:'#30363d', lineWidth:1, lineStyle:1, axisLabelVisible:false }});
    _rsiSeries.createPriceLine({{ price:30, color:'#30363d', lineWidth:1, lineStyle:1, axisLabelVisible:false }});

    // ── Pane 2: MACD ───────────────────────────────────────────────────────
    _macdHist   = _chartInst.addSeries(LightweightCharts.HistogramSeries, {{
      color:'#555', lastValueVisible:false, priceLineVisible:false
    }}, 2);
    _macdLine   = _chartInst.addSeries(LightweightCharts.LineSeries, {{
      color:'#2196F3', lineWidth:1, lastValueVisible:false, priceLineVisible:false
    }}, 2);
    _macdSignal = _chartInst.addSeries(LightweightCharts.LineSeries, {{
      color:'#FF5722', lineWidth:1, lastValueVisible:false, priceLineVisible:false
    }}, 2);

    // Pane proportions: main=5, RSI=1.2, MACD=1.2  (setStretchFactor available in v5.2+)
    var _initPanes = _chartInst.panes();
    if (_initPanes[0]) _initPanes[0].setStretchFactor(_PANE_FACTORS[0]);
    if (_initPanes[1]) _initPanes[1].setStretchFactor(_PANE_FACTORS[1]);
    if (_initPanes[2]) _initPanes[2].setStretchFactor(_PANE_FACTORS[2]);

    // Pane buttons (HTML overlays) call togglePaneExpand(n) directly — no canvas event needed.

    chart = {{ timeScale: function() {{ return _chartInst.timeScale(); }} }};
    _watchResize(_chartInst, el);

    // OHLC legend
    var _leg = document.getElementById('chartLegend');
    _chartInst.subscribeCrosshairMove(function(param) {{
      if (!param.time || !param.seriesData || !param.seriesData.get(series)) {{
        _leg.innerHTML = ''; return;
      }}
      var b = param.seriesData.get(series);
      var hh = new Date(param.time * 1000).toISOString().slice(11, 16);
      var _clr = b.close >= b.open ? '#3fb950' : '#f85149';
      _leg.innerHTML = '<span style="color:#8b949e">' + hh + '</span>'
        + '&ensp;O<span style="color:#adbac7">' + b.open.toFixed(0) + '</span>'
        + '&thinsp;H<span style="color:#3fb950">' + b.high.toFixed(0) + '</span>'
        + '&thinsp;L<span style="color:#f85149">' + b.low.toFixed(0) + '</span>'
        + '&thinsp;C<span style="color:' + _clr + '">' + b.close.toFixed(0) + '</span>';
    }});

    setChartMsg('Select a date to load chart data', '');
  }} catch(e) {{
    setChartMsg('Chart init error: ' + e.message, e.stack || '');
  }}
}}

// ─── Indicator math ───────────────────────────────────────────────────────────
function _emaArr(closes,p){{
  var out=new Array(closes.length).fill(null);
  if(closes.length<p)return out;
  var s=0;for(var j=0;j<p;j++)s+=closes[j];
  out[p-1]=s/p;var k=2/(p+1);
  for(var i=p;i<closes.length;i++)out[i]=closes[i]*k+out[i-1]*(1-k);
  return out;
}}
function calcIndicators(data){{
  var closes=data.map(function(c){{return c.close;}}),times=data.map(function(c){{return c.time;}}),n=data.length;
  function toS(arr){{var o=[];for(var i=0;i<arr.length;i++)if(arr[i]!==null)o.push({{time:times[i],value:parseFloat(arr[i].toFixed(4))}});return o;}}
  var e20=_emaArr(closes,20),e50=_emaArr(closes,50);
  var e12=_emaArr(closes,12),e26=_emaArr(closes,26);
  var macdArr=new Array(n).fill(null);
  for(var i=0;i<n;i++)if(e12[i]!==null&&e26[i]!==null)macdArr[i]=e12[i]-e26[i];
  var fm=macdArr.findIndex(function(v){{return v!==null;}});
  var sigArr=new Array(n).fill(null);
  if(fm>=0){{var ms=macdArr.slice(fm),es=_emaArr(ms,9);for(var i=0;i<ms.length;i++)sigArr[fm+i]=es[i];}}
  // RSI Wilder smoothing
  var rsiArr=new Array(n).fill(null);
  if(n>14){{var g=0,l=0;for(var i=1;i<=14;i++){{var d=closes[i]-closes[i-1];if(d>0)g+=d;else l-=d;}}var ag=g/14,al=l/14;rsiArr[14]=al===0?100:100-(100/(1+ag/al));for(var i=15;i<n;i++){{var d=closes[i]-closes[i-1],gv=d>0?d:0,lv=d<0?-d:0;ag=(ag*13+gv)/14;al=(al*13+lv)/14;rsiArr[i]=al===0?100:100-(100/(1+ag/al));}}}}
  var hist=[];
  for(var i=0;i<n;i++){{if(macdArr[i]!==null&&sigArr[i]!==null){{var v=macdArr[i]-sigArr[i];hist.push({{time:times[i],value:parseFloat(v.toFixed(4)),color:v>=0?'rgba(38,166,154,0.7)':'rgba(239,83,80,0.7)'}});}}}}
  return{{ema20:toS(e20),ema50:toS(e50),rsi:toS(rsiArr),macdLine:toS(macdArr),sigLine:toS(sigArr),histogram:hist}};
}}
function updateIndicators(){{
  if(!candles.length||!_ema20s)return;
  // Compute all indicators over all loaded candles (warmup days + today).
  // Shows continuous EMA/RSI/MACD across all visible days.
  var ind=calcIndicators(candles);
  _ema20s.setData(ind.ema20);_ema50s.setData(ind.ema50);
  if(_rsiSeries)_rsiSeries.setData(ind.rsi);
  if(_macdHist){{_macdHist.setData(ind.histogram);_macdLine.setData(ind.macdLine);_macdSignal.setData(ind.sigLine);}}
  _candleMap={{}};candles.forEach(function(c){{_candleMap[c.time]=c.close;}});
  var _cut=Date.UTC(+curDate.slice(0,4),+curDate.slice(5,7)-1,+curDate.slice(8,10))/1000;
  _rsiMap={{}};ind.rsi.filter(function(d){{return d.time>=_cut;}}).forEach(function(d){{_rsiMap[d.time]=d.value;}});
  _macdMap={{}};ind.sigLine.filter(function(d){{return d.time>=_cut;}}).forEach(function(d){{_macdMap[d.time]=d.value;}});
}}
// ── Pane expand / collapse ────────────────────────────────────────────────────
function togglePaneExpand(n){{
  console.log('[pane] click n='+n+' chartInst='+!!_chartInst+' expandedPane='+_expandedPane);
  if(!_chartInst){{ console.warn('[pane] no chart'); return; }}
  var hasPanes=typeof _chartInst.panes==='function';
  console.log('[pane] hasPanes='+hasPanes);
  if(!hasPanes){{ console.warn('[pane] panes() not a function'); return; }}
  var panes=_chartInst.panes();
  console.log('[pane] panes.length='+panes.length);
  if(!panes.length)return;
  var hasSF=panes[0]&&typeof panes[0].setStretchFactor==='function';
  console.log('[pane] hasSetStretchFactor='+hasSF);
  try{{
    if(_expandedPane===n){{
      _expandedPane=-1;
      _PANE_FACTORS.forEach(function(f,i){{if(panes[i])panes[i].setStretchFactor(f);}});
    }}else{{
      _expandedPane=n;
      // Use extreme ratio so any working implementation is obvious
      panes.forEach(function(p,i){{p.setStretchFactor(i===n?100:0.01);}});
    }}
    console.log('[pane] setStretchFactor done, expandedPane='+_expandedPane);
  }}catch(e){{
    console.error('[pane] setStretchFactor error:',e.message);
  }}
  _updatePaneBtns();
}}
function _updatePaneBtns(){{
  var factors=_expandedPane>=0
    ? _PANE_FACTORS.map(function(f,i){{return i===_expandedPane?7.4:0.1;}})
    : _PANE_FACTORS.slice();
  var total=factors.reduce(function(a,b){{return a+b;}},0),cum=0;
  factors.forEach(function(f,i){{
    var btn=document.getElementById('paneBtn'+i);
    if(btn){{
      btn.style.top='calc('+((cum/total)*100).toFixed(1)+'% + 4px)';
      btn.textContent=(_expandedPane===i)?'↩':'⛶';
      btn.style.color=(_expandedPane===i)?'#4fc3f7':'#666';
    }}
    cum+=f;
  }});
}}
// ─────────────────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', function() {{
  initChart();
  _updatePaneBtns();
  var now=new Date(Date.now()+19800000); // UTC+5:30 IST offset
  var today=now.toISOString().slice(0,10);
  document.getElementById('dp').value=today;
  curDate=today;
  loadAll();
}});

function shiftDay(d) {{
  var p=curDate.split('-');
  var dt=new Date(Date.UTC(+p[0],+p[1]-1,+p[2]));
  dt.setUTCDate(dt.getUTCDate()+d);
  curDate=dt.toISOString().slice(0,10);
  document.getElementById('dp').value=curDate;
  loadAll();
}}
function onDate() {{ curDate=document.getElementById('dp').value; loadAll(); }}
function setU(el) {{
  document.querySelectorAll('#uChips .chip').forEach(function(c){{c.classList.remove('on');}});
  el.classList.add('on'); curU=el.dataset.v; loadAll();
}}
function _filtered(){{
  return allTrades.filter(function(t){{
    return typeOn.has(t.option_type) && dirOn.has(t.direction||'SHORT');
  }});
}}
function togT(el) {{
  var v=el.dataset.v;
  if (typeOn.has(v)) {{ if(typeOn.size>1){{typeOn.delete(v);el.classList.remove('on');}} }}
  else {{ typeOn.add(v); el.classList.add('on'); }}
  var f=_filtered(); renderTrades(f); putMarkers(f);
}}
function togD(el) {{
  var v=el.dataset.v;
  if (dirOn.has(v)) {{ if(dirOn.size>1){{dirOn.delete(v);el.classList.remove('on');}} }}
  else {{ dirOn.add(v); el.classList.add('on'); }}
  var f=_filtered(); renderTrades(f); putMarkers(f);
}}
function loadAll() {{ loadChart(); loadTrades(); }}

async function loadChart() {{
  if (!series) {{ setChartMsg('Chart not ready',''); return; }}
  setChartMsg('Loading chart...','');
  try {{
    var ctl=new AbortController(), tid=setTimeout(function(){{ctl.abort();}},20000);
    var r=await fetch('/api/chart?underlying='+curU+'&date='+curDate,{{signal:ctl.signal}});
    clearTimeout(tid);
    var d=await r.json();
    candles=d.candles||[]; curInterval=d.interval||'1m';
    document.getElementById('ivl').textContent=d.interval||'--';
    if(d.warmup_log) console.log('[warmup]', d.warmup_log);
    if (candles.length) {{
      // Show all loaded candles (warmup days + trade date).
      // Batch warmup ensures consecutive trading days so no gaps.
      series.setData(candles);
      hideChartMsg();
      updateIndicators();
      // Pin visible window to today's trading hours (9:00–15:35 IST).
      var _y=+curDate.slice(0,4),_m=+curDate.slice(5,7)-1,_dd=+curDate.slice(8,10);
      var _r={{from:Date.UTC(_y,_m,_dd,9,0,0)/1000, to:Date.UTC(_y,_m,_dd,15,35,0)/1000}};
      try {{ _chartInst.timeScale().setVisibleRange(_r); }} catch(x) {{}}
    }}
    else setChartMsg('No chart data for '+curU+' '+curDate, d.error||'');
  }} catch(e) {{
    setChartMsg(e.name==='AbortError'?'Chart load timed out':'Chart error: '+e.message,'');
  }}
}}

async function loadSample() {{
  if (!series) {{ setChartMsg('Chart not initialised',''); return; }}
  setChartMsg('Loading sample...','');
  try {{
    var r=await fetch('/api/test-chart?date='+curDate);
    var d=await r.json();
    candles=d.candles||[];
    series.setData(candles);
    if (candles.length) {{ chart.timeScale().fitContent(); hideChartMsg(); document.getElementById('ivl').textContent='sample'; updateIndicators(); }}
    else setChartMsg('0 candles returned','');
  }} catch(e) {{ setChartMsg('Test error: '+e.message,''); }}
}}

async function loadTrades() {{
  try {{
    var r=await fetch('/api/trades?date='+curDate+'&underlying='+curU);
    allTrades=await r.json();
    if(_savedNotes.length) await _restoreNotes();
    var f=_filtered(); renderTrades(f); putMarkers(f);
  }} catch(e) {{ console.error(e); }}
}}
async function _restoreNotes(){{
  for(var i=0;i<_savedNotes.length;i++){{
    var s=_savedNotes[i];
    var m=allTrades.find(function(t){{
      return t.underlying===s.underlying&&t.option_type===s.option_type&&
             t.strike===s.strike&&t.entry_time===s.entry_time;
    }});
    if(m)await saveNote(m.id,s.notes);
  }}
  _savedNotes=[];
}}

function renderTrades(trades) {{
  var tbl=document.getElementById('tbl'),em=document.getElementById('empty');
  var cnt=document.getElementById('pcnt'),sum=document.getElementById('psummary');
  if (!trades.length){{tbl.style.display='none';em.style.display='';cnt.textContent='';sum.innerHTML='';return;}}
  tbl.style.display='';em.style.display='none';
  var closed=trades.filter(function(t){{return t.pnl!=null;}});
  var tot=closed.reduce(function(a,t){{return a+t.pnl;}},0);
  var wins=closed.filter(function(t){{return t.pnl>=0;}}).length;
  var loss=closed.filter(function(t){{return t.pnl<0;}}).length;
  cnt.textContent=trades.length+' trade'+(trades.length>1?'s':'');
  sum.innerHTML='<span class="'+(tot>=0?'pos':'neg')+'">'+(tot>=0?'+':'')+tot.toFixed(0)+'</span>&nbsp;'+wins+'W/'+loss+'L';
  var rows=trades.map(function(t) {{
    var tc=t.option_type.toLowerCase();
    var sk=t.strike?t.strike.toLocaleString('en-IN'):'--';
    var ep=t.exit_price!=null?t.exit_price.toFixed(2):'--';
    var pl=t.pnl!=null?'<span class="'+(t.pnl>=0?'pos':'neg')+'">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(0)+'</span>':'--';
    var lts=t.lots?t.lots+'L':t.quantity;
    var sel=selId===t.id?' sel':'';
    var nt=(t.notes||'').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    return '<tr class="'+sel+'" data-id="'+t.id+'" data-et="'+(t.entry_time||'')+'" onclick="selTrade(+this.dataset.id,this.dataset.et)">' +
      '<td style="white-space:nowrap">'+(t.entry_time?t.entry_time.slice(0,5):'--')+(t.exit_time?' <span style="color:#444">&#8594;</span> '+t.exit_time.slice(0,5):'')+'</td>' +
      '<td><span class="tag '+tc+'">'+t.option_type+'</span></td>' +
      '<td>'+sk+'</td><td>'+t.entry_price.toFixed(2)+'</td><td>'+ep+'</td>' +
      '<td>'+lts+'</td><td>'+pl+'</td>' +
      '<td><input class="ni" value="'+nt+'" placeholder="note..." ' +
        'onclick="event.stopPropagation()" onblur="saveNote('+t.id+',this.value)"></td>' +
      (t.status==='OPEN'?'<td><button class="delbtn" style="color:#ffb74d;font-size:10px;white-space:nowrap" onclick="closeTrade('+t.id+',event)" title="Mark closed">&#10003; Close</button> <button class="delbtn" onclick="delTrade('+t.id+',event)" title="Delete">&#215;</button></td>':'<td><button class="delbtn" onclick="delTrade('+t.id+',event)" title="Delete">&#215;</button></td>') +
      '</tr>';
  }});
  document.getElementById('tbody').innerHTML=rows.join('');
}}

function tsFor(ds,ts){{
  if(!ts)return null;
  var p=ds.split('-'),q=ts.split(':');
  return Date.UTC(+p[0],+p[1]-1,+p[2],+q[0],+q[1],0)/1000;
}}
function snapTs(ts){{
  if(!ts||!candles.length)return ts;
  var best=candles[0].time,diff=Math.abs(candles[0].time-ts);
  for(var i=0;i<candles.length;i++){{
    var d=Math.abs(candles[i].time-ts);
    if(d<diff){{diff=d;best=candles[i].time;}}
    if(candles[i].time>ts+7200)break;
  }}
  return best;
}}
function putMarkers(trades){{
  if(!series)return;
  var list=isolateId!==null?trades.filter(function(t){{return t.id===isolateId;}}):trades;
  var markers=[];
  for(var i=0;i<list.length;i++){{
    var t=list[i];
    var col=t.option_type==='CE'?'#4fc3f7':'#ffb74d';
    var lbl=t.option_type+' '+(t.strike?t.strike.toLocaleString('en-IN'):'');
    var ets=tsFor(curDate,t.entry_time);
    if(ets)markers.push({{time:snapTs(ets),position:'aboveBar',color:col,shape:'arrowDown',text:lbl,id:'e'+t.id}});
    if(t.exit_time&&t.exit_price!=null){{
      var xts=tsFor(curDate,t.exit_time);
      if(xts)markers.push({{
        time:snapTs(xts),position:'belowBar',
        color:(t.pnl!=null&&t.pnl>=0)?'#4caf50':'#ef5350',
        shape:'arrowUp',
        text:t.pnl!=null?(t.pnl>=0?'+':'')+Math.round(t.pnl):t.exit_price.toFixed(0),
        id:'x'+t.id
      }});
    }}
  }}
  markers.sort(function(a,b){{return a.time-b.time;}});
  if(_markersPlugin)_markersPlugin.setMarkers(markers);
}}
function selTrade(id,entryTime){{
  if(selId===id){{
    selId=null; isolateId=null;
    document.querySelectorAll('#tbody tr').forEach(function(r){{r.classList.remove('sel');}});
    putMarkers(_filtered()); return;
  }}
  selId=id; isolateId=id;
  document.querySelectorAll('#tbody tr').forEach(function(r){{r.classList.toggle('sel',+r.dataset.id===id);}});
  putMarkers(_filtered());
  if(entryTime&&candles.length){{
    var ts=tsFor(curDate,entryTime);
    var sec=curInterval==='5m'?300:60;
    if(ts)chart.timeScale().setVisibleRange({{from:ts-sec*25,to:ts+sec*90}});
  }}
}}
async function closeTrade(id,e){{
  e.stopPropagation();
  var px=prompt('Exit price? Enter 0 if expired worthless.','0');
  if(px===null)return;
  var price=parseFloat(px);
  if(isNaN(price)||price<0){{alert('Invalid price');return;}}
  try{{
    var r=await fetch('/api/trade/'+id+'/close',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{exit_price:price}})}});
    var d=await r.json();
    if(d.ok){{
      var t=allTrades.find(function(x){{return x.id===id;}});
      if(t){{t.status='CLOSED';t.exit_price=price;t.exit_time='15:30:00';t.pnl=d.pnl;}}
      var f=_filtered(); renderTrades(f); putMarkers(f);
    }}
  }}catch(err){{console.error(err);}}
}}
async function delTrade(id,e){{
  e.stopPropagation();
  if(!confirm('Delete this trade?'))return;
  try{{
    await fetch('/api/trade/'+id,{{method:'DELETE'}});
    allTrades=allTrades.filter(function(t){{return t.id!==id;}});
    if(selId===id){{selId=null;isolateId=null;}}
    var f=_filtered(); renderTrades(f); putMarkers(f);
  }}catch(e){{console.error(e);}}
}}
async function wipeDate(){{
  if(!confirm('Delete ALL trades for '+curDate+' and reimport from Dhan?'))return;
  try{{
    _savedNotes=allTrades.filter(function(t){{return t.notes&&t.notes.trim();}})
      .map(function(t){{return {{underlying:t.underlying,option_type:t.option_type,strike:t.strike,entry_time:t.entry_time,notes:t.notes}};}});
    await fetch('/api/trades/date/'+curDate,{{method:'DELETE'}});
    allTrades=[]; renderTrades([]); putMarkers([]);
    var btn=document.getElementById('mBtn');
    document.getElementById('mFrom').value=curDate;
    document.getElementById('mTo').value=curDate;
    document.getElementById('ov').classList.add('show');
  }}catch(e){{console.error(e);}}
}}
async function saveNote(id,notes){{
  try{{
    await fetch('/api/trade/'+id+'/notes',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{notes:notes}})}});
    var t=allTrades.find(function(x){{return x.id===id;}});
    if(t)t.notes=notes;
  }}catch(e){{console.error(e);}}
}}
function switchTab(t){{
  document.getElementById('mpanel-api').style.display=t==='api'?'':'none';
  document.getElementById('mpanel-csv').style.display=t==='csv'?'':'none';
  document.getElementById('mtab-api').classList.toggle('on',t==='api');
  document.getElementById('mtab-csv').classList.toggle('on',t==='csv');
}}
function openImp(){{
  var t=new Date().toISOString().slice(0,10);
  document.getElementById('mFrom').value=t;
  document.getElementById('mTo').value=t;
  document.getElementById('mres').textContent='';
  document.getElementById('mdiag').style.display='none';
  document.getElementById('mdiag').textContent='';
  document.getElementById('mres2').textContent='';
  document.getElementById('mdiag2').style.display='none';
  document.getElementById('mdiag2').textContent='';
  switchTab('api');
  document.getElementById('ov').classList.add('show');
}}
function closeImp(){{document.getElementById('ov').classList.remove('show');}}
async function doImport(){{
  var btn=document.getElementById('mBtn'),res=document.getElementById('mres'),diag=document.getElementById('mdiag');
  btn.disabled=true; btn.textContent='Importing...'; res.style.color=''; res.textContent='Fetching...';
  diag.style.display='none'; diag.textContent='';
  try{{
    var r=await fetch('/api/import',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{from_date:document.getElementById('mFrom').value,to_date:document.getElementById('mTo').value}})}});
    var d=await r.json();
    if(d.ok){{
      res.style.color='#4caf50';
      res.textContent=d.imported+' new, '+d.skipped+' stored ('+d.total_options+' options in '+d.total_raw+' trades)';
      if((d.total_raw===0||d.total_options===0)&&d.diag){{
        diag.textContent=JSON.stringify(d.diag,null,2);
        diag.style.display='block';
      }}
      if(d.imported>0)loadTrades();
    }}else{{res.style.color='#ef5350';res.textContent='Error: '+d.error;}}
  }}catch(e){{res.style.color='#ef5350';res.textContent='Network error: '+e.message;}}
  btn.disabled=false; btn.textContent='Import';
}}
async function doImportCsv(){{
  var inp=document.getElementById('mCsvFile');
  var btn=document.getElementById('mBtn2');
  var res=document.getElementById('mres2');
  var diag=document.getElementById('mdiag2');
  if(!inp.files||!inp.files.length){{res.style.color='#ef5350';res.textContent='Select a CSV file first';return;}}
  btn.disabled=true; btn.textContent='Importing...'; res.style.color=''; res.textContent='Reading...';
  diag.style.display='none'; diag.textContent='';
  var fd=new FormData(); fd.append('file',inp.files[0]);
  try{{
    var r=await fetch('/api/import-csv',{{method:'POST',body:fd}});
    var d=await r.json();
    if(d.ok){{
      res.style.color='#4caf50';
      res.textContent=d.imported+' new, '+d.skipped+' stored ('+d.total_options+' options / '+d.total_raw+' rows)';
      if((d.total_raw===0||d.total_options===0)&&d.diag){{
        diag.textContent=JSON.stringify(d.diag,null,2); diag.style.display='block';
      }}
      if(d.imported>0)loadTrades();
    }}else{{res.style.color='#ef5350';res.textContent='Error: '+d.error;}}
  }}catch(e){{res.style.color='#ef5350';res.textContent='Error: '+e.message;}}
  btn.disabled=false; btn.textContent='Upload & Import';
}}
async function doRefreshToken(){{
  var btns=document.querySelectorAll('.hbtn');
  var btn=null;
  btns.forEach(function(b){{if(b.textContent.indexOf('Token')>=0)btn=b;}});
  if(btn){{btn.textContent='Refreshing...';btn.disabled=true;}}
  try{{
    var r=await fetch('/api/refresh-token',{{method:'POST'}});
    var d=await r.json();
    if(btn){{btn.textContent=d.ok?'OK Token':'Fail Token';btn.style.color=d.ok?'#4caf50':'#ef5350';}}
    setTimeout(function(){{
      if(btn){{btn.textContent='Refresh Token';btn.style.color='';btn.disabled=false;}}
    }},3000);
  }}catch(e){{if(btn){{btn.textContent='Refresh Token';btn.disabled=false;}}}}
}}
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Trade Analyser %s starting on http://0.0.0.0:%d", APP_VERSION, PORT)
    get_db()
    import token_manager  # noqa: PLC0415
    if token_manager.is_token_refresh_configured():
        logger.info("Refreshing Dhan token at startup...")
        token_manager.refresh_token()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
