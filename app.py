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
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────

APP_VERSION = "v117"

PORT    = int(os.getenv("PORT", "5556"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyser.db")

LOT_SIZES = {
    "NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20,
    "FINNIFTY": 65, "MIDCPNIFTY": 75,
}

DHAN_INDEX_IDS = {
    "NIFTY":     {"security_id": "13",  "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "BANKNIFTY": {"security_id": "25",  "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "FINNIFTY":  {"security_id": "27",  "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "MIDCPNIFTY":{"security_id": "442", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "SENSEX":    {"security_id": "51",  "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
}

# Fallback candidates tried in order when primary returns empty data.
# All indices live in the IDX_I segment (segment "I" in the Dhan instrument CSV).
NIFTY_FALLBACKS = [
    {"security_id": "13", "exchange_segment": "IDX_I",  "instrument_type": "INDEX"},
    {"security_id": "13", "exchange_segment": "NSE_EQ", "instrument_type": "INDEX"},
]

SENSEX_FALLBACKS = [
    {"security_id": "51", "exchange_segment": "IDX_I",  "instrument_type": "INDEX"},
    {"security_id": "51", "exchange_segment": "BSE_EQ", "instrument_type": "INDEX"},
    {"security_id": "51", "exchange_segment": "BSE",    "instrument_type": "INDEX"},
]


CHART_TIMEOUT = 10

# ── Database ────────────────────────────────────────────────────────

_db_lock    = threading.Lock()
_conn: sqlite3.Connection | None = None

# ── In-memory trade cache ────────────────────────────────────────────
# Rebuilt after every write; GET /api/trades and /api/dates serve from here.
_cache_lock:   threading.Lock = threading.Lock()
_trades_cache: dict           = {}  # date -> list[dict]  (all fields, notes resolved)
_dates_cache:  list           = []  # sorted desc, max 90 entries


def _rebuild_cache(conn: sqlite3.Connection | None = None) -> None:
    """Reload all trades from DB into the in-memory cache."""
    global _trades_cache, _dates_cache
    db   = conn or get_db()
    rows = db.execute(
        "SELECT t.*, COALESCE(n.notes, t.notes, '') AS notes"
        " FROM trades t"
        " LEFT JOIN trade_notes n"
        "   ON n.date=t.date AND n.underlying=t.underlying"
        "   AND n.option_type=t.option_type AND n.strike=t.strike AND n.entry_time=t.entry_time"
        " ORDER BY t.date DESC, t.entry_time"
    ).fetchall()
    cache: dict = {}
    for r in rows:
        cache.setdefault(r["date"], []).append(dict(r))
    with _cache_lock:
        _trades_cache = cache
        _dates_cache  = sorted(cache.keys(), reverse=True)[:90]


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_db(_conn)
        _rebuild_cache(_conn)
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
            CREATE TABLE IF NOT EXISTS option_instruments (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                security_id      TEXT NOT NULL,
                exchange_segment TEXT NOT NULL,
                underlying       TEXT NOT NULL,
                option_type      TEXT NOT NULL,
                strike           REAL NOT NULL,
                expiry           TEXT NOT NULL,
                refreshed_at     REAL DEFAULT 0,
                UNIQUE(security_id)
            );
            CREATE INDEX IF NOT EXISTS idx_oi_lookup ON option_instruments(underlying, option_type, strike, expiry);
            CREATE TABLE IF NOT EXISTS tick_data (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                security_id TEXT NOT NULL,
                ts          INTEGER NOT NULL,
                price       REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tick_lookup ON tick_data(security_id, ts);
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
    # deduplicate: old imports stored entry_time="" (createTime="NA" bug); new imports store
    # real times. Same dhan_order_id+direction now has two rows. Keep the one with real entry_time.
    try:
        dup_groups = conn.execute(
            "SELECT dhan_order_id, direction FROM trades"
            " WHERE dhan_order_id != ''"
            " GROUP BY dhan_order_id, direction HAVING COUNT(*) > 1"
        ).fetchall()
        removed = 0
        for g in dup_groups:
            rows = conn.execute(
                "SELECT id, entry_time FROM trades WHERE dhan_order_id=? AND direction=?"
                " ORDER BY CASE WHEN entry_time != '' THEN 0 ELSE 1 END, id DESC",
                (g["dhan_order_id"], g["direction"]),
            ).fetchall()
            for row in rows[1:]:  # keep first (has real entry_time or newest), delete rest
                conn.execute("DELETE FROM trades WHERE id=?", (row["id"],))
                removed += 1
        if removed:
            conn.commit()
            logger.info("Dedup migration: removed %d duplicate trade records", removed)
    except Exception as e:
        logger.warning("Dedup migration failed: %s", e)


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


# ── Tick capture (15-second / 30-second charts) ────────────────────────────────

_EXC_SEG_INT = {"NSE_FNO": 2, "BSE_FNO": 8, "NSE_EQ": 3, "BSE_EQ": 4, "IDX_I": 0}
_tick_lock       = threading.Lock()
_tick_subscribed: dict[str, int] = {}   # security_id → exchange_segment_int
_tick_feed       = None


def _tick_on_data(feed, data):
    if not isinstance(data, dict):
        return
    try:
        sec_id = str(data.get("security_id") or "")
        ltp    = data.get("LTP") or data.get("ltp") or data.get("last_price")
        if not sec_id or ltp is None:
            return
        price = float(ltp)
        ts    = int(time.time()) + 19800   # IST-as-UTC epoch
        with _db_lock:
            db = get_db()
            db.execute("INSERT INTO tick_data (security_id, ts, price) VALUES (?,?,?)",
                       (sec_id, ts, price))
            db.commit()
    except Exception as e:
        logger.debug("tick_on_data error: %s", e)


def _start_tick_feed(instruments: list) -> None:
    global _tick_feed
    try:
        from dhanhq import MarketFeed, DhanContext  # noqa: PLC0415
        load_dotenv(override=True)
        client_id    = os.getenv("DHAN_CLIENT_ID", "")
        access_token = os.getenv("DHAN_ACCESS_TOKEN", "")
        if not client_id or not access_token:
            logger.warning("Tick feed: Dhan credentials not set, skipping")
            return
        if _tick_feed is not None:
            try:
                _tick_feed.disconnect()
            except Exception:
                pass
        ctx  = DhanContext(client_id, access_token)
        feed = MarketFeed(ctx, instruments, on_ticks=_tick_on_data)
        feed.start()
        _tick_feed = feed
        logger.info("Tick feed (re)started with %d instruments", len(instruments))
    except Exception as e:
        logger.warning("Tick feed start failed: %s", e)


def subscribe_ticks(pairs: list[tuple[str, str]]) -> int:
    """Subscribe to tick data. pairs = [(security_id, exchange_segment), ...]"""
    global _tick_subscribed
    added = 0
    with _tick_lock:
        for sec_id, exc_seg in pairs:
            if not sec_id or sec_id in _tick_subscribed:
                continue
            _tick_subscribed[sec_id] = _EXC_SEG_INT.get(exc_seg, 2)
            added += 1
        if added > 0:
            try:
                from dhanhq import MarketFeed  # noqa: PLC0415
                instruments = [
                    (seg_int, sid, MarketFeed.Ticker)
                    for sid, seg_int in _tick_subscribed.items()
                ]
                _start_tick_feed(instruments)
            except Exception as e:
                logger.warning("subscribe_ticks import error: %s", e)
    return added


def _auto_import_scheduler():
    """Background thread: auto-import today's trades at scheduled IST times.
    Ensures the tick feed subscribes early enough to capture intraday 15s data.
    """
    import time as _time
    _IST = timezone(timedelta(hours=5, minutes=30))
    _TRIGGERS = [10, 12, 14]  # fire once each at 10:00, 12:00, 14:00 IST
    _triggered: set = set()
    _last_date = None

    while True:
        try:
            _time.sleep(60)
            now = datetime.now(_IST)
            if now.date() != _last_date:
                _triggered.clear()
                _last_date = now.date()
            # Weekdays only, within market hours
            if now.weekday() >= 5:
                continue
            if not ((9, 15) <= (now.hour, now.minute) <= (15, 30)):
                continue
            for th in _TRIGGERS:
                if now.hour >= th and th not in _triggered:
                    _triggered.add(th)
                    today_str = str(now.date())
                    logger.info("Auto-import: scheduled tick refresh at %02d:00 IST", th)
                    try:
                        import_from_dhan(today_str, today_str)
                        rows = get_db().execute(
                            "SELECT DISTINCT security_id, exchange_segment FROM trades"
                            " WHERE date=? AND security_id != '' AND security_id GLOB '[0-9]*'",
                            (today_str,),
                        ).fetchall()
                        if rows:
                            n = subscribe_ticks(
                                [(r["security_id"], r["exchange_segment"]) for r in rows]
                            )
                            logger.info(
                                "Auto-import: %d new tick instruments subscribed", n
                            )
                    except Exception as e:
                        logger.warning("Auto-import scheduled job failed: %s", e)
        except Exception as e:
            logger.debug("Auto-import scheduler loop error: %s", e)


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


def _parse_fyers_symbol(name: str) -> dict | None:
    """Parse a Fyers symbol like 'BSE:SENSEX2581981600PE' into trade metadata.

    Returns a dict with keys: tradingSymbol, exchangeSegment, drvOptionType,
    drvStrikePrice, drvExpiryDate — or None if unrecognised/non-index.
    """
    import re as _re
    name = name.strip()
    if ":" not in name:
        return None
    exchange_prefix, body = name.split(":", 1)

    if body.endswith("CE"):
        opt_type, body = "CE", body[:-2]
    elif body.endswith("PE"):
        opt_type, body = "PE", body[:-2]
    else:
        return None  # not an option

    # Identify known index underlyings (longest-match first)
    underlying = None
    for u in ("BANKNIFTY", "MIDCPNIFTY", "FINNIFTY", "SENSEX", "NIFTY"):
        if body.startswith(u):
            underlying = u
            date_strike = body[len(u):]
            break
    if underlying is None:
        return None  # stock option — skip

    # Fyers compact date format: YY + M[M] + DD + STRIKE
    # Single-digit months 1-9 use 1 char; Oct/Nov/Dec use 2 chars ("10","11","12")
    # Some older symbols use 3-letter month abbreviation (e.g. BANKNIFTY25AUG...) — skip those
    if not _re.match(r'^\d', date_strike):
        return None  # month abbreviation format — not supported

    year   = 2000 + int(date_strike[:2])
    rest   = date_strike[2:]
    if not rest or not rest[0].isdigit():
        return None  # month-abbreviation format (e.g. "25AUG...") — not supported
    if rest[:2] in ("10", "11", "12"):
        month = int(rest[:2]); rest = rest[2:]
    else:
        month = int(rest[0]);  rest = rest[1:]
    day    = int(rest[:2])
    strike = float(rest[2:])

    expiry    = f"{year:04d}-{month:02d}-{day:02d}"
    exseg     = "BSE_FNO" if exchange_prefix == "BSE" else "NSE_FNO"
    sym_clean = f"{underlying}{date_strike[:]}{opt_type}"

    return {
        "tradingSymbol":   sym_clean,
        "exchangeSegment": exseg,
        "drvOptionType":   opt_type,
        "drvStrikePrice":  strike,
        "drvExpiryDate":   expiry,
    }


def _parse_fyers_csv(content: str) -> tuple[list[dict], str]:
    """Parse a Fyers orderbook CSV export into raw trade dicts for _process_raw_trades."""
    import csv
    import io
    import re as _re
    from datetime import datetime as _dt

    lines = content.splitlines()

    # Find the data header row (starts with "Name,")
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Name,"):
            header_idx = i
            break
    if header_idx is None:
        return [], "Could not find 'Name' header row in Fyers CSV"

    try:
        reader = list(csv.DictReader(io.StringIO("\n".join(lines[header_idx:]))))
    except Exception as e:
        return [], f"CSV parse error: {e}"

    raw: list[dict] = []
    skipped_reasons: dict = {}

    for row in reader:
        status = (row.get("Status") or "").strip()
        if status not in ("Executed",):
            skipped_reasons[status] = skipped_reasons.get(status, 0) + 1
            continue

        qty_raw = (row.get("Qty") or "0").replace(",", "").strip()
        try:
            qty = int(qty_raw)
        except ValueError:
            continue
        if qty <= 0:
            continue

        name = (row.get("Name") or "").strip()
        parsed = _parse_fyers_symbol(name)
        if parsed is None:
            skipped_reasons["non-index"] = skipped_reasons.get("non-index", 0) + 1
            continue

        try:
            price = float((row.get("Traded price") or "0").replace(",", ""))
        except ValueError:
            continue

        # Datetime: "19-08-2025 15:04:03"
        dt_raw = (row.get("Date & Time") or "").strip()
        try:
            create_time = _dt.strptime(dt_raw, "%d-%m-%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            create_time = dt_raw

        # OMS order ID: =""25081900419396"" → strip ="""" wrapper
        oid_raw = (row.get("OMS order ID") or "").strip()
        oid = _re.sub(r'^=""', "", oid_raw).rstrip('"')

        side = (row.get("Side") or "").strip().upper()
        tx_type = "SELL" if side == "SELL" else "BUY"

        raw.append({
            "tradingSymbol":   parsed["tradingSymbol"],
            "transactionType": tx_type,
            "tradedQuantity":  qty,
            "tradedPrice":     price,
            "createTime":      create_time,
            "orderId":         oid,
            "exchangeSegment": parsed["exchangeSegment"],
            "drvOptionType":   parsed["drvOptionType"],
            "drvStrikePrice":  parsed["drvStrikePrice"],
            "drvExpiryDate":   parsed["drvExpiryDate"],
            "securityId":      "",
        })

    if skipped_reasons:
        logger.info("Fyers CSV: skipped rows by reason: %s", skipped_reasons)

    return raw, ""


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


def _real_ts(v) -> str:
    """Return the string if it's a real timestamp; empty string if it's a Dhan 'NA' sentinel."""
    s = str(v or "").strip()
    return "" if not s or s.upper() in ("NA", "N/A", "-") else s


def _ts_to_time(ts: str) -> str:
    """Extract HH:MM:SS from a timestamp string.

    Handles 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM:SS' (trade history ISO),
    and bare 'HH:MM:SS' (trade book — today only, no date prefix).
    """
    if len(ts) >= 19:
        return ts[11:19]
    if len(ts) >= 8:
        return ts[:8]
    return ""


def _fifo_pair(group: list[dict]) -> list[dict]:
    """FIFO position tracking for one (date, security_id) group.

    Processes trades chronologically. Each BUY closes the oldest open SHORT
    position first; any excess BUY quantity opens a new LONG. Vice versa for SELLs.
    This matches how exchanges (and Dhan) calculate P&L — no quantity heuristics needed.

    Returns list of dicts: direction, entry_time, entry_price, exit_time, exit_price,
                           qty, order_id, status, pnl
    """
    def _trade_ts(t: dict) -> str:
        # Trade history records have createTime="NA"; fall through to exchangeTime
        return (_real_ts(t.get("createTime")) or _real_ts(t.get("orderCreateTime")) or
                _real_ts(t.get("exchangeTime")) or _real_ts(t.get("updateTime")) or "")

    sorted_t = sorted(group, key=_trade_ts)
    long_q:  list[dict] = []   # open LONG legs (BUYs awaiting close)
    short_q: list[dict] = []   # open SHORT legs (SELLs awaiting close)
    done:    list[dict] = []

    for t in sorted_t:
        tx    = _tx_type(t)
        ts    = _trade_ts(t)
        qty   = int(t.get("tradedQuantity") or t.get("quantity") or 0)
        price = float(t.get("tradedPrice") or t.get("price") or 0)
        oid   = str(t.get("orderId") or t.get("order_id") or "")
        if qty <= 0:
            continue

        if tx == "SELL":
            rem = qty
            # Close oldest LONG positions first (FIFO)
            while rem > 0 and long_q:
                head = long_q[0]
                take = min(head["qty"], rem)
                done.append({
                    "direction":   "LONG",
                    "entry_time":  _ts_to_time(head["ts"]),
                    "entry_price": head["price"],
                    "exit_time":   _ts_to_time(ts),
                    "exit_price":  price,
                    "qty":         take,
                    "order_id":    head["oid"],
                    "status":      "CLOSED",
                    "pnl":         round((price - head["price"]) * take, 2),
                })
                rem          -= take
                head["qty"]  -= take
                if head["qty"] == 0:
                    long_q.pop(0)
            if rem > 0:
                short_q.append({"qty": rem, "price": price, "ts": ts, "oid": oid})

        elif tx == "BUY":
            rem = qty
            # Close oldest SHORT positions first (FIFO)
            while rem > 0 and short_q:
                head = short_q[0]
                take = min(head["qty"], rem)
                done.append({
                    "direction":   "SHORT",
                    "entry_time":  _ts_to_time(head["ts"]),
                    "entry_price": head["price"],
                    "exit_time":   _ts_to_time(ts),
                    "exit_price":  price,
                    "qty":         take,
                    "order_id":    head["oid"],
                    "status":      "CLOSED",
                    "pnl":         round((head["price"] - price) * take, 2),
                })
                rem          -= take
                head["qty"]  -= take
                if head["qty"] == 0:
                    short_q.pop(0)
            if rem > 0:
                long_q.append({"qty": rem, "price": price, "ts": ts, "oid": oid})

    # Remaining = still-open positions
    for entry in short_q:
        done.append({
            "direction":   "SHORT",
            "entry_time":  _ts_to_time(entry["ts"]),
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
            "entry_time":  _ts_to_time(entry["ts"]),
            "entry_price": entry["price"],
            "exit_time":   "",
            "exit_price":  None,
            "qty":         entry["qty"],
            "order_id":    entry["oid"],
            "status":      "OPEN",
            "pnl":         None,
        })
    return done


def _process_raw_trades(raw: list[dict], extra_diag: dict | None = None) -> dict:
    """Dedup, filter options, FIFO-pair SELL/BUY, insert into DB. Returns result dict."""
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
        # createTime="NA" in trade history records; fall through to exchangeTime
        ts  = (_real_ts(t.get("createTime")) or _real_ts(t.get("exchangeTime")) or
               _real_ts(t.get("orderCreateTime")) or "")
        sid = str(t.get("securityId") or t.get("security_id") or "")
        trade_date = ts[:10] if len(ts) >= 10 else today_str
        groups[(trade_date, sid)].append(t)

    imported = skipped = 0
    db = get_db()

    for (trade_date, sid), group in groups.items():
        group = _aggregate_partial_fills(group)

        # Extract option metadata from group (all trades share the same option)
        ref        = group[0]
        opt_raw    = (ref.get("drvOptionType") or "").upper()
        opt_type   = "CE" if opt_raw in ("CALL", "CE") else "PE"
        if opt_raw not in ("CALL", "PUT", "CE", "PE"):
            sym_u = (ref.get("tradingSymbol") or ref.get("customSymbol") or "").upper()
            opt_type = "CE" if (sym_u.endswith("CE") or " CALL " in sym_u) else "PE"
        sym        = ref.get("tradingSymbol") or ref.get("customSymbol") or ""
        exseg      = ref.get("exchangeSegment") or "NSE_FNO"
        underlying = _underlying(sym, exseg)
        lot_size   = LOT_SIZES.get(underlying, 1)
        expiry     = str(ref.get("drvExpiryDate") or ref.get("expiryDate") or "")
        strike     = float(ref.get("drvStrikePrice") or ref.get("strikePrice") or 0)
        expiry_date = expiry[:10] if len(expiry) >= 10 else ""

        for pair in _fifo_pair(group):
            entry_time  = pair["entry_time"]
            entry_price = pair["entry_price"]
            exit_time   = pair["exit_time"] or ""
            exit_price  = pair["exit_price"]
            qty         = pair["qty"]
            lots        = round(qty / lot_size, 2) if lot_size else float(qty)
            direction   = pair["direction"]
            status      = pair["status"]
            pnl         = pair["pnl"]
            order_id    = pair["order_id"]

            # Auto-close worthless expiry on next-day re-import.
            # Only fires when today is strictly AFTER expiry (not intraday 0DTE).
            if (status == "OPEN" and expiry_date and expiry_date[:4].isdigit()
                    and today_str > expiry_date):
                exit_time  = "15:30:00"
                exit_price = 0.0
                pnl        = round(entry_price * qty, 2)
                status     = "CLOSED"

            with _db_lock:
                existing = None
                # Primary dedup: dhan_order_id is stable across re-imports regardless of
                # how timestamps were parsed. Use it when available (API imports always have it).
                if order_id:
                    existing = db.execute(
                        "SELECT id, status, entry_time FROM trades"
                        " WHERE dhan_order_id=? AND direction=?",
                        (order_id, direction),
                    ).fetchone()
                # Fallback: time-based dedup for CSV imports (no order ID)
                if not existing and entry_time:
                    existing = db.execute(
                        "SELECT id, status, entry_time FROM trades"
                        " WHERE date=? AND security_id=? AND entry_time=? AND direction=?",
                        (trade_date, sid, entry_time, direction),
                    ).fetchone()
                if not existing:
                    existing = db.execute(
                        "SELECT id, status, entry_time FROM trades"
                        " WHERE date=? AND underlying=? AND option_type=? AND strike=?"
                        " AND entry_time=? AND direction=?",
                        (trade_date, underlying, opt_type, strike, entry_time, direction),
                    ).fetchone()
                if existing:
                    updates, vals = [], []
                    # Patch empty entry_time left by old imports (createTime="NA" bug)
                    if not existing["entry_time"] and entry_time:
                        updates.append("entry_time=?"); vals.append(entry_time)
                    if existing["status"] == "OPEN" and status == "CLOSED":
                        updates += ["exit_time=?","exit_price=?","pnl=?","status=?","quantity=?","lots=?"]
                        vals    += [exit_time, exit_price, pnl, status, qty, lots]
                    if updates:
                        vals.append(existing["id"])
                        db.execute(f"UPDATE trades SET {','.join(updates)} WHERE id=?", vals)
                        db.commit()
                        imported += 1
                    else:
                        skipped += 1
                    continue

                cur = db.execute(
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
                        entry_time, entry_price, exit_time or "", exit_price,
                        qty, lot_size, lots, pnl, status,
                        sid, exseg, order_id, datetime.now().timestamp(), direction,
                    ),
                )
                db.commit()
                # Restore any note saved before this date was last wiped
                if entry_time:
                    note_row = db.execute(
                        "SELECT notes FROM trade_notes"
                        " WHERE date=? AND underlying=? AND option_type=? AND strike=? AND entry_time=?",
                        (trade_date, underlying, opt_type, strike, entry_time),
                    ).fetchone()
                    if note_row and note_row["notes"]:
                        db.execute("UPDATE trades SET notes=? WHERE id=?",
                                   (note_row["notes"], cur.lastrowid))
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
    inst = (trade.get("instrumentType") or trade.get("drvInstrumentType") or
            trade.get("instrument") or "").upper()
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
            # Dhan v2 API expects YYYY-MM-DD in the path parameter
            resp = dhan.get_trade_history(
                from_date=from_date,
                to_date=to_date,
                page_number=page,
            )
            batch = _extract_batch(resp)
            logger.info("get_trade_history page=%d: %d records", page, len(batch))
            if not batch:
                if page == 0 and not tried_p1_fallback:
                    tried_p1_fallback = True
                    resp1 = dhan.get_trade_history(
                        from_date=from_date,
                        to_date=to_date,
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
            # Produce IST-as-UTC epoch regardless of VPS timezone.
            # Dhan /charts/intraday returns true UTC integers; add 5.5h to get IST-as-UTC
            # so TradingView shows IST times (it displays UTC, which then reads as IST).
            # String path: force UTC interpretation so IST hour = epoch hour on any locale.
            if isinstance(ts_raw, (int, float)):
                ts_epoch = int(ts_raw) + 19800
            else:
                ts_str = str(ts_raw).strip()
                if len(ts_str) <= 8:
                    ts_str = f"{trade_date} {ts_str}"
                dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                ts_epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
            candles.append({
                "time":  ts_epoch,
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


# ── Option chart helpers ───────────────────────────────────────────────────────────────────


def _fetch_option_candles(security_id: str, exchange_segment: str,
                          from_date: str, to_date: str, interval: int = 1) -> tuple[list[dict], str]:
    """Fetch OHLCV candles for an option contract via /charts/intraday.

    interval: 1, 5, 15, 25, or 60 minutes (Dhan-native).
    For 3-minute candles, fetch interval=1 and call _aggregate_candles(candles, 3).
    """
    try:
        dhan = _dhan_client()
        resp = _with_timeout(
            dhan.intraday_minute_data,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type="OPTIDX",
            from_date=f"{from_date} 09:00:00",
            to_date=f"{to_date} 15:30:00",
            interval=interval,
        )
        if _is_auth_error(resp):
            try:
                import token_manager  # noqa: PLC0415
                if token_manager.refresh_token():
                    dhan = _dhan_client()
                    resp = _with_timeout(
                        dhan.intraday_minute_data,
                        security_id=security_id,
                        exchange_segment=exchange_segment,
                        instrument_type="OPTIDX",
                        from_date=f"{from_date} 09:00:00",
                        to_date=f"{to_date} 15:30:00",
                        interval=interval,
                    )
            except Exception as e:
                logger.warning("Token refresh failed: %s", e)
        candles = _parse_dhan_candles(resp, to_date)
        return candles, ""
    except Exception as e:
        return [], str(e)


def _parse_rolling_response(resp: dict, side: str) -> tuple[list[dict], list[float], list[dict]]:
    """Parse the nested rolling options API response structure.

    Dhan's /charts/rollingoption returns:
      resp["data"]["data"]["ce"] = {open, high, low, close, volume, spot, timestamp, ...}
      resp["data"]["data"]["pe"] = same or null

    Returns (ohlcv_candles, spot_values, spot_series).
    spot_series = [{"ts": <IST-as-UTC epoch>, "spot": <float>}, ...]
    side: 'ce' or 'pe'
    """
    outer = resp.get("data") if isinstance(resp, dict) else None
    inner = outer.get("data") if isinstance(outer, dict) else None
    side_data = (inner.get(side) if isinstance(inner, dict) else None) or {}
    if not isinstance(side_data, dict):
        return [], [], []

    timestamps = side_data.get("timestamp") or []
    opens  = side_data.get("open")  or []
    highs  = side_data.get("high")  or []
    lows   = side_data.get("low")   or []
    closes = side_data.get("close") or []
    spots  = side_data.get("spot")  or []

    candles = []
    n = len(timestamps)
    if opens and len(opens) == n:
        for i in range(n):
            try:
                ts_epoch = int(timestamps[i]) + 19800   # true UTC → IST-as-UTC
                candles.append({
                    "time":  ts_epoch,
                    "open":  round(float(opens[i]),  2),
                    "high":  round(float(highs[i]),  2),
                    "low":   round(float(lows[i]),   2),
                    "close": round(float(closes[i]), 2),
                })
            except (TypeError, ValueError):
                continue

    spot_vals = []
    spot_series = []
    n_ts = len(timestamps)
    n_sp = len(spots)
    for i in range(min(n_ts, n_sp)):
        try:
            v = float(spots[i])
            if v > 100:
                ts = int(timestamps[i]) + 19800
                spot_vals.append(v)
                spot_series.append({"ts": ts, "spot": round(v, 2)})
        except (TypeError, ValueError):
            continue
    # If spots exist but timestamps don't align, still collect spot_vals
    if not spot_vals and n_ts == 0:
        for s in spots:
            try:
                v = float(s)
                if v > 100:
                    spot_vals.append(v)
            except (TypeError, ValueError):
                continue

    return candles, spot_vals, spot_series


def _call_rolling_api(dhan, **kwargs):
    """Call expired_options_data with auth-retry."""
    resp = _with_timeout(dhan.expired_options_data, **kwargs)
    if _is_auth_error(resp):
        try:
            import token_manager  # noqa: PLC0415
            if token_manager.refresh_token():
                dhan = _dhan_client()
                resp = _with_timeout(dhan.expired_options_data, **kwargs)
        except Exception as e:
            logger.warning("Token refresh failed in rolling API: %s", e)
    return resp


def _get_nifty_spot_for_ladder(trade_date: str) -> tuple[float, list[dict], str]:
    """Get NIFTY spot price series for a date via the rolling options API.

    Returns (first_spot, spot_series, error).
    spot_series = [{"ts": IST-as-UTC epoch, "spot": float}, ...] for the full trading day.
    """
    try:
        dhan = _dhan_client()
        resp = _call_rolling_api(
            dhan,
            security_id="13",
            exchange_segment="NSE_FNO",
            instrument_type="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,   # Dhan treats 0 as "not provided"; 1 = nearest weekly expiry
            strike="ATM",
            drv_option_type="CALL",
            required_data=["spot"],
            from_date=trade_date,
            to_date=trade_date,
            interval=1,
        )
        status = (resp.get("status") or "").lower() if isinstance(resp, dict) else ""
        if status in ("failure", "failed", "error"):
            remarks = str(resp.get("remarks") or resp.get("message") or "")
            return 0.0, [], f"API error: {remarks[:300]}"
        _, spot_vals, spot_series = _parse_rolling_response(resp, "ce")
        if not spot_vals:
            return 0.0, [], "No spot data — date may be a holiday or outside Dhan's rolling window"
        spot = spot_vals[0]
        logger.info("ATM ladder: spot=%.2f series=%d pts for date=%s", spot, len(spot_series), trade_date)
        return spot, spot_series, ""
    except Exception as e:
        return 0.0, [], str(e)


def _fetch_rolling_candles_data(trade_date: str, strike_offset: str,
                                 option_type: str, interval: int = 1,
                                 from_date: str = None) -> tuple[list[dict], str]:
    """Fetch OHLCV candles for a NIFTY option via the expired rolling options API.

    from_date: start of date range (defaults to trade_date for single-day fetch).
    """
    drv_type = "CALL" if option_type.upper() in ("CE", "CALL") else "PUT"
    side     = "ce"   if drv_type == "CALL"                     else "pe"
    fd = from_date or trade_date
    try:
        dhan = _dhan_client()
        resp = _call_rolling_api(
            dhan,
            security_id="13",
            exchange_segment="NSE_FNO",
            instrument_type="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,   # Dhan treats 0 as "not provided"; 1 = nearest weekly expiry
            strike=strike_offset,
            drv_option_type=drv_type,
            required_data=["open", "high", "low", "close", "volume"],
            from_date=fd,
            to_date=trade_date,
            interval=interval,
        )
        status = (resp.get("status") or "").lower() if isinstance(resp, dict) else ""
        if status in ("failure", "failed", "error"):
            remarks = str(resp.get("remarks") or resp.get("message") or "")
            return [], f"API error: {remarks[:200]}"
        candles, _, _ = _parse_rolling_response(resp, side)
        logger.info("Rolling candles: %s→%s %s %s ivl=%dm → %d candles",
                    fd, trade_date, strike_offset, option_type, interval, len(candles))
        return candles, ""
    except Exception as e:
        return [], str(e)


def _aggregate_candles(candles: list[dict], minutes: int) -> list[dict]:
    """Aggregate 1-minute candles into N-minute candles."""
    if not candles or minutes <= 1:
        return candles
    result: list[dict] = []
    bucket: dict | None = None
    for c in candles:
        # IST-as-UTC: utcfromtimestamp gives the IST clock time
        dt = datetime.utcfromtimestamp(c["time"])
        m = dt.hour * 60 + dt.minute
        # Align to N-minute boundaries from market open (09:15 = 555 min)
        bucket_m = (m // minutes) * minutes
        bh, bm_m = divmod(bucket_m, 60)
        bucket_dt = dt.replace(hour=bh, minute=bm_m, second=0)
        bucket_ts = int(bucket_dt.timestamp())
        if bucket is None or bucket["time"] != bucket_ts:
            if bucket:
                result.append(bucket)
            bucket = {
                "time": bucket_ts,
                "open": c["open"], "high": c["high"],
                "low":  c["low"],  "close": c["close"],
            }
        else:
            bucket["high"]  = max(bucket["high"], c["high"])
            bucket["low"]   = min(bucket["low"],  c["low"])
            bucket["close"] = c["close"]
    if bucket:
        result.append(bucket)
    return result


def _option_chart_page() -> str:
    today = str(date.today())
    ver   = APP_VERSION
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Option Chart — Trade Analyser {ver}</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0d0d0d;color:#ccc;font:13px/1.4 ‘Segoe UI’,sans-serif;display:flex;flex-direction:column;height:100vh;overflow:hidden;}}
#hdr{{display:flex;align-items:center;gap:10px;padding:6px 14px;border-bottom:1px solid #1e1e1e;flex-shrink:0;}}
#hdr a{{color:#555;text-decoration:none;font-size:11px;}}
#hdr a:hover{{color:#aaa;}}
.htitle{{font-weight:600;font-size:14px;color:#ccc;}}
.badge{{font-size:10px;color:#555;}}
.ivl-btn{{background:#111;border:1px solid #2a2a2a;color:#888;padding:3px 9px;border-radius:3px;cursor:pointer;font-size:11px;}}
.ivl-btn.on{{background:#1a2a1a;border-color:#3a6a3a;color:#4fc3f7;}}
#chartArea{{flex:1;min-height:0;position:relative;border-bottom:1px solid #1e1e1e;}}
#chartEl{{width:100%;height:100%;}}
#chartTitle{{position:absolute;top:8px;left:10px;font-size:12px;font-weight:500;color:#C3BCDB;pointer-events:none;z-index:2;white-space:nowrap;}}
#msgEl{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#555;font-size:13px;text-align:center;pointer-events:none;z-index:3;}}
#errBanner{{position:absolute;bottom:10px;left:50%;transform:translateX(-50%);background:#2a1010;border:1px solid #4a2020;color:#f85149;font-size:12px;padding:6px 14px;border-radius:4px;z-index:5;display:none;max-width:80%;text-align:center;}}
/* bottom pane */
#tradesPane{{flex:0 0 265px;display:flex;flex-direction:column;min-height:0;}}
#tp-hdr{{padding:5px 10px;border-bottom:1px solid #0f0f0f;display:flex;align-items:center;gap:7px;flex-shrink:0;}}
.nbtn{{background:#111;border:1px solid #2a2a2a;color:#888;padding:2px 8px;border-radius:3px;cursor:pointer;font-size:12px;}}
.nbtn:hover{{border-color:#444;color:#ccc;}}
#dateIn{{width:118px;background:#111;border:1px solid #2a2a2a;color:#ccc;padding:3px 6px;border-radius:3px;font-size:12px;}}
#tcount{{font-size:11px;color:#555;}}
.mbtn{{margin-left:auto;background:#111;border:1px solid #2a2a2a;color:#888;padding:3px 9px;border-radius:3px;cursor:pointer;font-size:11px;white-space:nowrap;}}
.mbtn:hover{{border-color:#555;color:#bbb;}}
#manualForm{{padding:8px 10px;border-bottom:1px solid #1e1e1e;background:#060606;display:none;flex-shrink:0;}}
.mrow{{display:flex;gap:7px;align-items:flex-end;flex-wrap:wrap;}}
.mf{{display:flex;flex-direction:column;gap:3px;}}
.mf label{{font-size:10px;color:#666;}}
.mf input,.mf select{{background:#111;border:1px solid #2a2a2a;color:#ccc;padding:4px 6px;border-radius:3px;font-size:12px;}}
.gobtn{{background:#1a2a1a;border:1px solid #3a6a3a;color:#4fc3f7;padding:5px 12px;border-radius:3px;cursor:pointer;font-size:12px;font-weight:600;align-self:flex-end;}}
.gobtn:hover{{background:#224422;}}
#tp-body{{flex:1;overflow-y:auto;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
thead th{{position:sticky;top:0;background:#0a0a0a;color:#555;font-weight:500;padding:5px 10px;text-align:left;border-bottom:1px solid #1e1e1e;font-size:11px;white-space:nowrap;}}
tbody td{{padding:5px 10px;border-bottom:1px solid #0f0f0f;vertical-align:middle;white-space:nowrap;}}
tbody tr{{cursor:pointer;}}
tbody tr:hover{{background:#0f0f0f;}}
tbody tr.sel{{background:#0d1a0d;outline:1px solid #3a6a3a;outline-offset:-1px;}}
.ce{{color:#4fc3f7;}}.pe{{color:#ffb74d;}}
.g{{color:#3fb950;}}.r{{color:#f85149;}}
.dtag{{font-size:10px;color:#555;border:1px solid #222;padding:1px 4px;border-radius:2px;}}
#noTrades{{padding:28px;text-align:center;color:#444;font-size:12px;}}
</style>
</head>
<body>
<div id="hdr">
  <a href="/">&#8592; Main</a>
  <span class="htitle">Option Chart</span>
  <a href="/option-expiry" style="margin-left:6px;background:#111;border:1px solid #2a2a2a;color:#888;padding:3px 9px;border-radius:3px;font-size:11px;text-decoration:none;">By Expiry &#8599;</a>
  <a href="/option-ladder" style="background:#111;border:1px solid #2a2a2a;color:#888;padding:3px 9px;border-radius:3px;font-size:11px;text-decoration:none;">ATM Ladder &#8599;</a>
  <span class="badge">{ver}</span>
  <div style="margin-left:auto;display:flex;gap:4px;">
    <button class="ivl-btn" id="tick15s" onclick="setTick(15)" title="15-second tick chart (today only)">15s</button>
    <button class="ivl-btn" id="tick30s" onclick="setTick(30)" title="30-second tick chart (today only)">30s</button>
    <span style="width:1px;background:#2a2a2a;margin:2px 2px;"></span>
    <button class="ivl-btn on" id="ivl1"  onclick="setIvl(1)">1m</button>
    <button class="ivl-btn"    id="ivl3"  onclick="setIvl(3)">3m</button>
    <button class="ivl-btn"    id="ivl5"  onclick="setIvl(5)">5m</button>
    <button class="ivl-btn"    id="ivl15" onclick="setIvl(15)">15m</button>
  </div>
</div>
<div id="chartArea">
  <div id="chartTitle"></div>
  <div id="chartEl"></div>
  <div id="msgEl">Select a trade below to view its option chart</div>
  <div id="errBanner"></div>
</div>
<div id="tradesPane">
  <div id="tp-hdr">
    <button class="nbtn" onclick="shiftDay(-1)">&#9664;</button>
    <input type="date" id="dateIn" value="{today}" onchange="loadDate(this.value)">
    <button class="nbtn" onclick="shiftDay(1)">&#9654;</button>
    <span id="tcount"></span>
    <button class="mbtn" id="mtoggle" onclick="toggleManual()">&#43; Manual</button>
  </div>
  <div id="manualForm">
    <div class="mrow">
      <div class="mf">
        <label>Underlying</label>
        <select id="mf-ul" style="width:90px;"><option>NIFTY</option><option>SENSEX</option><option>BANKNIFTY</option><option>FINNIFTY</option><option>MIDCPNIFTY</option></select>
      </div>
      <div class="mf">
        <label>Type</label>
        <select id="mf-ot" style="width:52px;"><option>CE</option><option>PE</option></select>
      </div>
      <div class="mf">
        <label>Strike</label>
        <input type="number" id="mf-strike" placeholder="24500" step="50" style="width:75px;">
      </div>
      <div class="mf">
        <label>Expiry (required)</label>
        <input type="date" id="mf-expiry" style="width:124px;">
      </div>
      <button class="gobtn" onclick="loadManualChart()">Load</button>
    </div>
  </div>
  <div id="tp-body">
    <div id="noTrades">Loading&#8230;</div>
    <table id="tbl" style="display:none">
      <thead><tr>
        <th>Time</th><th>Opt</th><th>Strike</th><th>Expiry</th>
        <th>Entry &#8377;</th><th>Exit &#8377;</th><th>P&amp;L</th><th>Dir</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>
<script>
var _chart=null,_series=null,_markersPlugin=null,_curIvl=1,_curTick=0,_selRow=null;
var _ema20s=null,_ema50s=null,_rsiSeries=null;
var _macdHist=null,_macdLine=null,_macdSignal=null;
var _tradeDates=[],TODAY=’{today}’;

// ── Chart init ────────────────────────────────────────────────────────────────
(function initChart(){{
  try{{
    var el=document.getElementById(‘chartEl’);
    _chart=LightweightCharts.createChart(el,{{
      layout:{{background:{{color:’#0d0d0d’}},textColor:’#aaa’}},
      grid:{{vertLines:{{color:’#1a1a1a’}},horzLines:{{color:’#1a1a1a’}}}},
      crosshair:{{mode:0}},
      rightPriceScale:{{borderColor:’#2a2a2a’}},
      timeScale:{{borderColor:’#2a2a2a’,timeVisible:true,secondsVisible:true}},
    }});
    _series=_chart.addSeries(LightweightCharts.CandlestickSeries,{{
      upColor:’#3fb950’,downColor:’#f85149’,
      borderUpColor:’#3fb950’,borderDownColor:’#f85149’,
      wickUpColor:’#3fb950’,wickDownColor:’#f85149’,
    }});
    _markersPlugin=LightweightCharts.createSeriesMarkers(_series,[]);
    _ema20s=_chart.addSeries(LightweightCharts.LineSeries,{{
      color:’#2196F3’,lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false
    }});
    _ema50s=_chart.addSeries(LightweightCharts.LineSeries,{{
      color:’#FF9800’,lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false
    }});
    _rsiSeries=_chart.addSeries(LightweightCharts.LineSeries,{{
      color:’#58a6ff’,lineWidth:1,lastValueVisible:true,priceLineVisible:false
    }},1);
    _rsiSeries.createPriceLine({{price:70,color:’#2a2a2a’,lineWidth:1,lineStyle:1,axisLabelVisible:false}});
    _rsiSeries.createPriceLine({{price:30,color:’#2a2a2a’,lineWidth:1,lineStyle:1,axisLabelVisible:false}});
    _macdHist=_chart.addSeries(LightweightCharts.HistogramSeries,{{
      color:’#555’,lastValueVisible:false,priceLineVisible:false
    }},2);
    _macdLine=_chart.addSeries(LightweightCharts.LineSeries,{{
      color:’#2196F3’,lineWidth:1,lastValueVisible:false,priceLineVisible:false
    }},2);
    _macdSignal=_chart.addSeries(LightweightCharts.LineSeries,{{
      color:’#FF5722’,lineWidth:1,lastValueVisible:false,priceLineVisible:false
    }},2);
    try{{
      var panes=_chart.panes();
      if(panes[0])panes[0].setStretchFactor(5);
      if(panes[1])panes[1].setStretchFactor(1.2);
      if(panes[2])panes[2].setStretchFactor(1.2);
    }}catch(pe){{console.warn(‘Pane stretch failed:’,pe);}}
    new ResizeObserver(function(){{_chart.resize(el.offsetWidth,el.offsetHeight);}}).observe(el);
  }}catch(e){{console.error(‘Chart init failed:’,e);}}
}})();

// ── Indicator math ─────────────────────────────────────────────────────────────
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
  var rsiArr=new Array(n).fill(null);
  if(n>14){{var g=0,l=0;for(var i=1;i<=14;i++){{var d=closes[i]-closes[i-1];if(d>0)g+=d;else l-=d;}}var ag=g/14,al=l/14;rsiArr[14]=al===0?100:100-(100/(1+ag/al));for(var i=15;i<n;i++){{var d=closes[i]-closes[i-1],gv=d>0?d:0,lv=d<0?-d:0;ag=(ag*13+gv)/14;al=(al*13+lv)/14;rsiArr[i]=al===0?100:100-(100/(1+ag/al));}}}}
  var hist=[];
  for(var i=0;i<n;i++){{if(macdArr[i]!==null&&sigArr[i]!==null){{var v=macdArr[i]-sigArr[i];hist.push({{time:times[i],value:parseFloat(v.toFixed(4)),color:v>=0?’rgba(38,166,154,0.7)’:’rgba(239,83,80,0.7)’}});}}}}
  return{{ema20:toS(e20),ema50:toS(e50),rsi:toS(rsiArr),macdLine:toS(macdArr),sigLine:toS(sigArr),histogram:hist}};
}}
function updateIndicators(data){{
  if(!data.length||!_ema20s)return;
  var ind=calcIndicators(data);
  _ema20s.setData(ind.ema20);_ema50s.setData(ind.ema50);
  if(_rsiSeries)_rsiSeries.setData(ind.rsi);
  if(_macdHist){{_macdHist.setData(ind.histogram);_macdLine.setData(ind.macdLine);_macdSignal.setData(ind.sigLine);}}
}}

// ── Interval ──────────────────────────────────────────────────────────────────
function setIvl(n){{
  _curIvl=n; _curTick=0;
  [1,3,5,15].forEach(function(v){{
    var b=document.getElementById(‘ivl’+v);
    if(b) b.className=’ivl-btn’+(v===n?’ on’:’’);
  }});
  document.getElementById(‘tick15s’).className=’ivl-btn’;
  document.getElementById(‘tick30s’).className=’ivl-btn’;
  if(_selRow) _selRow._load();
}}
function setTick(s){{
  _curTick=s;
  document.getElementById(‘tick15s’).className=’ivl-btn’+(s===15?’ on’:’’);
  document.getElementById(‘tick30s’).className=’ivl-btn’+(s===30?’ on’:’’);
  [1,3,5,15].forEach(function(v){{
    var b=document.getElementById(‘ivl’+v);
    if(b) b.className=’ivl-btn’;
  }});
  if(_selRow) _selRow._load();
}}

function showMsg(m){{var e=document.getElementById(‘msgEl’);e.style.display=’’;e.textContent=m;}}
function hideMsg(){{document.getElementById(‘msgEl’).style.display=’none’;}}
function showErr(m){{var e=document.getElementById(‘errBanner’);e.style.display=m?’’:’none’;e.textContent=m||’’;}}
function fp(v){{return v!=null?v.toFixed(1):’—‘;}}

function fromDateFor(expDate){{
  var p=(expDate||TODAY).split(‘-’);
  var dt=new Date(Date.UTC(+p[0],+p[1]-1,+p[2]));
  dt.setUTCDate(dt.getUTCDate()-30);
  return dt.toISOString().slice(0,10);
}}

// ── Date nav ──────────────────────────────────────────────────────────────────
async function loadDates(){{
  try{{var r=await fetch(‘/api/dates’);_tradeDates=await r.json();}}catch(e){{}}
}}
function shiftDay(dir){{
  var cur=document.getElementById(‘dateIn’).value;
  if(_tradeDates.length){{
    var idx=_tradeDates.indexOf(cur),next;
    if(dir<0){{next=idx<0?_tradeDates[0]:_tradeDates[Math.min(idx+1,_tradeDates.length-1)];}}
    else     {{next=idx<0?_tradeDates[_tradeDates.length-1]:_tradeDates[Math.max(idx-1,0)];}}
    if(next){{loadDate(next);return;}}
  }}
  if(!cur) return;
  var p=cur.split(‘-’);
  var dt=new Date(Date.UTC(+p[0],+p[1]-1,+p[2]));
  dt.setUTCDate(dt.getUTCDate()+dir);
  loadDate(dt.toISOString().slice(0,10));
}}

// ── By Date: load trades table ────────────────────────────────────────────────
async function loadDate(d){{
  document.getElementById(‘dateIn’).value=d;
  if(_selRow){{_selRow.classList.remove(‘sel’);_selRow=null;}}
  var no=document.getElementById(‘noTrades’);
  no.textContent=’Loading…’;no.style.display=’’;
  document.getElementById(‘tbl’).style.display=’none’;
  try{{
    var resp=await fetch(‘/api/trades?date=’+d);
    var trades=await resp.json();
    document.getElementById(‘tcount’).textContent=
      trades.length?trades.length+’ trade’+(trades.length>1?’s’:’’):’’;
    if(!trades.length){{no.textContent=’No trades on this date’;return;}}
    var tbody=document.getElementById(‘tbody’);
    tbody.innerHTML=’’;
    trades.forEach(function(t){{
      var tr=document.createElement(‘tr’);
      var oc=t.option_type===’CE’?’ce’:’pe’;
      var pc=t.pnl>0?’g’:t.pnl<0?’r’:’’;
      var pstr=t.pnl!=null?’₹’+(t.pnl>=0?’+’:’’)+Math.round(t.pnl):’—‘;
      var exp=(t.expiry||’’).slice(0,10);
      tr.innerHTML=
        ‘<td>’+(t.entry_time||’’).slice(0,8)+’</td>’+
        ‘<td class="’+oc+’">’+t.option_type+’</td>’+
        ‘<td>’+t.strike+’</td>’+
        ‘<td style="color:#555">’+exp+’</td>’+
        ‘<td>’+fp(t.entry_price)+’</td>’+
        ‘<td>’+fp(t.exit_price)+’</td>’+
        ‘<td class="’+pc+’">’+pstr+’</td>’+
        ‘<td><span class="dtag">’+(t.direction||’SHORT’).charAt(0)+’</span></td>’;
      tr._load=function(){{loadTradeChart(t,tr);}};
      tr.onclick=function(){{
        if(_selRow)_selRow.classList.remove(‘sel’);
        _selRow=tr;tr.classList.add(‘sel’);
        tr._load();
      }};
      tbody.appendChild(tr);
    }});
    no.style.display=’none’;document.getElementById(‘tbl’).style.display=’’;
  }}catch(e){{no.textContent=’Error loading trades’;}}
}}

// ── Timestamp helpers ─────────────────────────────────────────────────────────
function tradeTs(dateStr,timeStr){{
  var d=dateStr.split(‘-’),t=(timeStr||’00:00:00’).split(‘:’);
  return Date.UTC(+d[0],+d[1]-1,+d[2],+t[0]||0,+t[1]||0,+t[2]||0)/1000;
}}
function snapTs(ts,candles){{
  var window=_curTick>0?_curTick*2:120;
  var best=candles[0].time,bestDiff=Math.abs(candles[0].time-ts);
  for(var i=1;i<candles.length;i++){{
    var diff=Math.abs(candles[i].time-ts);
    if(diff<bestDiff){{bestDiff=diff;best=candles[i].time;}}
    if(candles[i].time>ts+window) break;
  }}
  return best;
}}

// ── Entry/exit markers ────────────────────────────────────────────────────────
function putMarkers(t,candles){{
  if(!_markersPlugin||!candles.length) return;
  var markers=[],optColor=t.option_type===’CE’?’#4fc3f7’:’#ffb74d’;
  if(t.entry_time)
    markers.push({{time:snapTs(tradeTs(t.date,t.entry_time),candles),
      position:’aboveBar’,shape:’arrowDown’,color:optColor,text:’E ‘+fp(t.entry_price)}});
  if(t.exit_time&&t.exit_price!=null)
    markers.push({{time:snapTs(tradeTs(t.date,t.exit_time),candles),
      position:’belowBar’,shape:’arrowUp’,color:t.pnl>0?’#3fb950’:’#f85149’,
      text:’X ‘+fp(t.exit_price)}});
  markers.sort(function(a,b){{return a.time-b.time;}});
  _markersPlugin.setMarkers(markers);
}}

// ── Tick chart fetcher ────────────────────────────────────────────────────────
async function fetchTickAndDraw(t,label){{
  if(!t||!t.security_id){{showErr(‘Select a trade row to use tick charts’);return;}}
  if(t.date!==TODAY){{showErr(‘Tick charts only capture live intraday data — not available for historical dates’);return;}}
  showMsg(‘Loading…’);showErr(‘’);
  try{{
    var r=await fetch(‘/api/tick-candles?security_id=’+encodeURIComponent(t.security_id)
      +’&seconds=’+_curTick+’&date=’+t.date);
    var d=await r.json();
    if(d.error){{showMsg(‘’);showErr(d.error);return;}}
    var c=d.candles||[];
    if(!c.length){{showMsg(‘No tick data yet — ticks start accumulating when the market feed connects after import’);return;}}
    _series.setData(c);
    updateIndicators(c);
    putMarkers(t,c);
    var dp=t.date.split(‘-’);
    var dayStart=Date.UTC(+dp[0],+dp[1]-1,+dp[2],9,15,0)/1000;
    var dayEnd=Date.UTC(+dp[0],+dp[1]-1,+dp[2],15,30,0)/1000;
    _chart.timeScale().setVisibleRange({{from:dayStart,to:dayEnd}});
    hideMsg();
    document.getElementById(‘chartTitle’).textContent=
      (label||’’)+’ \xb7 ‘+_curTick+’s \xb7 ‘+c.length+’ bars’;
  }}catch(e){{showMsg(‘’);showErr(‘Error: ‘+e.message);}}
}}

// ── Core chart fetcher ────────────────────────────────────────────────────────
async function fetchAndDraw(qs,markerTrade,label){{
  if(_curTick>0){{fetchTickAndDraw(markerTrade,label);return;}}
  showMsg(‘Loading…’);showErr(‘’);
  try{{
    var r=await fetch(‘/api/option-candles?’+qs+’&interval=’+_curIvl);
    var d=await r.json();
    if(d.error){{showMsg(‘’);showErr(d.error);return;}}
    var c=d.candles||[];
    if(!c.length){{showMsg(‘No data — option may be outside Dhan rolling window’);return;}}
    _series.setData(c);
    updateIndicators(c);
    if(markerTrade){{
      putMarkers(markerTrade,c);
      var dp=markerTrade.date.split(‘-’);
      var dayStart=Date.UTC(+dp[0],+dp[1]-1,+dp[2],9,15,0)/1000;
      var dayEnd=Date.UTC(+dp[0],+dp[1]-1,+dp[2],15,30,0)/1000;
      _chart.timeScale().setVisibleRange({{from:dayStart,to:dayEnd}});
    }}else{{
      _markersPlugin.setMarkers([]);
      _chart.timeScale().fitContent();
    }}
    hideMsg();
    document.getElementById(‘chartTitle’).textContent=
      (label||’’)+’ \xb7 ‘+_curIvl+’m \xb7 ‘+c.length+’ bars’;
  }}catch(e){{showMsg(‘’);showErr(‘Error: ‘+e.message);}}
}}

// ── By Date: click a trade row ────────────────────────────────────────────────
function loadTradeChart(t){{
  var expDate=(t.expiry||’’).slice(0,10),toDate=expDate||TODAY;
  var qs=’security_id=’+encodeURIComponent(t.security_id||’’)
    +’&exchange_segment=’+encodeURIComponent(t.exchange_segment||’’)
    +’&underlying=’+encodeURIComponent(t.underlying||’’)
    +’&option_type=’+encodeURIComponent(t.option_type||’’)
    +’&strike=’+encodeURIComponent(t.strike||’’)
    +’&expiry=’+encodeURIComponent(expDate)
    +’&from_date=’+encodeURIComponent(fromDateFor(expDate))
    +’&to_date=’+encodeURIComponent(toDate);
  fetchAndDraw(qs,t,t.underlying+’ ‘+t.strike+’ ‘+t.option_type+(expDate?’ exp:’+expDate:’’));
}}

// ── By Date: manual lookup ────────────────────────────────────────────────────
function toggleManual(){{
  var f=document.getElementById(‘manualForm’);
  var open=f.style.display===’block’;
  f.style.display=open?’none’:’block’;
  document.getElementById(‘mtoggle’).textContent=open?’+ Manual’:’✕ Manual’;
}}
function loadManualChart(){{
  var ul=document.getElementById(‘mf-ul’).value;
  var ot=document.getElementById(‘mf-ot’).value;
  var strike=document.getElementById(‘mf-strike’).value;
  var expiry=document.getElementById(‘mf-expiry’).value;
  if(!strike){{showErr(‘Enter a strike price’);return;}}
  if(!expiry){{showErr(‘Enter expiry date’);return;}}
  if(_selRow){{_selRow.classList.remove(‘sel’);_selRow=null;}}
  var qs=’underlying=’+ul+’&option_type=’+ot+’&strike=’+strike
    +’&expiry=’+expiry+’&from_date=’+fromDateFor(expiry)+’&to_date=’+expiry;
  fetchAndDraw(qs,null,ul+’ ‘+strike+’ ‘+ot+’ exp:’+expiry);
}}

// ── Init ──────────────────────────────────────────────────────────────────────
loadDates().then(function(){{
  loadDate(_tradeDates.length?_tradeDates[0]:TODAY);
}});
</script>
</body>
</html>"""


def _option_expiry_page() -> str:
    today = str(date.today())
    ver   = APP_VERSION
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Historical Options — Trade Analyser {ver}</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0d0d0d;color:#ccc;font:13px/1.4 'Segoe UI',sans-serif;display:flex;flex-direction:column;height:100vh;overflow:hidden;}}
#hdr{{display:flex;align-items:center;gap:10px;padding:6px 14px;border-bottom:1px solid #1e1e1e;flex-shrink:0;}}
#hdr a{{color:#555;text-decoration:none;font-size:11px;}}
#hdr a:hover{{color:#aaa;}}
.htitle{{font-weight:600;font-size:14px;color:#ccc;}}
.badge{{font-size:10px;color:#555;}}
.ibtn{{background:#111;border:1px solid #2a2a2a;color:#888;padding:3px 9px;border-radius:3px;cursor:pointer;font-size:11px;}}
.ibtn.on{{background:#1a2a1a;border-color:#3a6a3a;color:#4fc3f7;}}
#ctrlBar{{display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid #1e1e1e;flex-shrink:0;flex-wrap:wrap;}}
.cf{{display:flex;flex-direction:column;gap:3px;}}
.cf label{{font-size:10px;color:#555;}}
.cf select,.cf input{{background:#111;border:1px solid #2a2a2a;color:#ccc;padding:4px 6px;border-radius:3px;font-size:12px;}}
.cf input::placeholder{{color:#444;}}
.type-row{{display:flex;gap:4px;}}
.ldbtn{{background:#1a2a1a;border:1px solid #3a6a3a;color:#4fc3f7;padding:4px 14px;border-radius:3px;cursor:pointer;font-size:12px;font-weight:600;align-self:flex-end;margin-top:14px;}}
.ldbtn:hover{{background:#224422;}}
.rfbtn{{background:#111;border:1px solid #2a2a2a;color:#555;padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px;align-self:flex-end;margin-top:14px;}}
.rfbtn:hover{{border-color:#555;color:#aaa;}}
#rfStatus{{font-size:11px;color:#555;align-self:flex-end;margin-top:16px;}}
#chartArea{{flex:1;min-height:0;position:relative;}}
#chartEl{{width:100%;height:100%;}}
#chartTitle{{position:absolute;top:8px;left:10px;font-size:12px;font-weight:500;color:#C3BCDB;pointer-events:none;z-index:2;white-space:nowrap;}}
#msgEl{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#555;font-size:13px;text-align:center;pointer-events:none;z-index:3;}}
#errBanner{{position:absolute;bottom:10px;left:50%;transform:translateX(-50%);background:#2a1010;border:1px solid #4a2020;color:#f85149;font-size:12px;padding:6px 14px;border-radius:4px;z-index:5;display:none;max-width:90%;text-align:center;}}
</style>
</head>
<body>
<div id="hdr">
  <a href="/">&#8592; Main</a>
  <a href="/option-chart">Option Chart</a>
  <a href="/option-ladder">ATM Ladder</a>
  <span class="htitle">Historical Options</span>
  <span class="badge">{ver}</span>
  <div style="margin-left:auto;display:flex;gap:4px;">
    <button class="ibtn on" id="ivl1"  onclick="setIvl(1)">1m</button>
    <button class="ibtn"    id="ivl3"  onclick="setIvl(3)">3m</button>
    <button class="ibtn"    id="ivl5"  onclick="setIvl(5)">5m</button>
    <button class="ibtn"    id="ivl15" onclick="setIvl(15)">15m</button>
  </div>
</div>
<div id="ctrlBar">
  <div class="cf">
    <label>Underlying</label>
    <select id="f-ul" style="width:110px;">
      <option>NIFTY</option><option>SENSEX</option>
      <option>BANKNIFTY</option><option>FINNIFTY</option><option>MIDCPNIFTY</option>
    </select>
  </div>
  <div class="cf">
    <label>Expiry date</label>
    <input type="date" id="f-expiry" style="width:130px;">
  </div>
  <div class="cf">
    <label>Strike</label>
    <input type="number" id="f-strike" placeholder="e.g. 25000" step="50" style="width:100px;">
  </div>
  <div class="cf">
    <label>Type</label>
    <div class="type-row" style="margin-top:4px;">
      <button class="ibtn on" id="t-ce" onclick="setType('CE')">CE</button>
      <button class="ibtn"    id="t-pe" onclick="setType('PE')">PE</button>
    </div>
  </div>
  <button class="ldbtn" onclick="loadChart()">Load</button>
  <button class="rfbtn" id="rfBtn" onclick="refreshInstruments()" title="Download Dhan instrument master to find security IDs for options not yet traded">&#8635; Refresh Instruments</button>
  <span id="rfStatus"></span>
</div>
<div id="chartArea">
  <div id="chartTitle">Enter underlying, expiry date, and strike above then click Load</div>
  <div id="chartEl"></div>
  <div id="msgEl" style="display:none"></div>
  <div id="errBanner"></div>
</div>
<script>
var _chart=null,_series=null,_markersPlugin=null,_curIvl=1,_optType='CE';
var _ema20s=null,_ema50s=null,_rsiSeries=null;
var _macdHist=null,_macdLine=null,_macdSignal=null;
var TODAY='{today}';

(function initChart(){{
  var el=document.getElementById('chartEl');
  _chart=LightweightCharts.createChart(el,{{
    layout:{{background:{{color:'#0d0d0d'}},textColor:'#aaa'}},
    grid:{{vertLines:{{color:'#1a1a1a'}},horzLines:{{color:'#1a1a1a'}}}},
    crosshair:{{mode:0}},
    rightPriceScale:{{borderColor:'#2a2a2a'}},
    timeScale:{{borderColor:'#2a2a2a',timeVisible:true,secondsVisible:false}},
  }});
  _series=_chart.addSeries(LightweightCharts.CandlestickSeries,{{
    upColor:'#3fb950',downColor:'#f85149',
    borderUpColor:'#3fb950',borderDownColor:'#f85149',
    wickUpColor:'#3fb950',wickDownColor:'#f85149',
  }});
  _markersPlugin=LightweightCharts.createSeriesMarkers(_series,[]);
  _ema20s=_chart.addSeries(LightweightCharts.LineSeries,{{
    color:'#2196F3',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false
  }});
  _ema50s=_chart.addSeries(LightweightCharts.LineSeries,{{
    color:'#FF9800',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false
  }});
  _rsiSeries=_chart.addSeries(LightweightCharts.LineSeries,{{
    color:'#58a6ff',lineWidth:1,lastValueVisible:true,priceLineVisible:false
  }},1);
  _rsiSeries.createPriceLine({{price:70,color:'#2a2a2a',lineWidth:1,lineStyle:1,axisLabelVisible:false}});
  _rsiSeries.createPriceLine({{price:30,color:'#2a2a2a',lineWidth:1,lineStyle:1,axisLabelVisible:false}});
  _macdHist=_chart.addSeries(LightweightCharts.HistogramSeries,{{
    color:'#555',lastValueVisible:false,priceLineVisible:false
  }},2);
  _macdLine=_chart.addSeries(LightweightCharts.LineSeries,{{
    color:'#2196F3',lineWidth:1,lastValueVisible:false,priceLineVisible:false
  }},2);
  _macdSignal=_chart.addSeries(LightweightCharts.LineSeries,{{
    color:'#FF5722',lineWidth:1,lastValueVisible:false,priceLineVisible:false
  }},2);
  var panes=_chart.panes();
  if(panes[0])panes[0].setStretchFactor(5);
  if(panes[1])panes[1].setStretchFactor(1.2);
  if(panes[2])panes[2].setStretchFactor(1.2);
  new ResizeObserver(function(){{_chart.resize(el.offsetWidth,el.offsetHeight);}}).observe(el);
}})();

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
  var rsiArr=new Array(n).fill(null);
  if(n>14){{var g=0,l=0;for(var i=1;i<=14;i++){{var d=closes[i]-closes[i-1];if(d>0)g+=d;else l-=d;}}var ag=g/14,al=l/14;rsiArr[14]=al===0?100:100-(100/(1+ag/al));for(var i=15;i<n;i++){{var d=closes[i]-closes[i-1],gv=d>0?d:0,lv=d<0?-d:0;ag=(ag*13+gv)/14;al=(al*13+lv)/14;rsiArr[i]=al===0?100:100-(100/(1+ag/al));}}}}
  var hist=[];
  for(var i=0;i<n;i++){{if(macdArr[i]!==null&&sigArr[i]!==null){{var v=macdArr[i]-sigArr[i];hist.push({{time:times[i],value:parseFloat(v.toFixed(4)),color:v>=0?'rgba(38,166,154,0.7)':'rgba(239,83,80,0.7)'}});}}}}
  return{{ema20:toS(e20),ema50:toS(e50),rsi:toS(rsiArr),macdLine:toS(macdArr),sigLine:toS(sigArr),histogram:hist}};
}}
function updateIndicators(data){{
  if(!data.length||!_ema20s)return;
  var ind=calcIndicators(data);
  _ema20s.setData(ind.ema20);_ema50s.setData(ind.ema50);
  if(_rsiSeries)_rsiSeries.setData(ind.rsi);
  if(_macdHist){{_macdHist.setData(ind.histogram);_macdLine.setData(ind.macdLine);_macdSignal.setData(ind.sigLine);}}
}}

function setIvl(n){{
  _curIvl=n;
  [1,3,5,15].forEach(function(v){{
    var b=document.getElementById('ivl'+v);
    if(b)b.className='ibtn'+(v===n?' on':'');
  }});
}}
function setType(t){{
  _optType=t;
  ['CE','PE'].forEach(function(v){{
    document.getElementById('t-'+v.toLowerCase()).className='ibtn'+(v===t?' on':'');
  }});
}}
function showMsg(m){{var e=document.getElementById('msgEl');e.style.display='';e.textContent=m;}}
function hideMsg(){{document.getElementById('msgEl').style.display='none';}}
function showErr(m){{var e=document.getElementById('errBanner');e.style.display=m?'block':'none';e.textContent=m||'';}}

function fromDateFor(expDate){{
  var p=(expDate||TODAY).split('-');
  var dt=new Date(Date.UTC(+p[0],+p[1]-1,+p[2]));
  dt.setUTCDate(dt.getUTCDate()-30);
  return dt.toISOString().slice(0,10);
}}

async function loadChart(){{
  var ul=document.getElementById('f-ul').value;
  var expiry=document.getElementById('f-expiry').value;
  var strike=document.getElementById('f-strike').value;
  if(!expiry){{showErr('Select an expiry date');return;}}
  if(!strike){{showErr('Enter a strike price');return;}}
  showMsg('Loading…');showErr('');
  var qs='underlying='+encodeURIComponent(ul)
    +'&option_type='+encodeURIComponent(_optType)
    +'&strike='+encodeURIComponent(strike)
    +'&expiry='+encodeURIComponent(expiry)
    +'&from_date='+encodeURIComponent(fromDateFor(expiry))
    +'&to_date='+encodeURIComponent(expiry)
    +'&interval='+_curIvl;
  try{{
    var r=await fetch('/api/option-candles?'+qs);
    var d=await r.json();
    if(d.error){{showMsg('');showErr(d.error);return;}}
    var c=d.candles||[];
    if(!c.length){{showMsg('No data returned — option may be outside Dhan rolling window');return;}}
    _series.setData(c);
    updateIndicators(c);
    _markersPlugin.setMarkers([]);
    _chart.timeScale().fitContent();
    hideMsg();
    document.getElementById('chartTitle').textContent=
      ul+' '+strike+' '+_optType+' exp:'+expiry+' \xb7 '+_curIvl+'m \xb7 '+c.length+' bars';
  }}catch(e){{showMsg('');showErr('Error: '+e.message);}}
}}

async function refreshInstruments(){{
  var btn=document.getElementById('rfBtn');
  var st=document.getElementById('rfStatus');
  btn.disabled=true;btn.textContent='Downloading…';st.textContent='';
  try{{
    var r=await fetch('/api/refresh-instruments',{{method:'POST'}});
    var d=await r.json();
    if(d.ok){{st.textContent='Cached '+d.count+' contracts';st.style.color='#3fb950';}}
    else{{st.textContent=d.error||'Failed';st.style.color='#f85149';}}
  }}catch(e){{st.textContent='Error: '+e.message;st.style.color='#f85149';}}
  btn.disabled=false;btn.textContent='↻ Refresh Instruments';
}}

// Pressing Enter in any input triggers load
document.addEventListener('keydown',function(e){{if(e.key==='Enter')loadChart();}});
</script>
</body>
</html>"""


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
        # Fetch ALL pages so we can spot pagination duplicates
        all_records = []
        pages_info  = []
        for pg in range(5):  # max 5 pages for debug
            resp  = dhan.get_trade_history(from_date=from_date, to_date=to_date, page_number=pg)
            batch = _extract_batch(resp)
            pages_info.append({"page": pg, "count": len(batch)})
            if not batch:
                break
            all_records.extend(batch)
        # Summarise: show each record's key fields to spot duplicates
        summary = [
            {
                "orderId":         r.get("orderId"),
                "transactionType": r.get("transactionType"),
                "customSymbol":    r.get("customSymbol") or r.get("tradingSymbol"),
                "tradedQuantity":  r.get("tradedQuantity"),
                "tradedPrice":     r.get("tradedPrice"),
                "exchangeTime":    r.get("exchangeTime"),
                "createTime":      r.get("createTime"),
                "securityId":      r.get("securityId"),
            }
            for r in all_records
        ]
        out["history"] = {
            "pages":             pages_info,
            "total_records":     len(all_records),
            "records_summary":   summary,
            "first_record":      all_records[0] if all_records else None,
            "first_record_keys": list(all_records[0].keys()) if all_records else None,
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
        # Ledger report — may include INTRADAY P&L entries that trade_history misses
        try:
            resp_ledger  = dhan.ledger_report(from_date=from_date, to_date=to_date)
            led_batch    = _extract_batch(resp_ledger)
            out["ledger"] = {
                "response_type":     type(resp_ledger).__name__,
                "status":            resp_ledger.get("status") if isinstance(resp_ledger, dict) else None,
                "record_count":      len(led_batch),
                "first_record":      led_batch[0] if led_batch else None,
                "first_record_keys": list(led_batch[0].keys()) if led_batch else None,
                "raw_preview":       str(resp_ledger)[:800],
            }
        except Exception as e:
            out["ledger"] = {"error": str(e)}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "error_type": type(e).__name__})
    return jsonify(out)


@app.route("/api/import", methods=["POST"])
def api_import():
    data      = request.json or {}
    from_date = data.get("from_date") or str(date.today())
    to_date   = data.get("to_date")   or str(date.today())
    try:
        result = import_from_dhan(from_date, to_date)
        _rebuild_cache()
        # Auto-subscribe tick feed for today's freshly imported options
        today = str(date.today())
        if from_date <= today <= to_date:
            try:
                db   = get_db()
                rows = db.execute(
                    "SELECT DISTINCT security_id, exchange_segment FROM trades"
                    " WHERE date=? AND security_id != ''", (today,)
                ).fetchall()
                if rows:
                    n = subscribe_ticks([(r["security_id"], r["exchange_segment"]) for r in rows])
                    if n:
                        logger.info("Auto-subscribed %d new tick instruments for today", n)
            except Exception as e:
                logger.warning("Auto-subscribe ticks failed: %s", e)
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
    is_fyers = content.lstrip().startswith("Report Title,Orderbook report")
    raw, parse_err = (_parse_fyers_csv(content) if is_fyers else _parse_csv_trades(content))
    if parse_err:
        return jsonify({"ok": False, "error": parse_err}), 400
    try:
        result = _process_raw_trades(raw)
        _rebuild_cache()
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
    with _cache_lock:
        rows = list(_trades_cache.get(d, []))
    if u and u != "ALL":
        rows = [r for r in rows if r.get("underlying") == u]
    if ot and ot not in ("ALL", "BOTH", ""):
        rows = [r for r in rows if r.get("option_type") == ot]
    return jsonify(rows)


@app.route("/api/trade/<int:tid>", methods=["DELETE"])
def api_delete_trade(tid: int):
    with _db_lock:
        db = get_db()
        db.execute("DELETE FROM trades WHERE id=?", (tid,))
        db.commit()
    _rebuild_cache()
    return jsonify({"ok": True})


@app.route("/api/trades/date/<trade_date>", methods=["DELETE"])
def api_delete_date(trade_date: str):
    with _db_lock:
        db = get_db()
        # Persist notes to trade_notes before deleting so they survive wipe+reimport
        db.execute(
            """
            INSERT OR REPLACE INTO trade_notes
                (date, underlying, option_type, strike, entry_time, notes, updated_at)
            SELECT date, underlying, option_type, strike, entry_time, notes, ?
            FROM trades
            WHERE date=? AND entry_time != '' AND notes != '' AND notes IS NOT NULL
            """,
            (datetime.now().timestamp(), trade_date),
        )
        db.execute("DELETE FROM trades WHERE date=?", (trade_date,))
        db.commit()
    _rebuild_cache()
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
    _rebuild_cache()
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
    _rebuild_cache()
    return jsonify({"ok": True})


@app.route("/api/chart")
def api_chart():
    u = request.args.get("underlying") or "NIFTY"
    d = request.args.get("date")       or str(date.today())
    candles, interval, err, warmup_log = chart_candles(u, d)
    return jsonify({"candles": candles, "interval": interval, "error": err, "warmup_log": warmup_log})


@app.route("/api/dates")
def api_dates():
    with _cache_lock:
        return jsonify(list(_dates_cache))


@app.route("/option-chart")
def option_chart():
    return _option_chart_page()


@app.route("/option-expiry")
def option_expiry():
    return _option_expiry_page()


@app.route("/api/option-list")
def api_option_list():
    """Return distinct options from the trades database for the picker dropdown."""
    rows = get_db().execute(
        "SELECT underlying, option_type, strike, expiry, security_id, exchange_segment, date"
        " FROM trades"
        " WHERE security_id != ''"
        " ORDER BY date DESC, entry_time DESC"
        " LIMIT 500"
    ).fetchall()
    # Deduplicate by (underlying, option_type, strike, expiry), keep latest trade date
    seen: set = set()
    result = []
    for r in rows:
        key = (r["underlying"], r["option_type"], r["strike"], r["expiry"])
        if key not in seen:
            seen.add(key)
            result.append(dict(r))
    return jsonify(result)


def _refresh_via_option_chain(expiries_to_fetch: list) -> tuple[int, str]:
    """Populate option_instruments for the given expiry list via Dhan option_chain API.
    Returns (count, error). Uses INSERT OR REPLACE so safe to call repeatedly."""
    import time as _time
    dhan = _dhan_client()
    now = _time.time()
    db = get_db()
    count = 0
    with _db_lock:
        for expiry in expiries_to_fetch:
            try:
                oc_resp = dhan.option_chain(13, "IDX_I", expiry)
                oc_data = (((oc_resp.get("data") or {}).get("data") or {}).get("oc") or {})
                if not oc_data:
                    logger.warning("option_chain: empty oc for expiry %s", expiry)
                    continue
                for strike_str, sides in oc_data.items():
                    try:
                        strike_f = float(strike_str)
                    except ValueError:
                        continue
                    for side_key, opt_type in [("ce", "CE"), ("pe", "PE")]:
                        side = sides.get(side_key) or {}
                        sec_id = side.get("security_id")
                        if not sec_id:
                            continue
                        db.execute(
                            "INSERT OR REPLACE INTO option_instruments"
                            " (security_id, exchange_segment, underlying, option_type, strike, expiry, refreshed_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (str(sec_id), "NSE_FNO", "NIFTY", opt_type, strike_f, expiry, now),
                        )
                        count += 1
                db.commit()
                if len(expiries_to_fetch) > 1:
                    _time.sleep(0.3)
            except Exception as oe:
                logger.warning("option_chain fetch failed for expiry %s: %s", expiry, oe)
                continue
    logger.info("option_chain refresh: cached %d NIFTY options for %d expiries", count, len(expiries_to_fetch))
    return count, ""


@app.route("/api/refresh-instruments", methods=["POST"])
def api_refresh_instruments():
    """Populate option_instruments. Tries Dhan CSV first; falls back to option_chain API.

    Optional query param ?expiry=YYYY-MM-DD fetches only that specific expiry (fast, targeted).
    Without it, fetches the next 10 expiries from Dhan (for manual Refresh button).
    """
    import urllib.request, csv, io as _io, time as _time
    target_expiry = request.args.get("expiry") or ""

    # If a specific expiry is requested, go straight to option_chain (no CSV needed)
    if target_expiry:
        try:
            count, err = _refresh_via_option_chain([target_expiry])
            if err:
                return jsonify({"ok": False, "error": err}), 500
            return jsonify({"ok": True, "count": count, "source": "dhan_api", "expiry": target_expiry})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # Full refresh: try CSV first, then fall back to option_chain for all upcoming expiries
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    csv_err = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TradeAnalyser/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(_io.StringIO(content))
        now = _time.time()
        db = get_db()
        count = 0
        with _db_lock:
            for row in reader:
                seg  = (row.get("SEM_SEGMENT") or "").strip()
                inst = (row.get("SEM_INSTRUMENT_NAME") or "").strip()
                if seg not in ("NSE_FNO", "BSE_FNO") or inst not in ("OPTIDX", "OPTSTK"):
                    continue
                sec_id   = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
                symbol   = (row.get("SEM_TRADING_SYMBOL") or "").strip()
                opt_type = (row.get("SEM_OPTION_TYPE") or "").strip().upper()
                expiry   = (row.get("SM_EXPIRY_DATE") or "").strip()
                strike_s = (row.get("SEM_STRIKE_PRICE") or "0").strip()
                if not sec_id or opt_type not in ("CE", "PE"):
                    continue
                try:
                    strike_f = float(strike_s)
                except ValueError:
                    continue
                underlying = _underlying(symbol, seg)
                db.execute(
                    "INSERT OR REPLACE INTO option_instruments"
                    " (security_id, exchange_segment, underlying, option_type, strike, expiry, refreshed_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (sec_id, seg, underlying, opt_type, strike_f, expiry, now),
                )
                count += 1
            db.commit()
        logger.info("Instrument refresh via CSV: cached %d FNO options", count)
        return jsonify({"ok": True, "count": count, "source": "csv"})
    except Exception as e:
        csv_err = str(e)
        logger.warning("CSV instrument refresh failed (%s), falling back to Dhan option_chain API", csv_err)

    # Fallback: fetch all upcoming expiries via option_chain
    try:
        dhan = _dhan_client()
        exp_resp = dhan.expiry_list(13, "IDX_I")
        expiries = ((exp_resp.get("data") or {}).get("data") or [])
        if not expiries:
            return jsonify({"ok": False, "error": f"CSV blocked: {csv_err}. Dhan expiry_list returned nothing"}), 500
        count, err = _refresh_via_option_chain(expiries[:10])
        return jsonify({"ok": True, "count": count, "source": "dhan_api"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"CSV blocked ({csv_err}). API fallback also failed: {e}"}), 500


@app.route("/api/nifty-expiry")
def api_nifty_expiry():
    """Return nearest NIFTY expiry >= requested date from Dhan expiry_list."""
    req_date = request.args.get("date") or str(date.today())
    try:
        dhan = _dhan_client()
        resp = dhan.expiry_list(13, "IDX_I")
        expiries = sorted((resp.get("data") or {}).get("data") or [])
        nearest = None
        if expiries:
            if req_date < expiries[0]:
                from datetime import datetime as _dt, timedelta as _td
                first_exp = _dt.strptime(expiries[0], "%Y-%m-%d").date()
                exp_weekday = first_exp.weekday()
                req_obj = _dt.strptime(req_date, "%Y-%m-%d").date()
                days_ahead = (exp_weekday - req_obj.weekday()) % 7
                nearest = str(req_obj + _td(days=days_ahead))
            else:
                nearest = next((e for e in expiries if e >= req_date), expiries[-1])
        return jsonify({"expiry": nearest, "all": expiries[:12]})
    except Exception as e:
        return jsonify({"expiry": None, "error": str(e)}), 500


@app.route("/api/debug-option-chain")
def api_debug_option_chain():
    """Returns raw Dhan option_chain and expiry_list responses for debugging."""
    expiry = request.args.get("expiry") or ""
    try:
        dhan = _dhan_client()
        expiry_resp = dhan.expiry_list(13, "IDX_I")
    except Exception as e:
        return jsonify({"error": f"expiry_list failed: {e}"})

    # Pick first expiry from list if none given
    if not expiry:
        try:
            exp_data = expiry_resp.get("data") or expiry_resp
            if isinstance(exp_data, list) and exp_data:
                expiry = exp_data[0]
            elif isinstance(exp_data, dict):
                for v in exp_data.values():
                    if isinstance(v, list) and v:
                        expiry = v[0]
                        break
        except Exception:
            pass

    oc_resp = None
    if expiry:
        try:
            oc_resp = dhan.option_chain(13, "IDX_I", expiry)
        except Exception as e:
            oc_resp = {"error": str(e)}

    # Return first 3 entries of option chain data to keep response small
    oc_preview = oc_resp
    if oc_resp and isinstance(oc_resp.get("data"), list):
        oc_preview = dict(oc_resp)
        oc_preview["data"] = oc_resp["data"][:3]
    elif oc_resp and isinstance(oc_resp.get("data"), dict):
        inner = oc_resp["data"]
        oc_preview = dict(oc_resp)
        oc_preview["data"] = {k: (v[:3] if isinstance(v, list) else v) for k, v in inner.items()}

    return jsonify({
        "expiry_list_raw": expiry_resp,
        "expiry_used": expiry,
        "option_chain_preview": oc_preview,
    })


@app.route("/api/debug-ladder")
def api_debug_ladder():
    """Debug endpoint: shows option_instruments table state and lookup results."""
    underlying  = (request.args.get("underlying") or "NIFTY").upper()
    option_type = (request.args.get("option_type") or "CE").upper()
    strike      = request.args.get("strike") or ""
    expiry      = request.args.get("expiry") or ""

    db = get_db()

    # Total row count
    total = db.execute("SELECT COUNT(*) FROM option_instruments").fetchone()[0]
    nifty_count = db.execute(
        "SELECT COUNT(*) FROM option_instruments WHERE underlying=?", (underlying,)
    ).fetchone()[0]

    # Sample expiry values stored for this underlying
    sample_expiries = [
        r[0] for r in db.execute(
            "SELECT DISTINCT expiry FROM option_instruments WHERE underlying=? ORDER BY expiry DESC LIMIT 20",
            (underlying,),
        ).fetchall()
    ]

    # Exact lookup (what /api/option-candles does)
    lookup_rows = []
    if strike:
        try:
            strike_f = float(strike)
            rows = db.execute(
                "SELECT security_id, exchange_segment, expiry FROM option_instruments"
                " WHERE underlying=? AND option_type=? AND strike=?"
                " AND (expiry LIKE ? OR ?='')"
                " ORDER BY expiry DESC LIMIT 10",
                (underlying, option_type, strike_f, f"%{expiry[:10]}%", expiry),
            ).fetchall()
            lookup_rows = [dict(r) for r in rows]
        except ValueError:
            lookup_rows = [{"error": "invalid strike"}]

    return jsonify({
        "queried": {
            "underlying": underlying,
            "option_type": option_type,
            "strike": strike,
            "expiry": expiry,
            "expiry_like": f"%{expiry[:10]}%" if expiry else "(empty — matches all)",
        },
        "db_stats": {
            "total_option_instruments": total,
            f"{underlying}_count": nifty_count,
        },
        "sample_expiries_in_db": sample_expiries,
        "lookup_result": lookup_rows,
    })


@app.route("/api/option-candles")
def api_option_candles():
    # security_id may be passed directly (from trade row) or looked up via symbol fields
    security_id_param      = request.args.get("security_id") or ""
    exchange_segment_param = request.args.get("exchange_segment") or ""
    underlying  = (request.args.get("underlying") or "NIFTY").upper()
    option_type = (request.args.get("option_type") or "CE").upper()
    try:
        strike = float(request.args.get("strike") or 0)
    except ValueError:
        return jsonify({"candles": [], "error": "Invalid strike"}), 400
    expiry    = request.args.get("expiry") or ""
    from_date = request.args.get("from_date") or str(date.today())
    to_date   = request.args.get("to_date")   or str(date.today())
    try:
        interval = int(request.args.get("interval") or 1)
    except ValueError:
        interval = 1

    if security_id_param and exchange_segment_param:
        security_id      = security_id_param
        exchange_segment = exchange_segment_param
    else:
        db  = get_db()
        row = db.execute(
            "SELECT security_id, exchange_segment FROM trades"
            " WHERE underlying=? AND option_type=? AND strike=?"
            " AND (expiry LIKE ? OR ?='')"
            " AND security_id != ''"
            " ORDER BY date DESC LIMIT 1",
            (underlying, option_type, strike, f"%{expiry[:10]}%", expiry),
        ).fetchone()
        if not row:
            # fall back to instrument master cache
            row = db.execute(
                "SELECT security_id, exchange_segment FROM option_instruments"
                " WHERE underlying=? AND option_type=? AND strike=?"
                " AND (expiry LIKE ? OR ?='')"
                " ORDER BY expiry DESC LIMIT 1",
                (underlying, option_type, strike, f"%{expiry[:10]}%", expiry),
            ).fetchone()
        if not row:
            return jsonify({
                "candles": [],
                "error": (
                    f"No security_id found for {underlying} {strike} {option_type}"
                    + (f" expiry {expiry}" if expiry else "")
                    + ". Try Refresh Instruments or import trades for this option."
                ),
            })
        security_id      = row["security_id"]
        exchange_segment = row["exchange_segment"]

    # Dhan supports 1, 5, 15, 25, 60 natively; 3m = fetch 1m and aggregate
    fetch_interval = 1 if interval == 3 else interval
    candles, err = _fetch_option_candles(
        security_id, exchange_segment, from_date, to_date, fetch_interval
    )
    if err:
        return jsonify({"candles": [], "error": f"Dhan error: {err}"})

    if interval == 3:
        candles = _aggregate_candles(candles, 3)

    logger.info("Option candles: %s %s %s %s→%s ivl=%dm → %d candles",
                underlying, strike, option_type, from_date, to_date, interval, len(candles))
    return jsonify({"candles": candles, "error": "", "security_id": security_id,
                    "exchange_segment": exchange_segment})


@app.route("/api/tick-subscribe", methods=["POST"])
def api_tick_subscribe():
    pairs = []
    for item in (request.json or {}).get("instruments") or []:
        sid  = str(item.get("security_id") or "")
        seg  = str(item.get("exchange_segment") or "NSE_FNO")
        if sid:
            pairs.append((sid, seg))
    added = subscribe_ticks(pairs)
    return jsonify({"ok": True, "added": added, "total": len(_tick_subscribed)})


@app.route("/api/tick-candles")
def api_tick_candles():
    security_id = request.args.get("security_id") or ""
    try:
        seconds = int(request.args.get("seconds") or 15)
    except ValueError:
        seconds = 15
    trade_date = request.args.get("date") or str(date.today())
    if not security_id:
        return jsonify({"candles": [], "error": "security_id required"}), 400
    # Day boundaries as IST-as-UTC epoch (same convention as stored ticks)
    try:
        dp = [int(x) for x in trade_date.split("-")]
        day_start = int(datetime(dp[0], dp[1], dp[2], 9, 15, 0).timestamp()) + 19800
        day_end   = int(datetime(dp[0], dp[1], dp[2], 15, 30, 0).timestamp()) + 19800
    except Exception:
        return jsonify({"candles": [], "error": "Invalid date"}), 400
    rows = get_db().execute(
        "SELECT ts, price FROM tick_data WHERE security_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (security_id, day_start, day_end),
    ).fetchall()
    if not rows:
        return jsonify({
            "candles": [],
            "error": "No tick data for this instrument. Import today's trades to start capturing.",
        })
    # Aggregate ticks into N-second OHLCV candles
    candles: list[dict] = []
    b_time = b_o = b_h = b_l = b_c = None
    for row in rows:
        ts    = row["ts"]
        price = row["price"]
        bkt   = (ts // seconds) * seconds
        if bkt != b_time:
            if b_time is not None:
                candles.append({"time": b_time, "open": b_o, "high": b_h,
                                "low": b_l, "close": b_c})
            b_time = bkt
            b_o = b_h = b_l = b_c = price
        else:
            if price > b_h: b_h = price
            if price < b_l: b_l = price
            b_c = price
    if b_time is not None:
        candles.append({"time": b_time, "open": b_o, "high": b_h,
                        "low": b_l, "close": b_c})
    logger.info("Tick candles: %s %s %ds → %d candles", security_id, trade_date, seconds, len(candles))
    return jsonify({"candles": candles, "error": ""})


@app.route("/api/atm-ladder")
def api_atm_ladder():
    """Return NIFTY ATM ±5 strike info + full spot series for a date."""
    trade_date = request.args.get("date") or str(date.today())
    spot, spot_series, err = _get_nifty_spot_for_ladder(trade_date)
    if err:
        return jsonify({"error": err, "spot": 0, "atm": 0, "strikes": [], "spot_series": [], "date": trade_date})
    atm = round(spot / 50) * 50
    strikes = []
    for i in range(5, -6, -1):
        if i == 0:
            offset = "ATM"
        elif i > 0:
            offset = f"ATM+{i}"
        else:
            offset = f"ATM{i}"   # e.g. "ATM-1"
        strikes.append({"offset": offset, "strike": int(atm + i * 50)})
    # Get nearest expiry for the ATM date from Dhan's expiry_list
    nearest_expiry = ""
    try:
        dhan_exp = _dhan_client()
        exp_resp = dhan_exp.expiry_list(13, "IDX_I")
        expiries = sorted((exp_resp.get("data") or {}).get("data") or [])
        if expiries:
            if trade_date < expiries[0]:
                # Past date: expiry_list only returns future dates, so derive the correct
                # past expiry from the weekday pattern of the nearest future expiry
                from datetime import datetime as _dt, timedelta as _td
                first_exp = _dt.strptime(expiries[0], "%Y-%m-%d").date()
                exp_weekday = first_exp.weekday()
                td_obj = _dt.strptime(trade_date, "%Y-%m-%d").date()
                days_ahead = (exp_weekday - td_obj.weekday()) % 7
                nearest_expiry = str(td_obj + _td(days=days_ahead))
            else:
                nearest_expiry = next((e for e in expiries if e >= trade_date), expiries[-1])
    except Exception:
        pass

    return jsonify({
        "date":        trade_date,
        "spot":        round(spot, 2),
        "atm":         int(atm),
        "strikes":     strikes,
        "spot_series": spot_series,
        "expiry":      nearest_expiry,
        "error":       "",
    })


@app.route("/api/rolling-candles")
def api_rolling_candles():
    """Return OHLCV candles for a NIFTY ATM-relative strike via the rolling/expired options API.

    Params: date (to_date), from_date (optional, defaults to date), offset, option_type, interval.
    """
    trade_date  = request.args.get("date")        or str(date.today())
    from_date_p = request.args.get("from_date")   or trade_date
    offset      = request.args.get("offset")      or "ATM"
    option_type = (request.args.get("option_type") or "CE").upper()
    try:
        interval = int(request.args.get("interval") or 1)
    except ValueError:
        interval = 1
    candles, err = _fetch_rolling_candles_data(trade_date, offset, option_type, interval, from_date=from_date_p)
    if err:
        return jsonify({"candles": [], "error": f"Dhan error: {err}"})
    if not candles:
        return jsonify({"candles": [], "error": "No data — date may be outside Dhan's rolling window (~30 days)"})
    return jsonify({"candles": candles, "error": ""})


@app.route("/api/debug-rolling")
def api_debug_rolling():
    """Return the raw Dhan rolling options API response for diagnosis."""
    trade_date  = request.args.get("date") or str(date.today())
    strike      = request.args.get("strike") or "ATM"
    option_type = (request.args.get("option_type") or "CALL").upper()
    req_data    = request.args.get("req_data") or "spot"
    try:
        dhan = _dhan_client()
        resp = _with_timeout(
            dhan.expired_options_data,
            security_id="13",
            exchange_segment="NSE_FNO",
            instrument_type="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,   # 0 is treated as "not provided" by Dhan; 1 = nearest expiry
            strike=strike,
            drv_option_type=option_type,
            required_data=req_data.split(","),
            from_date=trade_date,
            to_date=trade_date,
            interval=1,
        )
        return jsonify({"ok": True, "raw": resp})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/option-ladder")
def option_ladder_page():
    return _option_ladder_page()


def _option_ladder_page() -> str:
    today = str(date.today())
    ver   = APP_VERSION
    # Generate time select options 09:15 → 15:30 in 15-min steps
    h, m = 9, 15
    time_opts = ""
    while h < 15 or (h == 15 and m <= 30):
        time_opts += f'<option value="{h:02d}:{m:02d}">{h:02d}:{m:02d}</option>'
        m += 15
        if m >= 60:
            m = 0
            h += 1
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ATM Ladder — Trade Analyser {ver}</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{height:100%;overflow:hidden;}}
body{{background:#0d0d0d;color:#ccc;font:13px/1.4 'Segoe UI',sans-serif;display:flex;flex-direction:row;}}
#leftPanel{{flex:0 0 272px;display:flex;flex-direction:column;border-right:1px solid #1a1a1a;overflow:hidden;}}
#lp-nav{{display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid #1a1a1a;flex-shrink:0;flex-wrap:wrap;}}
#lp-nav a{{color:#555;text-decoration:none;font-size:11px;}}
#lp-nav a:hover{{color:#aaa;}}
.htitle{{font-weight:600;font-size:13px;color:#bbb;}}
.badge{{font-size:10px;color:#444;border:1px solid #222;padding:1px 4px;border-radius:2px;}}
#lp-ctrl{{padding:8px 10px;border-bottom:1px solid #1a1a1a;flex-shrink:0;}}
.ctrl-row{{display:flex;gap:4px;align-items:center;margin-bottom:6px;}}
.ctrl-row:last-child{{margin-bottom:0;}}
.arrbtn{{background:#111;border:1px solid #222;color:#666;padding:2px 7px;border-radius:3px;cursor:pointer;font-size:12px;}}
.arrbtn:hover{{color:#aaa;border-color:#444;}}
#dateIn{{flex:1;background:#111;border:1px solid #2a2a2a;color:#ccc;padding:3px 6px;border-radius:3px;font-size:12px;}}
#timeIn{{flex:1;background:#111;border:1px solid #2a2a2a;color:#ccc;padding:3px 6px;border-radius:3px;font-size:12px;cursor:pointer;}}
.time-lbl{{font-size:11px;color:#555;white-space:nowrap;}}
.ldbtn{{width:100%;background:#1a3a1a;border:1px solid #2a5a2a;color:#4fc3f7;padding:5px;border-radius:3px;cursor:pointer;font-size:12px;font-weight:600;}}
.ldbtn:hover{{background:#1e4a1e;}}
#spotInfo{{font-size:11px;color:#888;min-height:15px;line-height:1.3;word-break:break-word;}}
.ibtn{{background:#111;border:1px solid #2a2a2a;color:#888;padding:2px 8px;border-radius:3px;cursor:pointer;font-size:11px;}}
.ibtn.on{{background:#112211;border-color:#2a5a2a;color:#4fc3f7;}}
#lp-body{{flex:1;overflow-y:auto;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
thead th{{position:sticky;top:0;background:#080808;color:#444;font-weight:500;padding:4px 6px;text-align:center;border-bottom:1px solid #1a1a1a;font-size:11px;}}
#ladderBody td{{padding:5px 6px;border-bottom:1px solid #0e0e0e;text-align:center;white-space:nowrap;}}
.ce-td{{color:#4fc3f7;cursor:pointer;border-radius:3px;}}
.ce-td:hover{{background:rgba(79,195,247,.12);}}
.ce-td.sel{{background:rgba(79,195,247,.2);font-weight:700;}}
.pe-td{{color:#ffb74d;cursor:pointer;border-radius:3px;}}
.pe-td:hover{{background:rgba(255,183,77,.12);}}
.pe-td.sel{{background:rgba(255,183,77,.2);font-weight:700;}}
.atm-row td{{background:#111;}}
.off-col{{color:#3a3a3a;font-size:10px;text-align:right;padding-right:4px;}}
.sk-col{{font-weight:600;color:#999;}}
.atm-row .sk-col{{color:#ddd;}}
#rightPanel{{flex:1;position:relative;overflow:hidden;}}
#chartEl{{position:absolute;inset:0;width:100%;height:100%;}}
#chartTitle{{position:absolute;top:8px;left:10px;font-size:12px;font-weight:500;color:#C3BCDB;pointer-events:none;z-index:2;white-space:nowrap;}}
#msgEl{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#555;font-size:13px;text-align:center;pointer-events:none;z-index:3;}}
#errBanner{{position:absolute;bottom:10px;left:50%;transform:translateX(-50%);background:#2a1010;border:1px solid #4a2020;color:#f85149;font-size:12px;padding:6px 14px;border-radius:4px;z-index:5;display:none;max-width:80%;text-align:center;}}
::-webkit-scrollbar{{width:4px;height:4px;}}
::-webkit-scrollbar-thumb{{background:#222;border-radius:2px;}}
</style>
</head>
<body>
<div id="leftPanel">
  <div id="lp-nav">
    <a href="/">&#8592; Main</a>
    <a href="/option-chart">Option Chart</a>
    <span class="htitle">ATM Ladder</span>
    <span class="badge">{ver}</span>
  </div>
  <div id="lp-ctrl">
    <div class="ctrl-row">
      <button class="arrbtn" onclick="shiftDay(-1)">&#9664;</button>
      <input type="date" id="dateIn" value="{today}">
      <button class="arrbtn" onclick="shiftDay(1)">&#9654;</button>
    </div>
    <div class="ctrl-row">
      <span class="time-lbl">@ IST</span>
      <select id="timeIn" onchange="updateAtmForTime()">{time_opts}</select>
    </div>
    <div class="ctrl-row">
      <button class="ldbtn" onclick="loadLadder()">&#9654; Load ATM</button>
    </div>
    <div id="spotInfo"></div>
    <div class="ctrl-row" style="margin-top:7px;">
      <button class="ibtn on" id="ivl1"  onclick="setIvl(1)">1m</button>
      <button class="ibtn"    id="ivl5"  onclick="setIvl(5)">5m</button>
      <button class="ibtn"    id="ivl15" onclick="setIvl(15)">15m</button>
    </div>
    <div class="ctrl-row" style="margin-top:5px;">
      <button class="ibtn" id="refBtn" onclick="refreshInstruments()" style="flex:1;font-size:10px;padding:3px 6px;">&#8635; Refresh Instruments</button>
    </div>
  </div>
  <div id="lp-body">
    <table>
      <thead><tr><th>Offset</th><th>Strike</th><th>CE</th><th>PE</th></tr></thead>
      <tbody id="ladderBody">
        <tr><td colspan="4" style="color:#333;padding:28px 10px;text-align:center;font-size:11px;">Pick a date and click &#9654; Load ATM</td></tr>
      </tbody>
    </table>
  </div>
</div>
<div id="rightPanel">
  <div id="chartTitle">Load a date, then click a CE or PE strike to view its chart</div>
  <div id="chartEl"></div>
  <div id="msgEl">Select a strike to load its chart</div>
  <div id="errBanner"></div>
</div>
<script>
var _chart=null,_series=null,_markersPlugin=null,_curIvl=1;
var _ema20s=null,_ema50s=null;
var _tradeDates=[],_atmData=null,_selOffset=null,_selType=null,_selStrike=null;
var _spotSeries=[];
var _niftyExpiry='';
var _niftyExpiryForDate='';  // date for which _niftyExpiry was last computed
var TODAY='{today}';

(function initChart(){{
  try{{
    var el=document.getElementById('chartEl');
    _chart=LightweightCharts.createChart(el,{{
      layout:{{background:{{color:'#0d0d0d'}},textColor:'#aaa'}},
      grid:{{vertLines:{{color:'#1a1a1a'}},horzLines:{{color:'#1a1a1a'}}}},
      crosshair:{{mode:0}},
      rightPriceScale:{{borderColor:'#2a2a2a'}},
      timeScale:{{borderColor:'#2a2a2a',timeVisible:true,secondsVisible:false}},
    }});
    _series=_chart.addSeries(LightweightCharts.CandlestickSeries,{{
      upColor:'#3fb950',downColor:'#f85149',
      borderUpColor:'#3fb950',borderDownColor:'#f85149',
      wickUpColor:'#3fb950',wickDownColor:'#f85149',
    }});
    _markersPlugin=LightweightCharts.createSeriesMarkers(_series,[]);
    _ema20s=_chart.addSeries(LightweightCharts.LineSeries,{{
      color:'#2196F3',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false
    }});
    _ema50s=_chart.addSeries(LightweightCharts.LineSeries,{{
      color:'#FF9800',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false
    }});
    new ResizeObserver(function(){{_chart.resize(el.offsetWidth,el.offsetHeight);}}).observe(el);
  }}catch(e){{console.error('Chart init:',e);}}
}})();

function _emaArr(closes,p){{
  var out=new Array(closes.length).fill(null);
  if(closes.length<p)return out;
  var s=0;for(var j=0;j<p;j++)s+=closes[j];
  out[p-1]=s/p;var k=2/(p+1);
  for(var i=p;i<closes.length;i++)out[i]=closes[i]*k+out[i-1]*(1-k);
  return out;
}}
function updateIndicators(data){{
  if(!data.length||!_ema20s)return;
  var closes=data.map(function(c){{return c.close;}}),times=data.map(function(c){{return c.time;}});
  function toS(arr){{var o=[];for(var i=0;i<arr.length;i++)if(arr[i]!==null)o.push({{time:times[i],value:arr[i]}});return o;}}
  _ema20s.setData(toS(_emaArr(closes,20)));
  _ema50s.setData(toS(_emaArr(closes,50)));
}}

function showMsg(m){{var e=document.getElementById('msgEl');e.style.display=m?'':'none';e.textContent=m;}}
function hideMsg(){{document.getElementById('msgEl').style.display='none';}}
function showErr(m){{var e=document.getElementById('errBanner');e.style.display=m?'block':'none';e.textContent=m||'';}}

function setIvl(n){{
  _curIvl=n;
  [1,5,15].forEach(function(v){{
    var b=document.getElementById('ivl'+v);if(b)b.className='ibtn'+(v===n?' on':'');
  }});
  if(_selOffset&&_selType)loadChart(_selOffset,_selType);
}}

async function loadDates(){{
  try{{var r=await fetch('/api/dates');_tradeDates=await r.json();}}catch(e){{}}
}}
function shiftDay(dir){{
  var cur=document.getElementById('dateIn').value;
  if(_tradeDates.length){{
    var idx=_tradeDates.indexOf(cur),next;
    if(dir<0){{next=idx<0?_tradeDates[0]:_tradeDates[Math.min(idx+1,_tradeDates.length-1)];}}
    else     {{next=idx<0?_tradeDates[_tradeDates.length-1]:_tradeDates[Math.max(idx-1,0)];}}
    if(next){{document.getElementById('dateIn').value=next;loadLadder();return;}}
  }}
  if(!cur)return;
  var p=cur.split('-');
  var dt=new Date(Date.UTC(+p[0],+p[1]-1,+p[2]));
  dt.setUTCDate(dt.getUTCDate()+dir);
  document.getElementById('dateIn').value=dt.toISOString().slice(0,10);
  loadLadder();
}}

function _spotAtTime(timeVal){{
  if(!_spotSeries.length)return null;
  var dp=document.getElementById('dateIn').value.split('-');
  var tp=timeVal.split(':');
  var target=Date.UTC(+dp[0],+dp[1]-1,+dp[2],+tp[0],+tp[1],0)/1000;
  var best=_spotSeries[0],bestD=Math.abs(_spotSeries[0].ts-target);
  for(var i=1;i<_spotSeries.length;i++){{
    var d=Math.abs(_spotSeries[i].ts-target);
    if(d<bestD){{bestD=d;best=_spotSeries[i];}}
  }}
  return best;
}}

function _buildStrikes(atm){{
  var s=[];
  for(var i=5;i>=-5;i--){{
    var off=i===0?'ATM':(i>0?'ATM+'+i:'ATM'+i);
    s.push({{offset:off,strike:atm+i*50}});
  }}
  return s;
}}


function updateAtmForTime(){{
  var tv=document.getElementById('timeIn').value;
  var entry=_spotAtTime(tv);
  if(!entry)return;
  var atm=Math.round(entry.spot/50)*50;
  document.getElementById('spotInfo').textContent=
    'NIFTY ≈'+Math.round(entry.spot)+' @ '+tv+' → ATM '+atm;
  var strikes=_buildStrikes(atm);
  if(_atmData){{_atmData.atm=atm;_atmData.spot=entry.spot;_atmData.strikes=strikes;}}
  renderLadder(strikes);
}}

async function loadLadder(){{
  var d=document.getElementById('dateIn').value;
  document.getElementById('spotInfo').textContent='Loading…';
  var tbody=document.getElementById('ladderBody');
  tbody.innerHTML='<tr><td colspan="4" style="text-align:center;color:#444;padding:20px;font-size:11px;">Fetching NIFTY spot…</td></tr>';
  _selOffset=null;_selType=null;
  try{{
    var r=await fetch('/api/atm-ladder?date='+d);
    var data=await r.json();
    if(data.error){{
      document.getElementById('spotInfo').textContent='';
      tbody.innerHTML='<tr><td colspan="4" style="text-align:center;color:#f85149;padding:16px;font-size:11px;">'+data.error+'</td></tr>';
      return;
    }}
    _atmData=data;
    _spotSeries=data.spot_series||[];
    _niftyExpiry=data.expiry||'';
    _niftyExpiryForDate=d;
    // Proactively warm instrument cache for this expiry (background, non-blocking)
    if(_niftyExpiry){{
      fetch('/api/refresh-instruments?expiry='+encodeURIComponent(_niftyExpiry),{{method:'POST'}})
        .then(function(r){{return r.json();}})
        .then(function(rr){{
          if(rr.ok&&rr.count>0){{
            var rb=document.getElementById('refBtn');
            if(rb){{rb.textContent='✓ '+rr.count+' cached';setTimeout(function(){{rb.textContent='↻ Refresh Instruments';}},3000);}}
          }}
        }}).catch(function(){{}});
    }}
    if(_spotSeries.length){{
      updateAtmForTime();
    }}else{{
      var atm=Math.round(data.spot/50)*50;
      document.getElementById('spotInfo').textContent=
        'NIFTY ≈'+Math.round(data.spot)+' → ATM '+atm;
      renderLadder(_buildStrikes(atm));
    }}
  }}catch(e){{
    document.getElementById('spotInfo').textContent='';
    tbody.innerHTML='<tr><td colspan="4" style="text-align:center;color:#f85149;padding:16px;font-size:11px;">Error: '+e.message+'</td></tr>';
  }}
}}

function renderLadder(strikes){{
  var tbody=document.getElementById('ladderBody');
  tbody.innerHTML='';
  strikes.forEach(function(s){{
    var tr=document.createElement('tr');
    var isAtm=s.offset==='ATM';
    if(isAtm)tr.className='atm-row';
    tr.innerHTML=
      '<td class="off-col">'+s.offset+'</td>'+
      '<td class="sk-col">'+s.strike+'</td>'+
      '<td class="ce-td">CE</td>'+
      '<td class="pe-td">PE</td>';
    var ceTd=tr.querySelector('.ce-td');
    var peTd=tr.querySelector('.pe-td');
    ceTd.dataset.offset=s.offset;
    peTd.dataset.offset=s.offset;
    ceTd.dataset.strike=s.strike;
    peTd.dataset.strike=s.strike;
    ceTd.onclick=(function(off,sk){{return function(){{loadChart(off,'CE',sk);}};}})(s.offset,s.strike);
    peTd.onclick=(function(off,sk){{return function(){{loadChart(off,'PE',sk);}};}})(s.offset,s.strike);
    tbody.appendChild(tr);
  }});
  // Restore selection highlight
  if(_selOffset&&_selType){{
    var selCls=_selType==='CE'?'.ce-td':'.pe-td';
    document.querySelectorAll(selCls).forEach(function(td){{
      if(td.dataset.offset===_selOffset)td.classList.add('sel');
    }});
  }}
}}

async function loadChart(offset,optType,strike){{
  _selOffset=offset;_selType=optType;_selStrike=strike||0;
  document.querySelectorAll('.ce-td,.pe-td').forEach(function(td){{td.classList.remove('sel');}});
  var selCls=optType==='CE'?'.ce-td':'.pe-td';
  document.querySelectorAll(selCls).forEach(function(td){{
    if(td.dataset.offset===offset)td.classList.add('sel');
  }});
  var date=document.getElementById('dateIn').value;
  showMsg('Loading '+offset+' '+optType+'…');showErr('');
  try{{
    if(!strike){{hideMsg();showErr('ATM not loaded — click Load ATM first');return;}}
    // 7 calendar days back covers the full expiry week (~5 trading days)
    var dp0=date.split('-');
    var fromDt=new Date(Date.UTC(+dp0[0],+dp0[1]-1,+dp0[2]));
    fromDt.setUTCDate(fromDt.getUTCDate()-7);
    var fromDate=fromDt.toISOString().slice(0,10);

    // PRIMARY: rolling expired_options_data — works for any past option, no security_id needed
    showMsg('Loading '+offset+' '+optType+' chart…');
    var rollResp=await fetch('/api/rolling-candles?offset='+encodeURIComponent(offset)
      +'&option_type='+optType+'&from_date='+fromDate+'&date='+date+'&interval='+_curIvl);
    var roll=await rollResp.json();
    if(roll.candles&&roll.candles.length>0){{
      _series.setData(roll.candles);
      updateIndicators(roll.candles);
      _markersPlugin.setMarkers([]);
      _chart.timeScale().fitContent();
      hideMsg();
      // Get expiry for display (from cache or skip)
      var expDisp=(_niftyExpiryForDate===date&&_niftyExpiry)?_niftyExpiry:'';
      document.getElementById('chartTitle').textContent=
        'NIFTY '+offset+' (≈'+strike+') '+optType
        +(expDisp?' \xb7 exp '+expDisp:'')
        +' \xb7 '+fromDate+' → '+date+' \xb7 '+_curIvl+'m \xb7 '+roll.candles.length+' bars';
      return;
    }}

    // FALLBACK: specific security_id path (for options with known ID from trades/cache)
    var expiry=(_niftyExpiryForDate===date)?_niftyExpiry:'';
    if(!expiry){{
      try{{
        var er=await(await fetch('/api/nifty-expiry?date='+date)).json();
        expiry=er.expiry||'';
        if(expiry){{_niftyExpiry=expiry;_niftyExpiryForDate=date;}}
      }}catch(ef){{}}
    }}
    if(expiry){{
      var url='/api/option-candles?underlying=NIFTY'
        +'&option_type='+optType+'&strike='+strike+'&expiry='+expiry
        +'&from_date='+fromDate+'&to_date='+date+'&interval='+_curIvl;
      var d=await(await fetch(url+'&t='+Date.now())).json();
      if(!d.error&&d.candles&&d.candles.length>0){{
        _series.setData(d.candles);
        updateIndicators(d.candles);
        _markersPlugin.setMarkers([]);
        _chart.timeScale().fitContent();
        hideMsg();
        document.getElementById('chartTitle').textContent=
          'NIFTY '+strike+' '+optType+' \xb7 exp '+expiry+' \xb7 '+fromDate+' → '+date+' \xb7 '+_curIvl+'m \xb7 '+d.candles.length+' bars';
        return;
      }}
    }}

    hideMsg();
    showErr('No chart data for '+offset+' '+optType+' on '+date+' — date may be outside Dhan\'s ~30 day window');
  }}catch(e){{hideMsg();showErr('Error: '+e.message);}}
}}

async function refreshInstruments(){{
  var btn=document.getElementById('refBtn');
  if(btn){{btn.textContent='Refreshing…';btn.disabled=true;}}
  showErr('');
  try{{
    var r=await fetch('/api/refresh-instruments',{{method:'POST'}});
    var d=await r.json();
    if(btn){{btn.disabled=false;}}
    if(d.ok){{
      if(btn){{
        btn.textContent='✓ '+d.count+' instruments';
        setTimeout(function(){{if(btn)btn.textContent='↻ Refresh Instruments';}},4000);
      }}
    }}else{{
      if(btn)btn.textContent='↻ Refresh Instruments';
      showErr('Refresh failed: '+(d.error||'unknown error')+'. Check VPS network — needs to reach images.dhan.co');
    }}
  }}catch(e){{
    if(btn){{btn.textContent='↻ Refresh Instruments';btn.disabled=false;}}
    showErr('Refresh error: '+e.message);
  }}
}}

loadDates().then(function(){{
  var d=_tradeDates.length?_tradeDates[0]:TODAY;
  document.getElementById('dateIn').value=d;
  loadLadder();
}});
</script>
</body>
</html>"""


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
#autoImpStatus {{ font-size: 10px; color: #555; margin-left: 4px; white-space: nowrap }}
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
  <span id="autoImpStatus"></span>
  <a href="/option-chart" class="hbtn" style="text-decoration:none">&#128202; Option Chart</a>
  <a href="/option-expiry" class="hbtn" style="text-decoration:none">&#128269; Historical</a>
  <a href="/option-ladder" class="hbtn" style="text-decoration:none">&#128693; ATM Ladder</a>
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

function autoImport(){{
  var st=document.getElementById('autoImpStatus');
  if(st) st.textContent='[..]';
  var now=new Date(Date.now()+19800000);
  var today=now.toISOString().slice(0,10);
  fetch('/api/import',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{from_date:today,to_date:today}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      var ts=new Date(Date.now()+19800000).toISOString().slice(11,16)+' IST';
      if(st) st.textContent=(d.imported>0?'+'+d.imported+' ':'')+ts;
      if(d.imported>0){{
        var now2=new Date(Date.now()+19800000);
        var today2=now2.toISOString().slice(0,10);
        if(curDate===today2) loadTrades();
      }}
    }})
    .catch(function(){{if(st) st.textContent='err';}});
}}

window.addEventListener('DOMContentLoaded', function() {{
  initChart();
  _updatePaneBtns();
  var now=new Date(Date.now()+19800000); // UTC+5:30 IST offset
  var today=now.toISOString().slice(0,10);
  document.getElementById('dp').value=today;
  curDate=today;
  loadAll();
  setTimeout(autoImport, 5000);
  setInterval(autoImport, 120000);
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
      // Re-place markers now that candles are loaded — loadTrades() may have
      // run first (it's a fast DB call) and snapped to stale/empty candles.
      putMarkers(_filtered());
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
      '<td style="white-space:nowrap">'+(t.entry_time?t.entry_time.slice(0,8):'--')+(t.exit_time?' <span style="color:#444">&#8594;</span> '+t.exit_time.slice(0,8):'')+'</td>' +
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
function fp(v){{return v!=null?v.toFixed(1):'—';}}
function putMarkers(trades){{
  if(!series)return;
  var list=isolateId!==null?trades.filter(function(t){{return t.id===isolateId;}}):trades;
  var markers=[];
  for(var i=0;i<list.length;i++){{
    var t=list[i];
    var col=t.option_type==='CE'?'#4fc3f7':'#ffb74d';
    var ets=tsFor(curDate,t.entry_time);
    var lbl=t.option_type+' '+(t.strike?t.strike.toLocaleString('en-IN'):'');
    if(ets)markers.push({{time:snapTs(ets),position:'aboveBar',color:col,shape:'arrowDown',text:'E '+lbl,id:'e'+t.id}});
    if(t.exit_time&&t.exit_price!=null){{
      var xts=tsFor(curDate,t.exit_time);
      if(xts)markers.push({{
        time:snapTs(xts),position:'belowBar',
        color:(t.pnl!=null&&t.pnl>=0)?'#4caf50':'#ef5350',
        shape:'arrowUp',
        text:'X '+(t.pnl!=null?(t.pnl>=0?'+':'')+Math.round(t.pnl):t.exit_price.toFixed(0)),
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
  var now=new Date(Date.now()+19800000); // IST
  var today=now.toISOString().slice(0,10);
  var ago30=new Date(Date.now()+19800000-30*86400000).toISOString().slice(0,10);
  document.getElementById('mFrom').value=today;
  document.getElementById('mTo').value=today;
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
    # Start background scheduler for mid-session auto-imports (enables 15s tick data)
    threading.Thread(target=_auto_import_scheduler, daemon=True, name="auto-import").start()
    logger.info("Auto-import scheduler started (triggers at 10:00, 12:00, 14:00 IST)")
    # Resume tick feed for today's already-imported trades (handles app restarts)
    try:
        today_str = str(date.today())
        _startup_rows = get_db().execute(
            "SELECT DISTINCT security_id, exchange_segment FROM trades"
            " WHERE date=? AND security_id != '' AND security_id GLOB '[0-9]*'",
            (today_str,),
        ).fetchall()
        if _startup_rows:
            n = subscribe_ticks([(r["security_id"], r["exchange_segment"]) for r in _startup_rows])
            if n:
                logger.info("Startup: resumed tick feed for %d instruments (today's trades)", n)
    except Exception as _e:
        logger.warning("Startup tick resume failed: %s", _e)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
