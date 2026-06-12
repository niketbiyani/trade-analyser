# Claude Code Context — Trade Analyser

This file lets a new Claude session pick up exactly where the last one left off.

---

## What this project is

A post-session option trade review tool. It imports executed option trades from Dhan's trade history API and overlays entry/exit markers on the **underlying index chart** (Nifty, Sensex, Banknifty). Option charts disappear after expiry, so the spot chart is used instead.

Single-VPS Flask app, runs on port 5556. Companion to the risk-management platform (port 5555) on the same VPS.

**Branch for all work:** `claude/admiring-einstein-prd40v`

**Current version:** `v15`

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

**`_dhan_client()`** — builds fresh `dhanhq` client on every call, calling `load_dotenv(override=True)` first to pick up any token refresh.

**`_aggregate_partial_fills(trades)`** — merges same-`orderId` records before SELL→BUY pairing. Dhan records large orders as multiple partial fills; this sums quantities and weighted-averages prices per orderId.

**`_process_raw_trades(raw)`** — core trade processing:
1. Normalise `transactionType` → BUY/SELL
2. Parse `tradingSymbol` → underlying, option_type, strike, expiry
3. Group by `(date, securityId)`
4. Per group: call `_aggregate_partial_fills`, then pair SELLs→BUYs (SHORT) or BUYs→SELLs (LONG/hedge)
5. For LONG direction: swap entry/exit so entry=opening BUY, exit=closing SELL
6. Upsert into SQLite, skip duplicates by `(date, security_id, entry_time, dhan_order_id)`

**`_do_import(from_date, to_date)`** — fetches paginated trade history from Dhan, filters to FNO options only, calls `_process_raw_trades`.

**`import_from_dhan()`** — wraps `_do_import` with auto-refresh-on-auth-error retry.

**`_raw_dhan_chart(security_id, exchange_segment, instrument_type, to_date, from_date)`** — fetches 1-minute OHLCV candles from Dhan `intraday_minute_data`. Always `interval=1`. Supports up to 90 days per call; the app passes `from_date = previous trading day` and `to_date = trade_date` so there is data for EMA warmup.

**`chart_candles(underlying, trade_date)`** — main chart data function:
- Looks up security_id/exchange_segment for NIFTY/SENSEX/BANKNIFTY
- Calls `_raw_dhan_chart` with prev_day → trade_date range
- Returns `(candles, interval, error)`
- **No yfinance** — Dhan historical API only, always 1m candles

### Frontend (inline in `_page()`)

- TradingView Lightweight Charts v4.1.3 from CDN
- Dark theme (`#0d0d0d` background)
- **Three-pane layout**: main chart (candles + EMA 20/50), RSI 14, MACD 12,26,9
- All panes use `_chartOpts(el, timeScaleOpts)` helper — uses `crosshair: { mode: 1 }` (numeric, not enum). **Never use `LightweightCharts.CrosshairMode` or `LightweightCharts.LineStyle` enums** — they are not reliably exported from the v4.1.3 standalone bundle and cause silent failures.
- `initChart()` split into two try/catch blocks: first for main chart (critical — returns on failure), second for RSI+MACD (optional — `console.warn` on failure, main chart still works)
- `_syncingRange` flag prevents scroll feedback loops between panes
- CE markers: `#4fc3f7` (blue), PE markers: `#ffb74d` (amber)
- Entry = `arrowDown aboveBar`, exit = `arrowUp belowBar` (green if profit, red if loss)
- Markers must be sorted by time before calling `series.setMarkers()`
- `snapTs(ts)` snaps a trade timestamp to nearest available candle
- **Trade isolation**: clicking a row sets `isolateId`; `putMarkers` filters to that trade only. Clicking again clears isolation and shows all markers.
- **Notes preservation**: client-side snapshot of `(underlying, option_type, strike, entry_time) → notes` before wipe; `_restoreNotes()` replays saves after reimport by matching those 4 fields.
- **Date navigation fix**: `shiftDay()` uses `Date.UTC()` to avoid IST browser timezone shifting the date. `DOMContentLoaded` calculates today as `new Date(Date.now() + 19800000).toISOString().slice(0,10)` (UTC+5:30 offset).
- `_watchResize(inst, el)` — ResizeObserver that calls `inst.resize()` on each pane

### Token management (token_manager.py)

Copied from risk-management with `platform.log` → `analyser.log`.

Strategy:
1. `try_renew_token()` — extends active token by 24h (fast, works while valid)
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
    entry_price     REAL,      -- premium at entry
    exit_time       TEXT,      -- HH:MM:SS IST (empty if still open)
    exit_price      REAL,      -- premium at exit (null if open)
    quantity        INTEGER,   -- total contracts
    lot_size        INTEGER,   -- lot size for the underlying
    lots            REAL,      -- quantity / lot_size
    pnl             REAL,      -- (entry - exit) * qty for SHORT; (exit - entry) * qty for LONG; null if open
    status          TEXT,      -- CLOSED or OPEN
    notes           TEXT,      -- user notes
    security_id     TEXT,      -- Dhan security ID
    exchange_segment TEXT,     -- NSE_FNO or BSE_FNO
    dhan_order_id   TEXT,      -- Dhan order ID (used for dedup)
    created_at      REAL,      -- Unix timestamp of import
    direction       TEXT       -- SHORT (default) or LONG (hedge leg)
);
```

DB migrations run in `_init_db()` on startup:
- Adds `direction` column if missing (existing rows default to `SHORT`)
- Fixes LONG trades stored with swapped entry/exit times from older import logic

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

Update when SEBI revises lot sizes.

---

## Key technical gotchas

### Timestamp handling
Dhan `createTime` is IST string `"YYYY-MM-DD HH:MM:SS"`. Strip timezone → naive → `.timestamp()` gives "IST-as-UTC" Unix epoch. TradingView displays UTC, so it shows correct IST times. **Do not add timezone to TradingView timeScale** (v4 doesn't support it). **Do not use `new Date()` in JS** for converting trade times — use `Date.UTC()` to avoid browser timezone interference.

### Dhan trade history date format
`get_trade_history()` expects `DD-MM-YYYY`, not `YYYY-MM-DD`. The app converts internally.

### SELL/BUY pairing
- **SHORT direction**: each SELL is matched with the earliest BUY after it with equal quantity
- **LONG direction**: each BUY is matched with the earliest SELL after it with equal quantity; then entry/exit are swapped so entry=opening BUY, exit=closing SELL

### Partial fill aggregation
`_aggregate_partial_fills()` must be called before SELL/BUY pairing. It merges records sharing the same `orderId` — summing quantities, weighted-averaging prices.

### Underlying detection
- `BSE_FNO` → always `SENSEX`
- `NSE_FNO` → strip prefix from `tradingSymbol` (BANKNIFTY > MIDCPNIFTY > FINNIFTY > NIFTY)

### TradingView enum crash
`LightweightCharts.CrosshairMode` and `LightweightCharts.LineStyle` are NOT reliably exported from the v4.1.3 standalone bundle. Referencing them throws silently inside try/catch, killing the entire chart init. Always use numeric values: `CrosshairMode.Normal = 1`, `LineStyle.Dashed = 1`, `LineStyle.Solid = 0`.

### Chart data always from Dhan
yfinance has been removed entirely. All chart data comes from `dhan.intraday_minute_data()` which supports 5 years of 1m history. The app fetches `(prev_day, trade_date)` to give indicators enough warmup data.

---

## systemd service (VPS)

Service file: `/etc/systemd/system/trade-analyser.service`

```ini
[Unit]
Description=Trade Analyser
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/trade-analyser
ExecStart=/root/trade-analyser/venv/bin/python app.py
Restart=always
RestartSec=5
StandardOutput=append:/root/trade-analyser/analyser.log
StandardError=append:/root/trade-analyser/analyser.log

[Install]
WantedBy=multi-user.target
```

Commands:
```bash
sudo systemctl enable trade-analyser   # auto-start on boot (run once)
sudo systemctl start trade-analyser
sudo systemctl stop trade-analyser
sudo systemctl restart trade-analyser
sudo systemctl status trade-analyser
sudo journalctl -u trade-analyser -n 50   # systemd journal (if log file empty)
tail -f /root/trade-analyser/analyser.log
```

---

## VPS workflow

```bash
# Pull latest and restart
cd ~/trade-analyser
git pull origin claude/admiring-einstein-prd40v
sudo systemctl restart trade-analyser
tail -f /root/trade-analyser/analyser.log
```

Dashboard: `http://YOUR_VPS_IP:5556`

---

## Pending / next work

- **Spread grouping** — detect and visually group the sell leg + hedge leg of a credit spread (same underlying, same timestamp cluster, opposite strikes). Show as a bracketed pair on the chart with the net credit.
- **P&L for re-entries** — if the same option is traded twice in a session with different quantities, the current SELL→BUY pairing may mismatch. Add quantity netting logic.
- **Session summary** — daily stats card: total trades, win rate, gross P&L, best/worst trade.
- **Export** — CSV export of trade history for a date range.
- **Open positions indicator** — trades with OPEN status (no exit) show entry marker only. Consider adding a "still open" visual indicator.
- **SENSEX chart** — `^BSESN` on yfinance was removed; Dhan historical data for SENSEX needs the correct security_id verified in production.

---

## How to resume with a new Claude session

1. Open this repo in Claude Code
2. Say: *"Read CLAUDE.md and continue development on the trade analyser"*
3. Claude will read this file and have full context

Active branch: `claude/admiring-einstein-prd40v`
