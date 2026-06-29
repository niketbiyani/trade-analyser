# Claude Code Context — Trade Analyser

This file lets a new Claude session pick up exactly where the last one left off.

---

## What this project is

A post-session option trade review tool. It imports executed option trades from Dhan's trade history API and overlays entry/exit markers on the **underlying index chart** (Nifty, Sensex, Banknifty). Option charts disappear after expiry, so the spot chart is used instead.

Single-VPS Flask app, runs on port 5556. Companion to the risk-management platform (port 5555) on the same VPS.

**Branch for all work:** `claude/admiring-einstein-prd40v`

**Current version:** `v135`

---

## File structure

| File | Purpose |
|---|---|
| `app.py` | Single-file Flask app — all routes, DB, chart data, inline HTML/CSS/JS |
| `token_manager.py` | Dhan token auto-refresh via PIN + TOTP (copied from risk-management) |
| `analyser.db` | SQLite database (gitignored, created on first run) |
| `analyser.log` | Log file (gitignored) — may be empty; use `journalctl -u trade-analyser` |
| `requirements.txt` | Python dependencies |
| `.env` | Credentials (gitignored) — see `.env.example` |

---

## Architecture

### Backend (app.py)

**Config** — reads from `.env` via `load_dotenv`. Key vars: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `PORT`, `APPLICATION_ROOT`.

```python
APP_VERSION = "v128"
PORT     = int(os.getenv("PORT", "5556"))
APP_ROOT = os.getenv("APPLICATION_ROOT", "")   # e.g. "/analyser" for Nginx prefix
DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyser.db")

app = Flask(__name__)
app.config['APPLICATION_ROOT'] = APP_ROOT
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
```

**Database** — single SQLite file `analyser.db`. Tables: `trades`, `trade_notes`, `option_instruments`, `tick_data`. Thread-safe via `_db_lock`. Connection is module-level singleton.

**`_dhan_client()`** — builds fresh `dhanhq` client on every call, calling `load_dotenv(override=True)` first to pick up any token refresh.

**`_real_ts(v)`** — filters out Dhan "NA" sentinel values. Trade history records have `createTime="NA"` and `updateTime="NA"` (literal strings, not null). Returns `""` for these so code falls through to `exchangeTime`.

**`_ts_to_time(ts)`** — extracts `HH:MM:SS` from any timestamp format:
- `"YYYY-MM-DD HH:MM:SS"` → `ts[11:19]`
- `"YYYY-MM-DDTHH:MM:SS"` (ISO, from `exchangeTime` in trade history) → `ts[11:19]`
- `"HH:MM:SS"` (trade book bare time) → `ts[:8]`

**`_aggregate_partial_fills(trades)`** — merges same-`orderId` records before SELL→BUY pairing. Dhan records large orders as multiple partial fills; this sums quantities and weighted-averages prices per orderId.

**`_process_raw_trades(raw)`** — core trade processing:
1. Normalise `transactionType` → BUY/SELL
2. Parse `tradingSymbol`/`customSymbol` → underlying, option_type, strike, expiry
3. Group by `(date, securityId)` — uses `_real_ts()` to skip "NA" createTime, falls through to `exchangeTime`
4. Per group: call `_aggregate_partial_fills`, then pair via `_fifo_pair`
5. Upsert into SQLite — three dedup checks (see Dedup section below)
6. After each INSERT: restore any saved note from `trade_notes` backup table

**`_do_import(from_date, to_date)`** — fetches paginated trade history from Dhan, filters to FNO options only, calls `_process_raw_trades`. Paginates pages 0, 1, 2, ... until an empty response (no early break on batch size — Dhan's page size is ~20 records, not 50).

**`import_from_dhan()`** — wraps `_do_import` with auto-refresh-on-auth-error retry.

**`_raw_dhan_chart(security_id, exchange_segment, instrument_type, day, from_date)`** — fetches 1-minute OHLCV candles for a single day (or range if `from_date` given). Uses `/charts/intraday` which reliably returns 1-minute candles for the last 5+ trading days.

**`_fetch_warmup_candles(idx, from_day, to_day)`** — fetches warmup data via a **single batch call** to `intraday_minute_data` spanning the full warmup range. Groups parsed candles by trading date, keeps the 3 most recent days. Falls back to per-day calls (0.3s inter-call sleep) if batch returns nothing. **Do NOT use `/charts/historical` for 1-minute warmup** — that endpoint returns daily candles regardless of `type` parameter.

**`chart_candles(underlying, trade_date)`** — main chart data function:
- Looks up security_id/exchange_segment for NIFTY/SENSEX/BANKNIFTY
- Calls `_raw_dhan_chart` with prev_day → trade_date range
- Returns `(candles, interval, error)`
- **No yfinance** — Dhan historical API only, always 1m candles

### Tick data collection

Continuously collects 1-second (or faster) index ticks from Dhan's MarketFeed and stores them in `tick_data`. This powers the future 5s/15s chart feature.

**`subscribe_ticks(instruments)`** — wraps `dhanhq.MarketFeed.Ticker`. Each instrument is `(security_id, exchange_segment_string)`.

**`_tick_callback(inst, type, packet)`** — called on every tick; writes `(security_id, ts, price)` to `tick_data` table. `ts` is IST-as-UTC epoch (parse IST string naively with `.timestamp()`).

**Startup subscription** (at `__main__`):
```python
subscribe_ticks([("13", "NSE_EQ"), ("51", "BSE_EQ")])
```
- NIFTY: `security_id="13"`, `NSE_EQ`
- SENSEX: `security_id="51"`, `BSE_EQ`

**CRITICAL**: `IDX_I` (segment 0) does NOT work with `MarketFeed.Ticker` — it returns no ticks. Always use `NSE_EQ` (segment 3) for NIFTY index and `BSE_EQ` (segment 4) for SENSEX index.

Exchange segment int mapping for MarketFeed:
```
NSE_FNO=2, BSE_FNO=8, NSE_EQ=3, BSE_EQ=4, IDX_I=0 (broken)
```

**To verify tick collection on VPS:**
```bash
sqlite3 /root/trade-analyser/analyser.db \
  "SELECT security_id, COUNT(*), MIN(datetime(ts,'unixepoch')), MAX(datetime(ts,'unixepoch')) FROM tick_data WHERE security_id IN ('13','51') GROUP BY security_id;"
```

### Nginx reverse proxy (APPLICATION_ROOT)

The app can run behind Nginx at a path prefix (e.g. `/analyser/`).

**Flask side:**
- `APPLICATION_ROOT` env var sets the prefix (e.g. `/analyser`)
- `ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)` handles `X-Forwarded-*` headers
- Each page function does `root = APP_ROOT` and injects `var _root='{root}';` into the JS block
- All `fetch()` calls use `fetch(_root+'/api/...')` — never bare `/api/`
- All nav hrefs use `href="{root}/"` — never bare `/`

**Nginx side** (example block):
```nginx
location /analyser/ {
    proxy_pass         http://127.0.0.1:5556/;   # trailing slash strips prefix
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   X-Forwarded-Prefix /analyser;
}
```

The trailing slash on `proxy_pass` is mandatory — it strips `/analyser/` before forwarding to Flask.

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

**Auto-import and chart viewport fix (v119/v135):**
- `refreshTradesTable()` — fetches trades, calls `renderTrades()` AND `putMarkers()`. The `putMarkers()` call (added v135) ensures exit markers appear on the chart when a trade closes via auto-import — it does NOT reset the chart viewport, only re-places markers.
- `loadTrades()` — full load: fetches trades + calls `renderTrades()` + `putMarkers()`. Used on page load and manual date change.
- Auto-import (every 2 minutes on today's date) calls `refreshTradesTable()`, not `loadTrades()`. This prevents the chart from resetting its scroll position every 2 minutes.

**Resizable trades panel (v121):**
- `#resizeHandle` — a 5px tall `div` between `#chartsArea` and `#panel`
- Drag handle: `mousedown` captures `startY` + `startH`; `mousemove` adjusts `panel.style.height`; `mouseup` cleans up
- Height clamped: `Math.max(80, Math.min(startH+dy, window.innerHeight-180))`
- CSS: `cursor: ns-resize`, highlights on hover and while dragging

**Trades table (v128):**
- `table-layout: fixed; width: 100%` for stable column widths
- `overflow: hidden; text-overflow: ellipsis; white-space: nowrap` on all `th`/`td`
- Explicit px widths on every column; Notes column gets the remaining space:

| Column | Width |
|---|---|
| Time (HH:MM:SS) | 148px |
| Dir | 44px |
| Type | 36px |
| Strike | 70px |
| Entry ₹ | 64px |
| Exit ₹ | 64px |
| Lots | 36px |
| P&L | 52px |
| Notes | auto (remainder) |
| Actions | 58px |

- Time shows full `HH:MM:SS` — do NOT truncate to HH:MM. Seconds are important for 15s chart analysis.

**Direction column (v124):**
- Shows SHORT or LONG tag in a coloured pill after Time
- `.tag.short` — red tint (`rgba(239,83,80,.10)`, text `#ef9a9a`)
- `.tag.long` — green tint (`rgba(129,199,132,.10)`, text `#81c784`)

**Notes preservation (two-layer):**
- *Client-side*: `_savedNotes` snapshot taken in `wipeDate()` before DELETE; `_restoreNotes()` replays after import by matching `(underlying, option_type, strike, entry_time)`. Works within the same browser session.
- *Server-side*: `DELETE /api/trades/date/{date}` writes notes to `trade_notes` table before deleting. On each INSERT in `_process_raw_trades`, the backup is checked and restored automatically. Survives browser close, session change, or delayed reimport.

**Date navigation fix**: `shiftDay()` uses `Date.UTC()` to avoid IST browser timezone shifting the date. `DOMContentLoaded` calculates today as `new Date(Date.now() + 19800000).toISOString().slice(0,10)` (UTC+5:30 offset).

`_watchResize(inst, el)` — ResizeObserver that calls `inst.resize()` on each pane.

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

CREATE TABLE trade_notes (
    date         TEXT NOT NULL,
    underlying   TEXT NOT NULL,
    option_type  TEXT NOT NULL,
    strike       REAL NOT NULL,
    entry_time   TEXT NOT NULL,
    notes        TEXT DEFAULT '',
    updated_at   REAL DEFAULT 0,
    PRIMARY KEY (date, underlying, option_type, strike, entry_time)
);

CREATE TABLE tick_data (
    security_id  TEXT NOT NULL,
    ts           REAL NOT NULL,    -- IST-as-UTC epoch
    price        REAL NOT NULL
);

CREATE TABLE option_instruments (
    -- used by option-chart page for instrument lookup
    security_id     TEXT PRIMARY KEY,
    exchange_segment TEXT,
    trading_symbol  TEXT,
    underlying      TEXT,
    option_type     TEXT,
    strike          REAL,
    expiry          TEXT
);
```

`trade_notes` is a persistent notes backup. Written by `DELETE /api/trades/date/{date}` before wiping, read back by `_process_raw_trades` after each INSERT. Notes survive wipes, session changes, and reimports.

`tick_data` stores continuous NIFTY and SENSEX ticks. No primary key — each tick is an append. Future use: aggregate into 5s/15s candles for the chart.

DB migrations run in `_init_db()` on startup:
- Adds `direction` column if missing (existing rows default to `SHORT`)
- Fixes LONG trades stored with swapped entry/exit times from older import logic
- Deduplicates same `dhan_order_id+direction` pairs keeping the one with real entry_time

---

## Import dedup logic (three-level fallback in `_process_raw_trades`)

For each FIFO-paired trade being inserted:

1. **Primary** — `dhan_order_id + direction`: stable across reimports for API-imported trades (Dhan always returns orderId). Skip if match found; patch entry_time or close status if needed.

2. **Secondary** — `(date, security_id, entry_time, direction)`: catches re-imports of the same CSV (same symbol-string security_id, same FIFO entry_time). Only checked if `entry_time` is non-empty.

3. **Tertiary** — `(date, underlying, option_type, strike, entry_time, direction)`: cross-format bridge — catches CSV imported on top of API records (different security_id format but same trade identity). No status filter (v89 fix — previously `AND status='OPEN'` caused misses).

**Important**: dedup is a safety net, not a primary workflow. Always wipe before reimporting a date to guarantee a clean slate.

---

## API routes

| Method | Route | Description |
|---|---|---|
| GET | `/` | Main page |
| GET | `/option-chart` | Option premium chart page |
| POST | `/api/import` | `{from_date, to_date}` → import from Dhan |
| POST | `/api/import-csv` | multipart file upload → parse CSV and import |
| GET | `/api/trades` | `?date=&underlying=&option_type=` |
| PUT | `/api/trade/<id>/notes` | `{notes}` |
| DELETE | `/api/trades/date/<date>` | Wipe all trades for date; backs up notes to `trade_notes` first |
| GET | `/api/chart` | `?underlying=&date=` → 1m OHLCV candles |
| GET | `/api/option-candles` | `?security_id=&exchange_segment=&interval=` → option premium candles |
| GET | `/api/option-list` | Distinct options from trades DB (for By Expiry tab) |
| GET | `/api/dates` | List of dates with trades (last 90) |
| POST | `/api/refresh-token` | Trigger token refresh |
| GET | `/api/debug-dhan` | `?from_date=&to_date=` → raw Dhan API responses (all pages) |

---

## Lot sizes (hardcoded)

```python
LOT_SIZES = {
    "NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20,
    "FINNIFTY": 65, "MIDCPNIFTY": 75,
}
```

Update when SEBI revises lot sizes.

---

## Key technical gotchas

### Dhan trade history vs trade book — field differences (CRITICAL)

The two Dhan endpoints return **different field names and formats**:

| Field | Trade history (`/trades/{from}/{to}/{page}`) | Trade book (`/trades`) |
|---|---|---|
| Symbol | `customSymbol: "NIFTY 16 JUN 24050 CALL"` | `tradingSymbol: "NIFTY24JUN2424050CE"` |
| Time | `createTime: "NA"` (literal NA!) | `createTime: "YYYY-MM-DD HH:MM:SS"` |
| Real time | `exchangeTime: "2026-06-16T14:48:56"` (ISO) | same field exists but createTime is valid |
| Instrument | `instrument: "OPTIDX"` | `instrumentType: "OPTIDX"` |
| Option type | `drvOptionType: "CALL"/"PUT"` | same |

**`_real_ts(v)`** — always use this before accessing createTime. Returns `""` for "NA" sentinels so fallback chain reaches `exchangeTime`.

**`_ts_to_time(ts)`** — handles both `"YYYY-MM-DD HH:MM:SS"` and ISO `"YYYY-MM-DDTHH:MM:SS"` — both return `ts[11:19]` correctly.

### Dhan trade history API — date format and pagination

- URL: `GET /v2/trades/{from-date}/{to-date}/{page}` — dates **must be YYYY-MM-DD** (DD-MM-YYYY returns `TRADE_RESOURCE_ERROR`)
- Covers **both INTRADAY and MARGIN** product types (confirmed from live data)
- Page size is ~20 records — **do not break early when batch size < 50**, keep paginating until empty page
- `TRADE_RESOURCE_ERROR` with empty error_message usually means **expired token**, not a date format issue

### Wipe & reimport — notes are safe

`DELETE /api/trades/date/{date}` (triggered by the Wipe & reimport button) backs up all notes to the `trade_notes` table before deleting. On the subsequent reimport, `_process_raw_trades` checks `trade_notes` after each INSERT and restores matching notes automatically. Notes are matched by `(date, underlying, option_type, strike, entry_time)` — as long as the FIFO produces the same entry_time (consistent for stable data), notes are fully preserved. Client-side `_savedNotes`/`_restoreNotes()` provides a second layer for within-session restores.

### Wipe bug history (fixed in v90)

The DELETE route previously had `AND dhan_order_id != ''` which silently left CSV-imported trades (empty dhan_order_id) in the DB across every wipe. Fixed in v90: the route now deletes ALL trades for the date unconditionally.

### Timestamp handling
Dhan `createTime` in trade book is IST string `"YYYY-MM-DD HH:MM:SS"`. Strip timezone → naive → `.timestamp()` gives "IST-as-UTC" Unix epoch. TradingView displays UTC, so it shows correct IST times. **Do not add timezone to TradingView timeScale** (v4 doesn't support it). **Do not use `new Date()` in JS** for converting trade times — use `Date.UTC()` to avoid browser timezone interference.

### Dhan chart data — UTC integers need +19800 offset
`/charts/intraday` returns **true UTC epoch integers**. Adding `+19800` (5.5 hours) converts to IST-as-UTC so TradingView shows "09:15" instead of "03:45". Do NOT remove this offset.

### FIFO position tracking (`_fifo_pair`)
Replaces all quantity/time heuristics. Processes trades chronologically:
- **BUY** → closes oldest open SHORT first (FIFO); any excess opens a LONG
- **SELL** → closes oldest open LONG first (FIFO); any excess opens a SHORT
- One large order (e.g. BUY 130) can split across multiple positions (e.g. close SELL 65 + open LONG 65)
- Re-entries, partial closes, and same-option multiple positions all handled correctly
- Uses `_real_ts()` fallback chain: `createTime → orderCreateTime → exchangeTime → updateTime`

### Partial fill aggregation
`_aggregate_partial_fills()` runs before `_fifo_pair`. It merges records sharing the same `orderId` — summing quantities, weighted-averaging prices. This handles Dhan reporting one order as multiple fill notifications.

### Underlying detection
- `BSE_FNO` → always `SENSEX`
- `NSE_FNO` → strip prefix from symbol (BANKNIFTY > MIDCPNIFTY > FINNIFTY > NIFTY)
- `_underlying()` strips spaces/dashes before prefix matching — handles both compact `"NIFTY24JUN..."` and spaced `"NIFTY 16 JUN ..."` formats

### TradingView enum crash
`LightweightCharts.CrosshairMode` and `LightweightCharts.LineStyle` are NOT reliably exported from the v4.1.3 standalone bundle. Always use numeric values: `CrosshairMode.Normal = 1`, `LineStyle.Dashed = 1`, `LineStyle.Solid = 0`.

### Dhan endpoint distinction — CRITICAL
- `/charts/intraday` (`intraday_minute_data`) → 1-minute candles, covers last 5+ trading days. **Use this for all 1-minute chart data.**
- `/charts/historical` (`historical_daily_data`) → **daily candles only** (1 per day). Do NOT use for 1-minute data.

### JS "Script error. line 0"
Harmless cross-origin error from TradingView CDN script caught by global error handler. Does not affect functionality.

### Logs on VPS
The systemd service is configured to write to `analyser.log`, but in practice logs often go to the systemd journal. Always check both:
```bash
tail -f /root/trade-analyser/analyser.log
sudo journalctl -u trade-analyser -n 50 -f
```

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
sudo journalctl -u trade-analyser -n 50 -f   # live log stream
tail -f /root/trade-analyser/analyser.log    # file log (may be empty)
```

---

## VPS workflow

```bash
# Pull latest and restart
cd ~/trade-analyser
git pull origin claude/admiring-einstein-prd40v
sudo systemctl restart trade-analyser
sudo journalctl -u trade-analyser -n 30
```

**Without Nginx prefix:**  `http://YOUR_VPS_IP:5556`
**With Nginx at /analyser:** `http://YOUR_VPS_IP/analyser/`

---

## Option Chart page (`/option-chart`)

Added in v60–v63. Separate page from the main index chart.

**Layout:** Chart on top (TradingView LWC v5.2.0), trades pane at bottom (265px).

**Bottom pane — two tabs:**

1. **By Date** — date nav (◀ date ▶, trade count, Manual button). Click any trade row → loads that option's premium chart with entry/exit markers. Date navigation uses `/api/dates` to jump between actual trading days.

2. **By Expiry** — cascading dropdowns: Underlying → Expiry (shows "19 Jun 2026" format, sorted desc) → CE/PE toggle → Strike → auto-loads chart. Uses `/api/option-list` client-side (no extra routes). Covers expired options from imported trade data.

**`from_date` strategy:** 30 days before expiry date (`fromDateFor(expDate)` in JS). Covers weekly options. For same-day expiry (0DTE), Dhan only returns that 1 day anyway.

**LWC v5.2.0 differences from main page (v4.1.3):**
- `crosshair: { mode: 0 }` = Normal (mode:1 = Magnet in v5, opposite of v4!)
- Markers: `LightweightCharts.createSeriesMarkers(series, [])` → plugin with `.setMarkers(markers)`
- Loaded from `cdn.jsdelivr.net/npm/lightweight-charts@5.2.0/dist/lightweight-charts.standalone.production.mjs`

**Key JS functions:**
- `fetchAndDraw(qs, markerTrade, label)` — core fetch/render used by all load paths
- `fromDateFor(expDate)` — returns 30 days before expiry
- `tradeTs(dateStr, timeStr)` — IST string → Unix epoch (IST-as-UTC convention)
- `snapTs(ts, candles)` — snaps trade timestamp to nearest candle (120s window)
- `putMarkers(t, candles)` — entry (arrowDown, option colour) + exit (arrowUp, green/red)
- `initExpiry()` — lazy-loads `/api/option-list` on first By Expiry tab switch
- `switchTab(tab)` — shows/hides `#pane-d` / `#pane-e`

**`/api/option-candles`** — accepts `security_id`+`exchange_segment` directly (from trade row) or does DB lookup by (underlying, option_type, strike, expiry). Supports `interval=1/3/5/15`; 3m is server-side aggregated via `_aggregate_candles()`.

**`/api/option-list`** — returns up to 500 distinct options from trades DB, deduplicated by (underlying, option_type, strike, expiry). Used by By Expiry tab.

---

## Pending / next work

- **5s/15s chart from tick_data** — once NIFTY/SENSEX ticks are confirmed flowing (check after next market open), add a chart interval toggle (1m / 15s / 5s) to the main page. The 15s/5s chart would aggregate `tick_data` rows server-side and render as a separate candle series on the same chart — no new page, no new tab, just toggle the interval in-place.
- **Verify tick collection** — run the DB check below on next market day (9:15 IST) to confirm ticks are being stored:
  ```bash
  sqlite3 /root/trade-analyser/analyser.db \
    "SELECT security_id, COUNT(*) FROM tick_data WHERE security_id IN ('13','51') GROUP BY security_id;"
  ```
- **Spread grouping** — detect and visually group the sell leg + hedge leg of a credit spread (same underlying, same timestamp cluster, opposite strikes). Show as a bracketed pair on the chart with the net credit.
- **Session summary** — daily stats card: total trades, win rate, gross P&L, best/worst trade.
- **Export** — CSV export of trade history for a date range.
- **Open positions indicator** — trades with OPEN status show entry marker only. Add a visual "still open" indicator.
- **SENSEX chart** — Dhan historical data for SENSEX needs the correct security_id verified in production.
- **By Expiry: verify expired options** — Dhan `/charts/intraday` may not return data for options expired more than 5 trading days ago. Need to test with real expired contracts.

---

## Version history (recent)

| Version | Change |
|---|---|
| v119 | Fix chart viewport reset on auto-import — use `refreshTradesTable()` instead of `loadTrades()` |
| v120 | Add `APPLICATION_ROOT` + `ProxyFix` for Nginx reverse proxy at `/analyser/` |
| v121 | Resizable trades panel with drag handle between chart and trades table |
| v122 | Add `tick_data` table + `subscribe_ticks()` for continuous index tick collection |
| v123 | Fix tick subscription: IDX_I broken → use NSE_EQ for NIFTY, BSE_EQ for SENSEX |
| v124 | Add Direction (SHORT/LONG) column to trades table with coloured pill tags |
| v125 | Full HH:MM:SS time display (no truncation) — seconds needed for 15s chart analysis |
| v126 | `table-layout: fixed` with explicit column widths to stop Notes column from overflowing |
| v127 | Fix column widths after `fixed` layout — explicit px on all columns |
| v128 | Widen Strike (70px), Entry (64px), Exit (64px) — were too narrow and truncating values |
| v129–v133 | Collapsible RSI/MACD panes — separate LWC chart instances in height-based boxes (22px collapsed / 90px expanded); RSI/MACD toggle chips in top bar |
| v134 | Fix column headers truncating — Type→54px, Lots→52px, Dir→58px |
| v135 | Fix OPEN→CLOSED trade update: `refreshTradesTable()` now calls `putMarkers()` so exit markers appear on chart; add `_close_stale_open_trades()` at startup to re-import past dates with OPEN trades; extend scheduler to 15:00+16:00 IST triggers; logging for OPEN→CLOSED patch |

---

## How to resume with a new Claude session

1. Open this repo in Claude Code
2. Say: *"Read CLAUDE.md and continue development on the trade analyser"*
3. Claude will read this file and have full context

Active branch: `claude/admiring-einstein-prd40v`
