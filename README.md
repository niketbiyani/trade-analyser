# Trade Analyser

Post-session option trade review tool. Imports your executed trades from Dhan and plots entry/exit markers on the underlying index chart (Nifty, Sensex, Banknifty) so you can visually review every trade after the session.

Since option charts aren't accessible after expiry, trades are overlaid on the **spot index** chart instead — giving you full context of where the market was when you entered and exited.

---

## What it looks like

- **Top bar** — date picker with prev/next arrows, underlying selector (NIFTY / SENSEX / BANKNIFTY), CE/PE filter chips, token refresh button, import button
- **Chart** — TradingView candlestick chart of the index. Blue `▼` markers for CE entries, amber `▼` for PE entries. Green `▲` for profitable exits, red `▲` for losing exits. Exit markers show the P&L in points.
- **Trades panel** — table of all trades for the selected date. Click any row to scroll the chart to that trade's entry candle. Add notes inline — saves automatically.

---

## Prerequisites

- Python 3.11+
- A Dhan account with API access enabled
- TOTP set up on your Dhan account (for auto token refresh)

---

## Installation

```bash
git clone https://github.com/niketbiyani/trade-analyser
cd trade-analyser
git checkout claude/admiring-einstein-prd40v

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
nano .env
```

```env
# Your Dhan client ID (shown on the Dhan web portal under API settings)
DHAN_CLIENT_ID=your_client_id_here

# Current Dhan access token — copy from your risk-management .env if already set up
DHAN_ACCESS_TOKEN=your_access_token_here

# Your 6-digit Dhan login PIN (used for auto token regeneration)
DHAN_PIN=123456

# TOTP secret from Dhan (base32 string, not a 6-digit code)
# Get it from: https://web.dhan.co -> Profile -> DhanHQ Trading APIs -> Setup TOTP
DHAN_TOTP_SECRET=YOUR_BASE32_SECRET

# Port to run on (default 5556)
PORT=5556
```

### Getting your TOTP secret

1. Go to [web.dhan.co](https://web.dhan.co)
2. Profile → DhanHQ Trading APIs → **Setup TOTP**
3. Instead of scanning the QR code, click "show secret" or "copy key"
4. Paste that base32 string as `DHAN_TOTP_SECRET`

> If you already have this configured in your risk-management project, use the same values — it's the same Dhan account.

---

## Running

```bash
cd ~/trade-analyser
source venv/bin/activate
python app.py
```

Open `http://YOUR_VPS_IP:5556` in your browser.

On startup the app automatically attempts a token refresh if PIN + TOTP are configured.

---

## Running as a systemd service (optional)

Create `/etc/systemd/system/trade-analyser.service`:

```ini
[Unit]
Description=Trade Analyser
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/trade-analyser
ExecStart=/root/trade-analyser/venv/bin/python app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable trade-analyser
sudo systemctl start trade-analyser
sudo systemctl status trade-analyser
```

Logs: `tail -f /root/trade-analyser/analyser.log`

---

## How to use

### Importing trades

1. Click **↓ Import from Dhan** in the top bar
2. Set **From Date** and **To Date** (you can import weeks or months at once)
3. Click **Import**
4. The app fetches your full trade history from Dhan, filters to options only (NSE_FNO / BSE_FNO), pairs each SELL (entry) with the earliest subsequent BUY (exit) for the same instrument, and stores everything locally
5. Duplicate detection — re-importing the same date range is safe, already-stored trades are skipped

### Viewing trades on the chart

- Use the **date picker** or **← →** arrows to navigate between days
- Switch between **NIFTY / SENSEX / BANKNIFTY** with the underlying chips — the chart and trade list update together
- Toggle **CE** and **PE** chips to show/hide each type
- **Click any row** in the trades panel to zoom the chart to that trade's entry candle

### Chart resolution

The chart automatically picks the best resolution based on how old the date is:

| Age of date | Resolution |
|---|---|
| Last 5 days | 1-minute candles |
| Last 55 days | 5-minute candles |
| Older | Daily candles |

The resolution badge in the top bar shows which is active.

### Adding notes

Click the **Notes** cell in any trade row, type your note, then click away. It saves automatically. Notes persist across sessions.

### Token management

The **↻ Token** button in the top bar manually refreshes your Dhan access token (renew first, regenerate via PIN+TOTP if expired). The token is also refreshed automatically:
- On app startup
- If an import call fails with an auth error (auto-retry once)

---

## Chart markers explained

| Marker | Meaning |
|---|---|
| Blue `▼` above candle | CE trade entry (you sold a call) |
| Amber `▼` above candle | PE trade entry (you sold a put) |
| Green `▲` below candle | Exit at a profit |
| Red `▲` below candle | Exit at a loss |
| Number on exit marker | P&L in ₹ for that trade |

Markers are snapped to the nearest available candle if the exact minute is missing from the chart data.

---

## Trade matching logic

Dhan's trade history returns individual executions. The app reconstructs positions as follows:

1. Group all option trades by `(date, securityId)`
2. Within each group, sort SELLs and BUYs by time
3. Each SELL = opening a short position (entry)
4. Match each SELL with the earliest BUY that occurs **after** it with the same quantity
5. P&L = `(entry_price - exit_price) × quantity`

For **credit spreads**, both legs appear as separate trades (the sell leg and the hedge buy leg each get their own marker). Spread grouping is not yet implemented — it's on the roadmap.

For **naked shorts**, only one SELL→BUY pair appears.

---

## Data storage

All imported trades are stored in `analyser.db` (SQLite, in the project directory). The database is never overwritten on re-import — it only appends new trades.

To reset and re-import everything from scratch:
```bash
rm analyser.db
```

---

## Troubleshooting

**Chart shows no data**
- If the date is a weekend or market holiday, yfinance returns no candles — this is expected
- For dates older than 55 days, only daily candles are available
- SENSEX (`^BSESN`) on yfinance sometimes has data gaps — this is a yfinance limitation

**Import returns 0 trades**
- Check that the date range had actual trades executed through Dhan
- Futures (FUTIDX) are filtered out — only options (OPTIDX) are imported
- Equity trades (NSE_EQ / BSE_EQ) are filtered out

**Import fails with auth error**
- The app will auto-retry once after refreshing the token
- If it still fails, click **↻ Token** in the top bar to force a refresh
- Make sure `DHAN_PIN` and `DHAN_TOTP_SECRET` are set correctly in `.env`

**Token generation rate limited ("once every 2 minutes")**
- Dhan rate-limits token generation to once per 2 minutes
- The token manager waits 130 seconds and retries automatically — just wait

**P&L shows as `—` for some trades**
- The trade is still OPEN (no matching BUY found in that session)
- Or the BUY happened on a different day (e.g. overnight position)

---

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Main UI |
| `POST` | `/api/import` | Import trades from Dhan `{from_date, to_date}` |
| `GET` | `/api/trades` | Get trades `?date=&underlying=&option_type=` |
| `PUT` | `/api/trade/<id>/notes` | Update notes `{notes}` |
| `GET` | `/api/chart` | Get OHLCV candles `?underlying=&date=` |
| `GET` | `/api/dates` | List dates with imported trades |
| `POST` | `/api/refresh-token` | Manually refresh Dhan token |
