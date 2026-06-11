# Claude Code Context — Trade Analyser

This file lets a new Claude session pick up exactly where the last one left off.

---

## What this project is

A post-session option trade review tool. It imports executed option trades from Dhan's trade history API and overlays entry/exit markers on the **underlying index chart** (Nifty, Sensex, Banknifty). Option charts disappear after expiry, so the spot chart is used instead.

Single-VPS Flask app, runs on port 5556. Companion to the risk-management platform (port 5555) on the same VPS.

**Branch for all work:** `claude/admiring-einstein-prd40v`

---

## File structure

| File | Purpose |
|---|---|
| `app.py` | Single-file Flask app — all routes, DB, chart data, inline HTML/CSS/JS |
| `token_manager.py` | Dhan token auto-refresh via PIN + TOTP (copied from risk-management) |
| `analyser.db` | SQLite database (gitignored, created on first run) |
| `analyser.log` | Log file (gitignored) |
| `requirements.txt` | Python dependencies |
| `.env` | Credentials (gitignored) — see `.env.example` |

---

## Architecture

### Backend (app.py)

**Config** — reads from `.env` via `load_dotenv`. Key vars: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `PORT`.

**Database** — single SQLite file `analyser.db`, one table `trades`. Thread-safe via `_db_lock`. Connection is module-level singleton.

**`_dhan_client()`** — builds fresh `dhanhq` client on every call, calling `load_dotenv(override=True)` first to pick up any token refresh automatically.

**`_do_import(from_date, to_date)`** — core import logic:
1. Calls `dhan.get_trade_history(from_date, to_date, page_number)` — Dhan date format is `DD-MM-YYYY`
2. Paginates until batch < 50 records
3. Filters to `exchangeSegment in (NSE_FNO, BSE_FNO)` and `drvOptionType in (CALL, PUT, CE, PE)`
4. Groups by `(date, securityId)`
5. For each group: pairs each SELL with the earliest subsequent BUY of equal quantity
6. Inserts into SQLite, skips duplicates by `(date, security_id, entry_time, dhan_order_id)`

**`import_from_dhan()`** — wraps `_do_import` with auto-refresh-on-auth-error retry.

**`chart_candles(underlying, trade_date)`** — fetches OHLCV from yfinance:
- `^NSEI` = NIFTY, `^BSESN` = SENSEX, `^NSEBANK` = BANKNIFTY
- Interval: `1m` if <=5 days old, `5m` if <=55 days, `1d` older
- Strips timezone → naive IST → `.timestamp()` gives "IST-as-UTC" epoch
- TradingView then displays correct IST times (same trick as risk-management)

### Frontend (inline in `_page()`)

- TradingView Lightweight Charts v4.1.3 from CDN
- Dark theme matching risk-management (`#0d0d0d` background)
- `Date.UTC(y, mo-1, d, h, m, 0) / 1000` converts trade times to match server timestamps
- `snapTs(ts)` snaps a timestamp to the nearest available candle
- CE markers: `#4fc3f7` (blue), PE markers: `#ffb74d` (amber)
- Entry = `arrowDown aboveBar`, exit = `arrowUp belowBar` (green if profit, red if loss)
- Markers must be sorted by time before calling `series.setMarkers()`
- `ResizeObserver` handles chart resize

### Token management (token_manager.py)

Copied from risk-management with `platform.log` → `analyser.log`.

Strategy:
1. `try_renew_token()` — extends active token by 24h (fast, works while token is valid)
2. `generate_fresh_token()` — PIN + TOTP via `DhanLogin`. Retries on 2-minute rate limit.
3. Writes new token to `.env` file via regex replace
4. `_dhan_client()` reloads `.env` on every call, so refresh is automatic

Startup: `refresh_token()` called at `__main__` if `is_token_refresh_configured()` is true.

---

## Database schema

```sql
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT,      -- YYYY-MM-DD
    underlying      TEXT,      -- NIFTY, SENSEX, BANKNIFTY, FINNIFTY
    option_type     TEXT,      -- CE or PE
    strike          REAL,
    expiry          TEXT,      -- as returned by Dhan
    entry_time      TEXT,      -- HH:MM:SS IST
    entry_price     REAL,      -- premium sold at
    exit_time       TEXT,      -- HH:MM:SS IST (empty if still open)
    exit_price      REAL,      -- premium bought back at (null if open)
    quantity        INTEGER,   -- total contracts
    lot_size        INTEGER,   -- lot size for the underlying
    lots            REAL,      -- quantity / lot_size
    pnl             REAL,      -- (entry - exit) * quantity, null if open
    status          TEXT,      -- CLOSED or OPEN
    notes           TEXT,      -- user notes
    security_id     TEXT,      -- Dhan security ID
    exchange_segment TEXT,     -- NSE_FNO or BSE_FNO
    dhan_order_id   TEXT,      -- Dhan order ID (used for dedup)
    created_at      REAL       -- Unix timestamp of import
);
```

---

## API routes

| Method | Route | Description |
|---|---|---|
| GET | `/` | Main page |
| POST | `/api/import` | `{from_date, to_date}` → import from Dhan |
| GET | `/api/trades` | `?date=&underlying=&option_type=` |
| PUT | `/api/trade/<id>/notes` | `{notes}` |
| GET | `/api/chart` | `?underlying=&date=` → OHLCV candles |
| GET | `/api/dates` | List of dates with trades (last 90) |
| POST | `/api/refresh-token` | Trigger token refresh |

---

## Lot sizes (hardcoded)

```python
LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 15, "SENSEX": 10,
    "FINNIFTY": 40, "MIDCPNIFTY": 50,
}
```

These change periodically with SEBI revisions. Update here when lot sizes change.

---

## Key technical gotchas

### Timestamp handling
Dhan `createTime` is IST string `"YYYY-MM-DD HH:MM:SS"`. yfinance timestamps are tz-aware IST. Both are stripped to naive and `.timestamp()` is called — this gives "IST-as-UTC" Unix epoch. TradingView displays UTC, so it shows the correct IST time. **Do not add timezone to TradingView timeScale** (v4 doesn't support it). **Do not use `new Date()` in JS** for converting trade times — use `Date.UTC()` to avoid browser timezone interference.

### Dhan trade history date format
`get_trade_history()` expects `DD-MM-YYYY`, not `YYYY-MM-DD`. The app converts internally.

### SELL/BUY pairing
Each SELL is matched with the earliest BUY **after** it (by `createTime` string comparison) with equal quantity. This handles re-entries correctly. Unmatched SELLs are stored as OPEN with null exit.

### Underlying detection
- `BSE_FNO` → always `SENSEX`
- `NSE_FNO` → strip prefix from `tradingSymbol` (BANKNIFTY > MIDCPNIFTY > FINNIFTY > NIFTY)

### yfinance gaps
`^BSESN` (SENSEX) sometimes has missing 1m/5m data on yfinance. This is a data provider limitation — the chart will be empty for those dates even if trades exist.

---

## Pending / next work

- **Spread grouping** — detect and visually group the sell leg + hedge leg of a credit spread (same underlying, same timestamp cluster, opposite strikes). Show as a bracketed pair on the chart with the net credit.
- **P&L for re-entries** — if the same option is traded twice in a session with different quantities, the current SELL→BUY pairing may mismatch. Add quantity netting logic.
- **Daily candles fallback note** — when showing 1d candles for old dates, add a UI note explaining the resolution.
- **SENSEX chart fallback** — when `^BSESN` returns no data, try `^BSESN` with a wider date window or fall back to daily.
- **Open positions** — trades with no exit (OPEN status) show entry marker only. Consider adding a "still open" indicator.
- **Export** — CSV export of trade history for a date range.
- **Session summary** — daily stats card: total trades, win rate, gross P&L, best/worst trade.

---

## VPS workflow

```bash
# Pull latest
cd ~/trade-analyser
git pull origin claude/admiring-einstein-prd40v

# Run manually
source venv/bin/activate && python app.py

# If running as systemd service
sudo systemctl restart trade-analyser
tail -f /root/trade-analyser/analyser.log
```

Dashboard: `http://YOUR_VPS_IP:5556`

---

## How to resume with a new Claude session

1. Open this repo in Claude Code
2. Say: *"Read CLAUDE.md and continue development on the trade analyser"*
3. Claude will read this file and have full context

Active branch: `claude/admiring-einstein-prd40v`
