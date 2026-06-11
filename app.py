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
from collections import defaultdict
from datetime import date, datetime, timedelta

import yfinance as yf
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────

PORT    = int(os.getenv("PORT", "5556"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyser.db")

LOT_SIZES = {
    "NIFTY": 75, "SENSEX": 10,
    "FINNIFTY": 40, "MIDCPNIFTY": 50,
}

DHAN_INDEX_IDS = {
    "NIFTY":  {"security_id": "13",  "exchange_segment": "NSE_EQ", "instrument_type": "INDEX"},
    "SENSEX": {"security_id": "51",  "exchange_segment": "BSE_EQ", "instrument_type": "INDEX"},
}

YF_TICKERS = {
    "NIFTY":  "^NSEI",
    "SENSEX": "^BSESN",
}

CHART_TIMEOUT = 10  # seconds for any external chart data call

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
                created_at      REAL    DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_date ON trades(date);
            CREATE INDEX IF NOT EXISTS idx_underlying ON trades(underlying);
        """)


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
    sym_ok = sym.endswith("CE") or sym.endswith("PE") or " CALL " in sym or " PUT " in sym
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
                        diag["history_raw_p0"] = str(resp)[:400]
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
            raw.extend(tb_batch)
        except Exception as e:
            logger.warning("get_trade_book failed: %s", e)
            diag["tradebook_error"] = str(e)

    seen: set = set()
    deduped = []
    for t in raw:
        key = (t.get("orderId") or "", t.get("exchangeTradeId") or t.get("exchangeOrderId") or "")
        if key not in seen:
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
        ts = t.get("createTime") or t.get("exchangeTime") or t.get("orderCreateTime") or ""
        sid = str(t.get("securityId") or t.get("security_id") or "")
        trade_date = ts[:10] if len(ts) >= 10 else today_str
        groups[(trade_date, sid)].append(t)

    imported = skipped = 0
    db = get_db()

    for (trade_date, sid), group in groups.items():
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

            exit_t = next(
                (
                    b for b in buys
                    if int(b.get("tradedQuantity") or b.get("quantity") or 0) == qty
                    and (b.get("createTime") or b.get("orderCreateTime") or "") > ts_str
                ),
                None,
            )
            if exit_t:
                buys.remove(exit_t)

            exit_ts    = (exit_t.get("createTime") or exit_t.get("exchangeTime") or "") if exit_t else ""
            exit_time  = exit_ts[11:19] if len(exit_ts) >= 19 else ""
            exit_price = float(exit_t.get("tradedPrice") or exit_t.get("price") or 0) if exit_t else None
            pnl        = round((entry_price - (exit_price or 0)) * qty, 2) if exit_price is not None else None
            status     = "CLOSED" if exit_t else "OPEN"

            with _db_lock:
                if db.execute(
                    "SELECT id FROM trades WHERE date=? AND security_id=? AND entry_time=? AND dhan_order_id=?",
                    (trade_date, sid, entry_time, order_id),
                ).fetchone():
                    skipped += 1
                    continue

                db.execute(
                    """
                    INSERT INTO trades
                        (date, underlying, option_type, strike, expiry,
                         entry_time, entry_price, exit_time, exit_price,
                         quantity, lot_size, lots, pnl, status,
                         security_id, exchange_segment, dhan_order_id, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        trade_date, underlying, opt_type, strike, expiry,
                        entry_time, entry_price, exit_time, exit_price,
                        qty, lot_size, lots, pnl, status,
                        sid, exseg, order_id, datetime.now().timestamp(),
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
    """Run fn(*args, **kwargs) with a 10s socket timeout."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(CHART_TIMEOUT)
    try:
        return fn(*args, **kwargs)
    finally:
        socket.setdefaulttimeout(old)


def _is_auth_error(resp) -> bool:
    """Detect expired-token response from Dhan API."""
    if not isinstance(resp, dict):
        return False
    status = (resp.get("status") or "").lower()
    if status not in ("failure", "error", "fail"):
        return False
    remarks = str(resp.get("remarks") or resp.get("message") or "").lower()
    return any(w in remarks for w in ("unauthorized", "token", "401", "auth", "access"))


def _raw_dhan_chart(security_id: str, exchange_segment: str,
                    instrument_type: str, trade_date: str) -> tuple[dict, str]:
    """Call intraday_minute_data; tries with date params, falls back without."""
    try:
        dhan = _dhan_client()
    except Exception as e:
        return {}, str(e)

    def _call(d):
        try:
            return _with_timeout(
                d.intraday_minute_data,
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=trade_date,
                to_date=trade_date,
            ), ""
        except TypeError:
            # SDK version that doesn't accept date params
            return _with_timeout(
                d.intraday_minute_data,
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
            ), ""

    try:
        resp, err = _call(dhan)
    except Exception as e:
        return {}, str(e)

    # If token expired, refresh and retry once
    if _is_auth_error(resp):
        logger.info("Chart API auth error — refreshing token and retrying")
        try:
            import token_manager  # noqa: PLC0415
            if token_manager.refresh_token():
                dhan = _dhan_client()
                resp, err = _call(dhan)
        except Exception as e:
            logger.warning("Token refresh failed during chart load: %s", e)

    logger.info(
        "Dhan chart [%s %s %s %s]: type=%s status=%s keys=%s preview=%s",
        security_id, exchange_segment, instrument_type, trade_date,
        type(resp).__name__,
        resp.get("status") if isinstance(resp, dict) else "n/a",
        list(resp.keys()) if isinstance(resp, dict) else None,
        str(resp)[:300],
    )
    return resp, ""


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
    candles = []
    for i, ts_str in enumerate(timestamps):
        try:
            ts_str = str(ts_str).strip()
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


def _chart_from_yfinance(sym: str, trade_date: str) -> tuple[list[dict], str]:
    dt  = datetime.strptime(trade_date, "%Y-%m-%d")
    age = (datetime.now() - dt).days
    interval = "1m" if age <= 5 else ("5m" if age <= 55 else "1d")
    start = (dt - timedelta(days=3)).strftime("%Y-%m-%d")
    end   = (dt + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        df = _with_timeout(yf.Ticker(sym).history,
                           start=start, end=end,
                           interval=interval, auto_adjust=True)
    except Exception as e:
        logger.error("yfinance %s: %s", sym, e)
        return [], interval
    if df.empty:
        return [], interval
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    if interval in ("1m", "5m"):
        df = df[df.index.date == dt.date()]
    candles = []
    for ts, row in df.iterrows():
        o, h, l, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
        if any(v != v for v in (o, h, l, c)):
            continue
        candles.append({"time": int(ts.timestamp()),
                        "open": round(o, 2), "high": round(h, 2),
                        "low": round(l, 2), "close": round(c, 2)})
    return candles, interval


def chart_candles(underlying: str, trade_date: str) -> tuple[list[dict], str, str]:
    u = underlying.upper()
    idx = DHAN_INDEX_IDS.get(u)
    if idx:
        raw_resp, dhan_err = _raw_dhan_chart(
            idx["security_id"], idx["exchange_segment"],
            idx["instrument_type"], trade_date,
        )
        if dhan_err:
            logger.error("Dhan chart error: %s", dhan_err)
        else:
            candles = _parse_dhan_candles(raw_resp, trade_date)
            if candles:
                return candles, "1m", ""
            logger.info("Dhan chart 0 candles for %s %s. Check analyser.log for raw response.", u, trade_date)
    sym = YF_TICKERS.get(u)
    if sym:
        candles, interval = _chart_from_yfinance(sym, trade_date)
        if candles:
            return candles, interval, ""
    return [], "1m", (
        f"No chart data for {u} {trade_date}. "
        "Check analyser.log or visit /api/debug-chart?underlying="+u+"&date="+trade_date
    )


def _make_test_candles(trade_date: str) -> list[dict]:
    """Generate fake 1-minute NIFTY-like candles for testing JS chart rendering."""
    dt = datetime.strptime(trade_date, "%Y-%m-%d")
    base_time = datetime(dt.year, dt.month, dt.day, 9, 15, 0)
    price = 24500.0
    rng = random.Random(42)
    candles = []
    for i in range(375):  # 9:15–15:29
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


# ── Flask ───────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return _page()


@app.route("/api/test-chart")
def api_test_chart():
    """Returns fake candle data to verify the JS chart renders correctly."""
    d = request.args.get("date") or str(date.today())
    candles = _make_test_candles(d)
    return jsonify({"candles": candles, "interval": "1m", "error": ""})


@app.route("/api/debug-chart")
def api_debug_chart():
    """Diagnose chart: /api/debug-chart?underlying=NIFTY&date=YYYY-MM-DD"""
    u   = (request.args.get("underlying") or "NIFTY").upper()
    d   = request.args.get("date") or str(date.today())
    idx = DHAN_INDEX_IDS.get(u)
    if not idx:
        return jsonify({"ok": False, "error": f"No index config for {u}"})
    try:
        raw_resp, dhan_err = _raw_dhan_chart(
            idx["security_id"], idx["exchange_segment"],
            idx["instrument_type"], d,
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
    q, p = "SELECT * FROM trades WHERE date=?", [d]
    if u and u != "ALL":
        q += " AND underlying=?"; p.append(u)
    if ot and ot not in ("ALL", "BOTH", ""):
        q += " AND option_type=?"; p.append(ot)
    return jsonify([dict(r) for r in db.execute(q + " ORDER BY entry_time", p).fetchall()])


@app.route("/api/trade/<int:tid>/notes", methods=["PUT"])
def api_notes(tid: int):
    notes = (request.json or {}).get("notes", "")
    with _db_lock:
        db = get_db()
        db.execute("UPDATE trades SET notes=? WHERE id=?", (notes, tid))
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/chart")
def api_chart():
    u = request.args.get("underlying") or "NIFTY"
    d = request.args.get("date")       or str(date.today())
    candles, interval, err = chart_candles(u, d)
    return jsonify({"candles": candles, "interval": interval, "error": err})


@app.route("/api/dates")
def api_dates():
    rows = get_db().execute(
        "SELECT DISTINCT date FROM trades ORDER BY date DESC LIMIT 90"
    ).fetchall()
    return jsonify([r["date"] for r in rows])


# ── HTML page ─────────────────────────────────────────────────────────────────────────────────

def _page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Trade Analyser</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
:root {
  --bg: #0d0d0d; --surface: #141414; --s2: #1e1e1e; --border: #2a2a2a;
  --text: #e0e0e0; --dim: #555; --ce: #4fc3f7; --pe: #ffb74d;
  --green: #4caf50; --red: #ef5350; --acc: #7c4dff;
}
html, body { height: 100%; overflow: hidden }
body { display: flex; flex-direction: column; background: var(--bg); color: var(--text);
       font: 13px/1.4 'SF Mono', Consolas, monospace }
#bar { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
       background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0 }
#bar h1 { font-size: 13px; font-weight: 700; letter-spacing: 1.5px; margin-right: 4px; color: var(--acc) }
.date-nav { display: flex; gap: 4px; align-items: center }
.date-nav button { background: var(--s2); border: 1px solid var(--border); color: var(--text);
                   padding: 3px 9px; cursor: pointer; border-radius: 3px; font: inherit }
.date-nav button:hover { border-color: var(--acc) }
#dp { background: var(--s2); border: 1px solid var(--border); color: var(--text);
      padding: 3px 8px; border-radius: 3px; font: inherit; cursor: pointer }
.chips { display: flex; gap: 4px }
.chip { padding: 3px 11px; border-radius: 10px; border: 1px solid var(--border);
        color: var(--dim); cursor: pointer; font-size: 11px; user-select: none; transition: .15s }
.chip.on { color: var(--text); border-color: var(--acc); background: rgba(124,77,255,.12) }
.chip.ce.on { color: var(--ce); border-color: var(--ce); background: rgba(79,195,247,.1) }
.chip.pe.on { color: var(--pe); border-color: var(--pe); background: rgba(255,183,77,.1) }
#ivl { font-size: 10px; color: var(--dim); border: 1px solid var(--border); padding: 2px 6px; border-radius: 3px }
#impBtn { margin-left: auto; background: var(--acc); border: none; color: #fff;
          padding: 5px 14px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 12px }
#impBtn:hover { opacity: .85 }
#refreshBtn { background: var(--s2); border: 1px solid var(--border); color: var(--dim);
              padding: 5px 10px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 11px }
#refreshBtn:hover { border-color: var(--acc); color: var(--text) }
#sampleBtn { background: var(--s2); border: 1px solid var(--border); color: var(--dim);
             padding: 5px 10px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 11px }
#sampleBtn:hover { border-color: #4caf50; color: #4caf50 }
#main { flex: 1; display: flex; flex-direction: column; min-height: 0 }
#chartBox { flex: 1; min-height: 200px; position: relative }
#chartEl  { width: 100%; height: 100% }
#chartMsg { position: absolute; inset: 0; display: flex; align-items: center;
            justify-content: center; color: var(--dim); font-size: 12px;
            background: var(--bg); pointer-events: none; flex-direction: column; gap: 8px }
#chartMsg.hide { display: none }
#chartMsgSub { font-size: 10px; color: #333; max-width: 500px; text-align: center; word-break: break-all }
#panel { height: 235px; border-top: 1px solid var(--border);
         display: flex; flex-direction: column; background: var(--surface) }
#ph { display: flex; align-items: center; gap: 12px; padding: 6px 14px;
      border-bottom: 1px solid var(--border); flex-shrink: 0 }
#ph b { color: var(--dim); font-size: 10px; letter-spacing: 1px }
#psummary { margin-left: auto; font-size: 11px; color: var(--dim) }
#pbody { flex: 1; overflow-y: auto }
table { width: 100%; border-collapse: collapse; font-size: 12px }
th { position: sticky; top: 0; background: var(--s2); color: var(--dim); font-weight: 500;
     padding: 5px 12px; text-align: left; border-bottom: 1px solid var(--border) }
td { padding: 5px 12px; border-bottom: 1px solid rgba(255,255,255,.035) }
tr.sel td { background: rgba(124,77,255,.08) }
tr:not(.sel):hover td { background: rgba(255,255,255,.02) }
.tag { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 10px; font-weight: 700 }
.tag.ce { background: rgba(79,195,247,.12); color: var(--ce) }
.tag.pe { background: rgba(255,183,77,.12); color: var(--pe) }
.pos { color: var(--green) } .neg { color: var(--red) }
.ni { background: none; border: none; color: var(--text); font: inherit; width: 100%; outline: none }
.ni:focus { border-bottom: 1px solid var(--acc) }
.ni::placeholder { color: #333 }
#empty { text-align: center; color: var(--dim); padding: 36px; font-size: 12px }
#ov { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
      z-index: 99; align-items: center; justify-content: center }
#ov.show { display: flex }
#modal { background: var(--surface); border: 1px solid var(--border);
         border-radius: 8px; padding: 24px; width: 380px }
#modal h2 { font-size: 14px; margin-bottom: 16px }
.fr label { display: block; font-size: 11px; color: var(--dim); margin-bottom: 3px }
.fr input[type=date] { width: 100%; background: var(--s2); border: 1px solid var(--border);
                       color: var(--text); padding: 6px 9px; border-radius: 4px;
                       font: inherit; margin-bottom: 12px }
.mfooter { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px }
.btn  { padding: 6px 16px; border-radius: 4px; border: none; cursor: pointer; font: inherit; font-size: 12px }
.btnp { background: var(--acc); color: #fff }
.btns { background: var(--s2); color: var(--text); border: 1px solid var(--border) }
#mres { margin-top: 10px; font-size: 12px; min-height: 18px; word-break: break-word }
#mdiag { margin-top: 8px; font-size: 10px; color: var(--dim); max-height: 120px;
         overflow-y: auto; background: var(--s2); border-radius: 4px; padding: 6px 8px;
         white-space: pre-wrap; display: none }
::-webkit-scrollbar { width: 5px; height: 5px }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px }
</style>
</head>
<body>
<div id="bar">
  <h1>TRADE ANALYSER</h1>
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
  <span id="ivl">—</span>
  <button id="sampleBtn" onclick="loadSample()" title="Load fake candles to test chart rendering">Test Chart</button>
  <button id="refreshBtn" onclick="doRefreshToken()" title="Refresh Dhan token">&#8635; Token</button>
  <button id="impBtn" onclick="openImp()">&darr; Import from Dhan</button>
</div>
<div id="main">
  <div id="chartBox">
    <div id="chartEl"></div>
    <div id="chartMsg">
      <span id="chartMsgMain">Loading chart…</span>
      <span id="chartMsgSub"></span>
    </div>
  </div>
  <div id="panel">
    <div id="ph">
      <b>TRADES</b>
      <span id="pcnt" style="font-size:11px;color:var(--dim)"></span>
      <span id="psummary"></span>
    </div>
    <div id="pbody">
      <div id="empty">No trades for this date — import from Dhan or pick another day.</div>
      <table id="tbl" style="display:none">
        <thead><tr>
          <th>Time</th><th>Type</th><th>Strike</th>
          <th>Entry &#8377;</th><th>Exit &#8377;</th><th>Lots</th><th>P&amp;L</th><th>Notes</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </div>
</div>
<div id="ov" onclick="if(event.target===this)closeImp()">
  <div id="modal">
    <h2>Import Trades from Dhan</h2>
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
</div>
<script>
let chart, series;
let curDate = '', curU = 'NIFTY';
let typeOn  = new Set(['CE','PE']);
let allTrades = [], candles = [], curInterval = '1m';
let selId = null;
window.addEventListener('DOMContentLoaded', () => {
  if (typeof LightweightCharts === 'undefined') {
    setChartMsg('Chart library failed to load.', 'Check internet / CDN access, then refresh.');
    return;
  }
  initChart();
  const today = new Date().toISOString().slice(0,10);
  document.getElementById('dp').value = today;
  curDate = today;
  loadAll();
});
function setChartMsg(main, sub) {
  const el = document.getElementById('chartMsg');
  el.classList.remove('hide');
  document.getElementById('chartMsgMain').textContent = main || '';
  document.getElementById('chartMsgSub').textContent  = sub  || '';
}
function hideChartMsg() { document.getElementById('chartMsg').classList.add('hide'); }
function initChart() {
  chart = LightweightCharts.createChart(document.getElementById('chartEl'), {
    autoSize: true,
    layout: { background:{color:'#0d0d0d'}, textColor:'#555' },
    grid:   { vertLines:{color:'#181818'}, horzLines:{color:'#181818'} },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor:'#2a2a2a' },
    timeScale: { borderColor:'#2a2a2a', timeVisible:true, secondsVisible:false },
  });
  series = chart.addCandlestickSeries({
    upColor:'#26a69a', downColor:'#ef5350',
    borderUpColor:'#26a69a', borderDownColor:'#ef5350',
    wickUpColor:'#26a69a', wickDownColor:'#ef5350',
  });
}
function shiftDay(d) {
  const dt = new Date(curDate + 'T00:00:00');
  dt.setDate(dt.getDate() + d);
  curDate = dt.toISOString().slice(0,10);
  document.getElementById('dp').value = curDate;
  loadAll();
}
function onDate() { curDate = document.getElementById('dp').value; loadAll(); }
function setU(el) {
  document.querySelectorAll('#uChips .chip').forEach(c => c.classList.remove('on'));
  el.classList.add('on'); curU = el.dataset.v; loadAll();
}
function togT(el) {
  const v = el.dataset.v;
  if (typeOn.has(v)) { if (typeOn.size > 1) { typeOn.delete(v); el.classList.remove('on'); } }
  else { typeOn.add(v); el.classList.add('on'); }
  const f = allTrades.filter(t => typeOn.has(t.option_type));
  renderTrades(f); putMarkers(f);
}
function loadAll() { loadChart(); loadTrades(); }
async function loadChart() {
  setChartMsg('Loading chart…', '');
  try {
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), 20000);
    const r = await fetch('/api/chart?underlying=' + curU + '&date=' + curDate,
                          {signal: controller.signal});
    clearTimeout(tid);
    const d = await r.json();
    candles = d.candles || []; curInterval = d.interval || '1m';
    document.getElementById('ivl').textContent = d.interval || '—';
    series.setData(candles);
    if (candles.length) {
      chart.timeScale().fitContent();
      hideChartMsg();
    } else {
      const sub = d.error ? d.error : 'No data — try “Test Chart” to verify rendering works';
      setChartMsg('No chart data for ' + curU + ' ' + curDate, sub);
    }
  } catch(e) {
    const main = e.name === 'AbortError' ? 'Chart load timed out (20s)' : 'Chart error: ' + e.message;
    setChartMsg(main, 'Try \"Test Chart\" button to check if rendering works');
  }
}
async function loadSample() {
  const btn = document.getElementById('sampleBtn');
  btn.textContent = '…'; btn.disabled = true;
  try {
    const r = await fetch('/api/test-chart?date=' + curDate);
    const d = await r.json();
    candles = d.candles || [];
    series.setData(candles);
    if (candles.length) {
      chart.timeScale().fitContent();
      setChartMsg('', '');
      document.getElementById('chartMsg').classList.add('hide');
      document.getElementById('ivl').textContent = 'sample';
    } else {
      setChartMsg('Test chart returned 0 candles', '');
    }
  } catch(e) {
    setChartMsg('Test chart error: ' + e.message, '');
  }
  btn.textContent = 'Test Chart'; btn.disabled = false;
}
async function loadTrades() {
  try {
    const r = await fetch('/api/trades?date=' + curDate + '&underlying=' + curU);
    allTrades = await r.json();
    const f = allTrades.filter(t => typeOn.has(t.option_type));
    renderTrades(f); putMarkers(f);
  } catch(e) { console.error(e); }
}
function renderTrades(trades) {
  const tbl = document.getElementById('tbl'), em = document.getElementById('empty');
  const cnt = document.getElementById('pcnt'), sum = document.getElementById('psummary');
  if (!trades.length) { tbl.style.display='none'; em.style.display=''; cnt.textContent=''; sum.innerHTML=''; return; }
  tbl.style.display=''; em.style.display='none';
  const closed = trades.filter(t => t.pnl != null);
  const tot = closed.reduce((a,t)=>a+t.pnl,0);
  const wins = closed.filter(t=>t.pnl>=0).length, loss = closed.filter(t=>t.pnl<0).length;
  cnt.textContent = trades.length + ' trade' + (trades.length>1?'s':'');
  sum.innerHTML = '<span class="'+(tot>=0?'pos':'neg')+'">'+(tot>=0?'+':'')+'₹'+tot.toFixed(0)+'</span> &nbsp; '+wins+'W / '+loss+'L';
  document.getElementById('tbody').innerHTML = trades.map(t => {
    const tc=t.option_type.toLowerCase(), sk=t.strike?t.strike.toLocaleString('en-IN'):'—';
    const ep=t.exit_price!=null?t.exit_price.toFixed(2):'—';
    const pl=t.pnl!=null?'<span class="'+(t.pnl>=0?'pos':'neg')+'">'+(t.pnl>=0?'+':'')+'₹'+t.pnl.toFixed(0)+'</span>':'—';
    const lts=t.lots?t.lots+'L':t.quantity, sel=selId===t.id?' sel':'';
    const nt=(t.notes||'').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    return `<tr class="${sel}" data-id="${t.id}" onclick="selTrade(${t.id},'${t.entry_time||''}')">
      <td>${t.entry_time?t.entry_time.slice(0,5):'—'}</td><td><span class="tag ${tc}">${t.option_type}</span></td>
      <td>${sk}</td><td>${t.entry_price.toFixed(2)}</td><td>${ep}</td><td>${lts}</td><td>${pl}</td>
      <td><input class="ni" value="${nt}" placeholder="add note…" onclick="event.stopPropagation()" onblur="saveNote(${t.id},this.value)"></td>
    </tr>`;
  }).join('');
}
function tsFor(ds,ts){if(!ts)return null;const[y,mo,d]=ds.split('-').map(Number);const[h,m]=ts.split(':').map(Number);return Date.UTC(y,mo-1,d,h,m,0)/1000;}
function snapTs(ts){if(!ts||!candles.length)return ts;let best=candles[0].time,diff=Math.abs(candles[0].time-ts);for(const c of candles){const d=Math.abs(c.time-ts);if(d<diff){diff=d;best=c.time;}if(c.time>ts+7200)break;}return best;}
function putMarkers(trades){
  if(!series)return;
  const markers=[];
  for(const t of trades){
    const col=t.option_type==='CE'?'#4fc3f7':'#ffb74d';
    const lbl=t.option_type+' '+(t.strike?t.strike.toLocaleString('en-IN'):'');
    const ets=tsFor(curDate,t.entry_time);
    if(ets)markers.push({time:snapTs(ets),position:'aboveBar',color:col,shape:'arrowDown',text:lbl,id:'e'+t.id,size:1.2});
    if(t.exit_time&&t.exit_price!=null){
      const xts=tsFor(curDate,t.exit_time);
      if(xts)markers.push({time:snapTs(xts),position:'belowBar',color:(t.pnl!=null&&t.pnl>=0)?'#4caf50':'#ef5350',shape:'arrowUp',text:t.pnl!=null?(t.pnl>=0?'+':'')+Math.round(t.pnl):t.exit_price.toFixed(0),id:'x'+t.id,size:1.2});
    }
  }
  markers.sort((a,b)=>a.time-b.time);
  series.setMarkers(markers);
}
function selTrade(id,entryTime){
  selId=id;
  document.querySelectorAll('#tbody tr').forEach(r=>r.classList.toggle('sel',+r.dataset.id===id));
  if(entryTime&&candles.length){const ts=tsFor(curDate,entryTime);const sec=curInterval==='5m'?300:60;if(ts)chart.timeScale().setVisibleRange({from:ts-sec*25,to:ts+sec*90});}
}
async function saveNote(id,notes){
  try{await fetch('/api/trade/'+id+'/notes',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({notes})});const t=allTrades.find(x=>x.id===id);if(t)t.notes=notes;}catch(e){console.error(e);}
}
function openImp(){
  const t=new Date().toISOString().slice(0,10);
  document.getElementById('mFrom').value=t;
  document.getElementById('mTo').value=t;
  document.getElementById('mres').textContent='';
  document.getElementById('mdiag').style.display='none';
  document.getElementById('mdiag').textContent='';
  document.getElementById('ov').classList.add('show');
}
function closeImp(){document.getElementById('ov').classList.remove('show');}
async function doImport(){
  const btn=document.getElementById('mBtn'),res=document.getElementById('mres'),diag=document.getElementById('mdiag');
  btn.disabled=true;btn.textContent='Importing…';res.style.color='';res.textContent='Fetching from Dhan…';
  diag.style.display='none';diag.textContent='';
  try{
    const r=await fetch('/api/import',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({from_date:document.getElementById('mFrom').value,to_date:document.getElementById('mTo').value})});
    const d=await r.json();
    if(d.ok){
      res.style.color='#4caf50';
      res.textContent=`✓ ${d.imported} new, ${d.skipped} already stored (${d.total_options} options in ${d.total_raw} total trades)`;
      if(d.total_raw===0||d.total_options===0){
        const info=[];
        if(d.total_raw===0) info.push('API returned 0 trade records for this date range.');
        else if(d.total_options===0) info.push(`Found ${d.total_raw} trades but none detected as options.`);
        if(d.diag&&Object.keys(d.diag).length){
          info.push('');
          info.push('Debug info:');
          info.push(JSON.stringify(d.diag,null,2));
        }
        if(info.length){diag.textContent=info.join('\n');diag.style.display='block';}
      }
      if(d.imported>0)loadTrades();
    } else{
      res.style.color='#ef5350';res.textContent='Error: '+d.error;
    }
  }catch(e){res.style.color='#ef5350';res.textContent='Network error: '+e.message;}
  btn.disabled=false;btn.textContent='Import';
}
async function doRefreshToken(){
  const btn=document.getElementById('refreshBtn');btn.textContent='Refreshing…';btn.disabled=true;
  try{const r=await fetch('/api/refresh-token',{method:'POST'});const d=await r.json();
    btn.textContent=d.ok?'✓ Token':'✗ Token';btn.style.color=d.ok?'#4caf50':'#ef5350';
    setTimeout(()=>{btn.textContent='↻ Token';btn.style.color='';btn.disabled=false;},3000);
  }catch(e){btn.textContent='↻ Token';btn.disabled=false;}
}
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Trade Analyser starting on http://0.0.0.0:%d", PORT)
    get_db()
    import token_manager  # noqa: PLC0415
    if token_manager.is_token_refresh_configured():
        logger.info("Refreshing Dhan token at startup...")
        token_manager.refresh_token()
    # threaded=True: without it Flask's dev server is single-threaded
    # and one slow chart request blocks all other requests
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
