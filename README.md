# Trade Analyser

Post-session option trade review tool. Imports your executed trades from Dhan and plots entry/exit markers on the underlying index chart (Nifty, Sensex, Banknifty) so you can visually review every trade after the session.

Since option charts aren't accessible after expiry, trades are overlaid on the **spot index** chart instead — giving you full context of where the market was when you entered and exited.

---

## What it looks like

**Main page** (`/`):
- **Top bar** — date picker with prev/next arrows, underlying selector (NIFTY / SENSEX / BANKNIFTY), direction filter (Short / Hedge), CE/PE filter, import button, token refresh button
- **Chart** — 1-minute TradingView candlestick chart of the index with EMA 20 (blue) and EMA 50 (orange)
  - Blue `▼` markers for CE entries, amber `▼` for PE entries
  - Green `▲` for profitable exits, red `▲` for losing exits
- **RSI 14** — oscillator panel below the main chart, synced scrolling
- **MACD 12,26,9** — histogram + signal panel below RSI, synced scrolling
- **Resizable trades panel** — drag the handle between the chart and table to resize. Click any row to isolate that trade on the chart. Click again to show all. Add notes inline — saves automatically and is preserved across wipe+reimport.

**Option Chart page** (`/option-chart`):
- View the premium chart for any option you traded — useful for reviewing how the option moved, not just the index
- Two browse modes: By Date (see all trades for a day) or By Expiry (browse by underlying → expiry → strike)
- Entry and exit markers on the option premium chart with green/red colouring

---

## Prerequisites

- Python 3.11+
- A Dhan account with API access enabled
- TOTP configured on your Dhan account (for auto token refresh)

---

## First-time setup on VPS

```bash
cd ~
git clone https://github.com/niketbiyani/trade-analyser
cd trade-analyser
git checkout claude/admiring-einstein-prd40v

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env          # fill in credentials (see Configuration below)
```

---

## Configuration

Edit `.env` with your Dhan credentials:

```env
# Your Dhan client ID (shown on the Dhan web portal under API settings)
DHAN_CLIENT_ID=your_client_id_here

# Current Dhan access token — copy from your risk-management .env if already set up
DHAN_ACCESS_TOKEN=your_access_token_here

# Your 6-digit Dhan login PIN (used for auto token regeneration)
DHAN_PIN=123456

# TOTP secret from Dhan (base32 string — NOT the 6-digit code)
# Get it from: web.dhan.co → Profile → DhanHQ Trading APIs → Setup TOTP → "show secret"
DHAN_TOTP_SECRET=YOUR_BASE32_SECRET

# Port to run on (default 5556)
PORT=5556

# Optional: path prefix when running behind Nginx (e.g. /analyser)
# Leave blank if accessing the app directly by IP:port
APPLICATION_ROOT=
```

> If you already have this configured in your risk-management project, use the same `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `DHAN_PIN`, and `DHAN_TOTP_SECRET` values — it's the same Dhan account.

---

## Running as a systemd service (always-on)

This is the recommended way to run the app — it starts automatically on VPS boot and restarts itself if it ever crashes.

### 1. Create the service file

```bash
sudo nano /etc/systemd/system/trade-analyser.service
```

Paste this content (adjust paths if your username isn't `root`):

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

### 2. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable trade-analyser    # auto-start on boot
sudo systemctl start trade-analyser
sudo systemctl status trade-analyser    # confirm it's running
```

Dashboard: `http://YOUR_VPS_IP:5556`

### Once it's running — it stays running

Once enabled and started, you don't need to do anything. The service:
- Starts automatically when the VPS boots
- Restarts itself within 5 seconds if it crashes
- Keeps running indefinitely unless you stop it manually
- Continuously collects NIFTY and SENSEX index ticks in the background (for future 5s/15s chart analysis)

---

## Setting up behind Nginx (optional)

If you want to serve the app at a path prefix like `http://YOUR_VPS_IP/analyser/` instead of directly on port 5556, configure Nginx like this:

```nginx
location /analyser/ {
    proxy_pass         http://127.0.0.1:5556/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   X-Forwarded-Prefix /analyser;
}
```

Then add `APPLICATION_ROOT=/analyser` to your `.env` and restart the service. The trailing slash in `proxy_pass` is required.

---

## Day-to-day commands

### Check if it's running
```bash
sudo systemctl status trade-analyser
```

### View logs
```bash
# Try the file first:
tail -f /root/trade-analyser/analyser.log

# If empty, use the systemd journal:
sudo journalctl -u trade-analyser -n 50 -f
```

### Restart (after pulling updates)
```bash
cd ~/trade-analyser
git pull origin claude/admiring-einstein-prd40v
sudo systemctl restart trade-analyser
```

### Stop the service
```bash
sudo systemctl stop trade-analyser
```

### Disable auto-start (if you no longer want it running on boot)
```bash
sudo systemctl disable trade-analyser
```

---

## Pulling updates

```bash
cd ~/trade-analyser
git pull origin claude/admiring-einstein-prd40v
sudo systemctl restart trade-analyser
sudo journalctl -u trade-analyser -n 30
```

---

## How to use

### Importing trades

1. Click **↓ Import** in the top bar
2. Set **From Date** and **To Date** (you can import weeks or months at once)
3. Click **Import**
4. The app fetches your trade history from Dhan, filters to options only (NSE_FNO / BSE_FNO), reconstructs positions using FIFO pairing (handles partial fills, re-entries, spreads), and stores everything locally
5. Re-importing the same date range is safe — already-stored trades are skipped. Any notes you've added are preserved.

### Wipe and reimport

Use the **Wipe & reimport** button if you want a completely fresh import for a date (e.g. after a fix or data issue). Your notes are automatically backed up and restored after the reimport — they will not be lost.

### Viewing trades on the chart

- Use the **date picker** or **← →** arrows to navigate between days
- Switch between **NIFTY / SENSEX / BANKNIFTY** with the underlying chips
- Toggle **CE** / **PE** / **Short** / **Hedge** chips to show/hide each type
- **Click any row** in the trades panel to isolate that trade on the chart (other markers hidden). Click again to show all.
- **Drag the handle** between the chart and the trades table to resize the panel

### Adding notes

Click the **Notes** cell in any trade row, type your note, then click away — saves automatically. Notes are linked to the trade by instrument + entry time, so they survive a wipe-and-reimport.

### Token management

The **↻ Token** button manually refreshes your Dhan access token. Token is also refreshed automatically:
- On app startup
- If an import call fails with an auth error (auto-retry once)

---

## Chart and indicators

- **Always 1-minute candles** via Dhan's historical API (supports up to 5+ trading days of history)
- Loads **today + previous trading day** data so EMAs are warm from the start of the session
- Markers are only shown for the selected date — not the previous day
- **EMA 20** (blue), **EMA 50** (orange) overlaid on candles
- **RSI 14** in pane below — 70/30 reference lines
- **MACD 12,26,9** in bottom pane — histogram + signal line
- All three panes scroll and zoom in sync

---

## Chart markers explained

| Marker | Meaning |
|---|---|
| Blue `▼` above candle | CE trade entry (short call) |
| Amber `▼` above candle | PE trade entry (short put) |
| Blue `▽` above candle | CE hedge entry (long call) |
| Amber `▽` above candle | PE hedge entry (long put) |
| Green `▲` below candle | Exit at a profit |
| Red `▲` below candle | Exit at a loss |
| Number on exit marker | P&L in ₹ for that trade |

---

## Trade matching logic

Dhan's trade history returns individual executions (including partial fills). The app reconstructs positions as follows:

1. Group all option trades by `(date, securityId)`
2. Aggregate partial fills — same `orderId` records are merged (summed quantity, weighted-avg price)
3. Use FIFO position tracking: BUY closes oldest open SHORT; SELL closes oldest open LONG. Surplus opens a new position in the opposite direction.
4. This handles re-entries, partial closes, same-option multiple positions, and spread legs all correctly.
5. P&L for short = `(entry_price - exit_price) × quantity`; for hedge = `(exit_price - entry_price) × quantity`

---

## Tick data collection

The app continuously collects NIFTY and SENSEX index ticks from Dhan's MarketFeed while the service is running. Ticks are stored in the `tick_data` table in SQLite. This data will be used to build 5-second and 15-second charts on the main page — letting you review trades at finer granularity than 1-minute candles.

To check tick collection is working (run after market opens at 9:15 IST):
```bash
sqlite3 /root/trade-analyser/analyser.db \
  "SELECT security_id, COUNT(*) as ticks FROM tick_data WHERE security_id IN ('13','51') GROUP BY security_id;"
```
- `13` = NIFTY, `51` = SENSEX

---

## Troubleshooting

**Chart shows no data**
- If the date is a weekend or market holiday, no candles are available — expected
- Dhan historical API only returns data for trading days with market activity

**Import returns 0 trades**
- Check the date range actually had trades executed through Dhan
- Futures (FUTIDX) are filtered out — only options (OPTIDX) are imported
- Equity trades (NSE_EQ / BSE_EQ) are filtered out

**Import fails with auth error**
- The app will auto-retry once after refreshing the token
- If it still fails, click **↻ Token** in the top bar
- Make sure `DHAN_PIN` and `DHAN_TOTP_SECRET` are set correctly in `.env`

**Token generation rate limited**
- Dhan rate-limits token generation to once per 2 minutes
- The token manager waits 130 seconds and retries automatically — just wait

**P&L shows as `—` for some trades**
- The trade is OPEN (no matching exit found in that session)
- Or the exit happened on a different day

**Service not starting / logs empty**
```bash
sudo journalctl -u trade-analyser -n 50
```
Check the journal output for the specific error.

**App works on port 5556 but not on /analyser/**
- Verify Nginx config has trailing slash on `proxy_pass`: `proxy_pass http://127.0.0.1:5556/;`
- Verify `X-Forwarded-Prefix /analyser` header is set in Nginx
- Verify `APPLICATION_ROOT=/analyser` is set in `.env`
- Restart the service after changing `.env`

---

## Data storage

All imported trades are stored in `analyser.db` (SQLite, in the project directory). Re-importing never overwrites existing records — it only appends new ones. Notes are always preserved.

To delete all trade data and start fresh:
```bash
sudo systemctl stop trade-analyser
rm /root/trade-analyser/analyser.db
sudo systemctl start trade-analyser
```

---

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Main UI |
| `GET` | `/option-chart` | Option premium chart page |
| `POST` | `/api/import` | Import trades from Dhan `{from_date, to_date}` |
| `POST` | `/api/import-csv` | Upload CSV trade file |
| `GET` | `/api/trades` | Get trades `?date=&underlying=&option_type=` |
| `PUT` | `/api/trade/<id>/notes` | Update notes `{notes}` |
| `DELETE` | `/api/trades/date/<date>` | Wipe all trades for a date (backs up notes first) |
| `GET` | `/api/chart` | Get 1m OHLCV candles `?underlying=&date=` |
| `GET` | `/api/option-candles` | Get option premium candles `?security_id=&exchange_segment=&interval=` |
| `GET` | `/api/option-list` | Distinct options from trades DB |
| `GET` | `/api/dates` | List dates with imported trades |
| `POST` | `/api/refresh-token` | Manually refresh Dhan token |
| `GET` | `/api/debug-dhan` | Raw Dhan API response dump `?from_date=&to_date=` |
