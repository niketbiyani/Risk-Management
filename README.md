# Nifty Options Risk Management Platform

A single-VPS trade management platform for Nifty/SENSEX options scalping via Dhan broker. Designed for **two-legged credit spreads** (bear call, bull put) on 15-second charts with sub-second order execution and prop-firm-style risk enforcement.

Dashboard: `http://YOUR_VPS_IP` (password protected via Nginx)
Trade Analyser: `http://YOUR_VPS_IP:5556`
Analytics: `http://YOUR_VPS_IP/analytics`
Mobile: `http://YOUR_VPS_IP/mobile`

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Nginx (:80) — password protected              │
│  Basic Auth · Reverse proxy to :5555 · /analyser/ → :5556       │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│              Web Dashboard (:5555) — mobile responsive           │
│  Option Chain │ Spread Quick Bar │ Positions │ DOM/Chart         │
│  Equity Curve │ Trailing Drawdown │ Analytics │ /mobile view     │
└───────────────────────────┬─────────────────────────────────────┘
                            │ SocketIO (real-time events)
┌───────────────────────────▼─────────────────────────────────────┐
│                      Flask Backend (dashboard.py)                │
│  All HTML/CSS/JS inline · All API routes · SocketIO server       │
└──────┬────────────────────┬──────────────────────┬──────────────┘
       │                    │                      │
┌──────▼──────┐   ┌─────────▼────────┐   ┌────────▼─────────────┐
│  monitor.py  │   │ trade_manager.py │   │   trade_journal.py   │
│             │   │                  │   │                      │
│ • Position  │   │ • SL/TP orders   │   │ • SQLite journal DB  │
│   poll 2s   │   │ • Spread detect  │   │ • Entry/exit entries │
│ • Execute   │   │ • Pending spread │   │ • Screenshots        │
│   spreads   │   │   queue          │   │                      │
│ • WS tick   │   │ • Position cache │   │                      │
│   routing   │   │ • Exchange SL    │   │                      │
│ • Auto-     │   │   order tracking │   │                      │
│   journal   │   │                  │   │                      │
│ • Analyser  │   │                  │   │                      │
│   P&L sync  │   │                  │   │                      │
└──────┬──────┘   └──────────────────┘   └──────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────────┐
│                        dhan_api.py                               │
│  REST wrapper · DepthWebSocket (LTP + DOM) · OrderUpdate WS     │
└──────┬──────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────────┐
│                        Dhan Broker API                           │
│  Orders · Positions · Option Chain · Kill Switch · Market Feed  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│               Trade Analyser (:5556) — separate app             │
│  FIFO P&L calculation · /api/trades · /api/dates · /api/import  │
│  Used by: equity curve, analytics page, HWM realized P&L source │
└─────────────────────────────────────────────────────────────────┘
```

---

## Features

### Option Chain

- Supports **NIFTY** (NSE_FNO, IDX_I) and **SENSEX** (BSE_FNO)
- Full chain with CE/PE LTP, OI, IV, strike displayed per row
- Each row has **[S]** and **[B]** pill buttons for one-click spread leg selection
- **Real-time LTP** for ATM±10 strikes via single DepthWebSocket (<50ms) — cells update in-place without re-rendering
- REST poll every 2s for **OI and IV only** (LTP is WebSocket only)
- ATM centering with **hysteresis** — prevents flickering at strike boundaries
- **SENSEX**: two-pass `ticker_data` fetch (Dhan limit ~9 IDs/call)
- Feed auto-restarts after page refresh and on underlying switch
- Hidden on mobile screens (<768px)

### Spread Quick Bar

One-click spread entry docked at the bottom of the option chain panel.

**Workflow:** Click **[S]** on sell leg → Click **[B]** on buy leg → Enter SL → Click Execute

**Inputs:**
| Field | Behaviour |
|-------|-----------|
| Sell price | Pre-fills from LTP, tick-updates. Dirty flag stops overwrite after manual edit |
| SL | Mandatory (gold border). Must be > sell price to enable execute |
| Buy price | Pre-fills from buy leg LTP, tick-updates |
| Max Loss ₹ | Auto-calculates lots: `floor(maxLoss / ((sl − sellPrice) × lotSize))` |
| Override qty | Bypass auto-calc |

**Execute buttons:**
| Button | Behaviour |
|--------|-----------|
| ⚡ EXECUTE LMT | BUY hedge @ MARKET → 200ms → SELL @ typed LIMIT |
| ⚡ MKT | BUY hedge @ MARKET → 200ms → SELL @ MARKET |
| ARM TRIGGER | Queue spread — fires when sell leg LTP ≤ trigger price |
| SELL MKT | Emergency single-leg SELL |
| BUY MKT | Emergency single-leg BUY |

**product_type:** Always `"MARGIN"` (NRML). **Never INTRADAY/MIS.**

### Exchange SL Order Management

After a spread fills, a `STOP_LOSS_MARKET` BUY order sits live at Dhan as hard protection:
- **Full exit** → cancel exchange SL before placing exit
- **Partial exit** → replace exchange SL with new quantity at same trigger price
- **SL hit naturally** → monitor loop handles, no cancel needed

### Risk Management (Prop-Firm Style)

| Rule | Behaviour |
|------|-----------|
| Daily Max Loss | Lockout when total P&L (realized + unrealized) hits limit |
| Daily Profit Target | Optional lockout when target reached |
| Profit Lock | Once realized ≥ threshold, locks a % floor (e.g. ₹10k earned → ₹5k locked) |
| Trailing Drawdown | HWM of **realized** P&L only; lockout if total falls > % from HWM |
| Max Open Positions | Blocks new orders above limit |
| Max Single Trade Risk | Blocks orders exceeding per-trade risk |

**Critical design:** HWM uses **realized P&L only** (not unrealized). Unrealized fluctuations don't advance HWM — prevents false lockouts from paper gains.

**Critical design:** Realized P&L sourced from **trade-analyser** (FIFO), not Dhan's `realizedProfit` field (which uses average price math and inflates by ~40% on active days).

### Trailing Drawdown Panel

Shows "gap to lockout" as the primary metric:
- Gap = how far total P&L is above the lockout floor
- Progress bar: green (safe) → amber (approaching) → red (danger)
- Inactive until realized P&L crosses `PROFIT_LOCK_THRESHOLD`

### Equity Curve

Replaces the old tick-by-tick intraday P&L chart:
- One dot per closed trade (green = profit, red = loss)
- Cumulative realized P&L line in blue
- Red dashed lockout floor line when profit lock is active
- **Time labels** on x-axis showing trade exit time (HH:MM)
- **Zoom:** mouse wheel zooms x-axis, click-drag pans, double-click resets, pinch zoom on mobile
- Win/loss count + win rate in card header
- Refreshes every 60s — only updates when trades close, no tick noise
- Data from trade-analyser FIFO calculation (not Dhan's inflated average-price figure)
- CDN dependencies: `hammerjs@2.0.8` + `chartjs-plugin-zoom@2.0.1` loaded after Chart.js

### Snapshot Chart (DOM Panel)

- TradingView Lightweight Charts v4.1.3
- 1-minute OHLCV candlesticks (today + previous day)
- Live bar updates from WebSocket ticks
- Auto-loads when sell leg selected via [S]
- `[Depth]` / `[Chart]` tab toggle

### Analytics Page (`/analytics`)

- **FY toggle** — filter stats by financial year (Apr–Mar). Format: `FY 2025-26`
- **Stats bar** — Total Days/Trades, P&L, Win Rates, Best/Worst Day — filtered to selected FY
- **Calendar view** — monthly calendar, click day for detail
- **Day detail** — stats + trade table with CE/PE filter tabs + equity curve at bottom
- **Mobile responsive** — 2-col stat grid, compact calendar on small screens
- Silent background refresh every 2 minutes

### Mobile Support

- **Auto-detect** (`/`): option chain/DOM hidden on screens <768px; core cards remain
- **Dedicated view** (`/mobile`): touch-optimised single-column layout with large buttons, P&L, positions with exit controls, equity curve, auto-refresh every 3s

### WebSocket Integration

**DepthWebSocket** — single connection handles both DOM and LTP:
- Merged to avoid Dhan error 805 (max concurrent connections)
- LTP for ATM±10 strikes + open positions
- 20-level DOM for selected instrument
- Auto-reconnects every 5s

**OrderUpdate WebSocket** — instant order state notifications:
- Fills → success toast
- Rejections → error toast + `SPREAD_FAILED` event
- Latency: <200ms

### Trade Analyser Integration

The trade-analyser (`niketbiyani/trade-analyser`, port 5556) is the source of truth for P&L:
- Monitor syncs realized P&L every 60s via background thread
- Equity curve and analytics page both pull from trade-analyser
- Auto-triggers `/api/import` before fetching to ensure fresh data

---

## Infrastructure

### Nginx (Reverse Proxy + Auth)

```nginx
server {
    listen 80;
    auth_basic "Trading Dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:5555;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_http_version 1.1;
    }

    location /analyser/ {
        proxy_pass http://127.0.0.1:5556;
    }
}
```

Reset password: `sudo htpasswd /etc/nginx/.htpasswd trader`

### Firewall (UFW)

```
22   ALLOW  (SSH)
80   ALLOW  (Nginx)
5555 DENY   (direct access blocked)
5556 ALLOW  (trade analyser — direct until prefix routing fixed in analyser app)
```

---

## State Management

State is encrypted and tamper-proof (`state_manager.py`):
- Fernet encryption (AES-128-CBC + SHA-256 HMAC)
- Resets at **IST midnight** (not UTC) — VPS runs UTC, uses explicit UTC+5:30
- Lockouts cannot be reversed within the trading day
- Emergency unlock: `POST /api/admin/unlock`
- Surgical HWM reset: `POST /api/admin/reset_hwm` (resets HWM + profit lock without clearing full state)

---

## Setup

### 1. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:
```
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_access_token
SECRET_KEY=any_random_string_for_flask

# Risk limits
DAILY_MAX_LOSS=5000
DAILY_PROFIT_TARGET=20000
MAX_OPEN_POSITIONS=5
MAX_SINGLE_TRADE_RISK=2000

# Profit lock
PROFIT_LOCK_THRESHOLD=10000
PROFIT_LOCK_PERCENTAGE=50

# Trailing drawdown
TRAILING_DRAWDOWN_ENABLED=true
TRAILING_DRAWDOWN_PERCENTAGE=40

# Cooldowns (seconds)
COOLDOWN_AFTER_LOSS=300
COOLDOWN_AFTER_CONSECUTIVE_LOSSES=600
CONSECUTIVE_LOSS_COUNT=3
```

### 3. Run

**As a systemd service (recommended for VPS):**
```bash
sudo bash install_service.sh
sudo systemctl start risk-manager
```

**Manual:**
```bash
python main.py
```

---

## VPS Workflow

```bash
# Pull latest branch
git pull origin claude/laughing-fermi-9HTg6

# Clean restart
sudo systemctl stop risk-manager && > /root/Risk-Management/platform.log && sudo systemctl start risk-manager

# Watch logs
tail -f /root/Risk-Management/platform.log

# Status
sudo systemctl status risk-manager

# Reset Nginx password
sudo htpasswd /etc/nginx/.htpasswd trader
```

> **Dhan rate limit:** Token generation is limited to once per 2 minutes. If the service restart-loops, stop it, wait 130s, then start.

> **Memory:** VPS has 848MB RAM + 1GB swap. Check with `free -h`. If swap usage spikes, restart the service.

---

## API Reference

### Status & Positions

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Full risk status, positions, P&L, lockout state |
| `/api/positions` | GET | Open positions with SL/TP levels |
| `/api/equity_curve` | GET | Today's per-trade realized P&L from trade-analyser |

### Orders

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/order/place` | POST | Place a single order |
| `/api/order/place_spread` | POST | Execute spread immediately or queue with trigger price |
| `/api/order/cancel_sl/<sid>` | POST | Cancel exchange SL before full exit |
| `/api/order/replace_sl/<sid>` | POST | Replace exchange SL after partial exit |
| `/api/order/calculate_size` | POST | Auto-calc lot quantity from max risk + SL width |
| `/api/sl` | POST | Set monitored stop loss |
| `/api/tp` | POST | Set monitored take profit |
| `/api/exit_all` | POST | Emergency close all positions |

### Option Chain & Charts

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/option_chain` | GET | Fetch option chain (OI, IV, strikes) |
| `/api/option_chain/subscribe_ltp` | POST | Subscribe instruments to LTP WebSocket |
| `/api/chart/<security_id>` | GET | Intraday 1m OHLC candles (today + prev day) |
| `/api/chart/nifty` | GET | Nifty index 1m candles |

### Analytics

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/analyser/dates` | GET | Dates with trade data (proxied from trade-analyser) |
| `/api/analytics/day_trades` | GET | Trades for a specific date (`?date=YYYY-MM-DD`) |

### Journal

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/journal/entry` | POST | Create a journal entry |
| `/api/journal/entry/<id>` | PUT | Update exit price or notes |
| `/api/journal/entries` | GET | List journal entries |
| `/api/journal/open_entry/<sid>` | GET | Find most recent open entry |
| `/api/journal/screenshot` | POST | Upload screenshot (base64 PNG) |
| `/api/journal/upload_csv` | POST | Import Dhan trade book CSV |

### Admin

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/unlock` | POST | Emergency state reset (bypasses lockout) |
| `/api/admin/reset_hwm` | POST | Reset HWM + profit lock without clearing full state |
| `/api/admin/reload_config` | POST | Reload configuration variables from `.env` dynamically in memory |

---

## Troubleshooting

### 1. Disk Exhaustion (`No space left on device`)
* **Symptom:** The service exits immediately on startup with `status=1/FAILURE`. Checking logs using `sudo journalctl -u risk-manager.service -n 50` shows a traceback containing:
  ```
  OSError: [Errno 28] No space left on device
  ```
* **Cause:** The server disk is 100% full, preventing the application or systemd from writing log files (`platform.log` or system journal). This also crashes token refresh attempts, which truncates the `.env` file to `0` bytes.
* **Resolution:**
  1. Check disk space distribution:
     ```bash
     df -h
     ```
  2. Locate the largest folders inside `/root` (or top-level) without writing to temporary storage:
     ```bash
     du -h --max-depth=1 /
     du -h --max-depth=1 /root
     ```
  3. Clean up space immediately:
     * Truncate large platform logs:
       ```bash
       > /root/Risk-Management/platform.log
       ```
     * Vacuum system journals (keeping only last 100MB):
       ```bash
       sudo journalctl --vacuum-size=100M
       ```
     * Clean package manager cache:
       ```bash
       sudo apt-get clean
       ```
     * Remove unused folders (e.g., old projects or backups):
       ```bash
       rm -rf /root/risk_guardian /root/dom-analyzer
       rm -rf /root/backups/*
       ```

### 2. Empty / Corrupted `.env` File
* **Symptom:** The service logs error messages stating:
  ```
  Config error: DHAN_CLIENT_ID is required
  Config error: DHAN_ACCESS_TOKEN is required
  ```
* **Cause:** When the disk runs out of space, the automated token manager attempting to write a renewed token to `.env` truncates the file to 0 bytes but fails to write the contents, leaving the file completely empty.
* **Resolution:**
  1. Verify if `.env` is empty:
     ```bash
     cat /root/Risk-Management/.env
     ```
  2. Copy settings from the `trade-analyser` project if available:
     ```bash
     cp /root/trade-analyser/.env /root/Risk-Management/.env
     ```
  3. Edit `.env` to configure port `5555`:
     ```bash
     nano /root/Risk-Management/.env
     ```
     * Ensure `DASHBOARD_PORT=5555` is set.
     * Ensure risk parameters (e.g., `DAILY_MAX_LOSS=5000`) are appended to the bottom.

### 3. API Invalid Warnings (Expired/Missing Token)
* **Symptom:** Quotes do not load or the UI throws an "api invalid" warning.
* **Cause:** Your Dhan access token is expired or invalid.
* **Resolution (Manual):**
  * Generate a new access token on `web.dhan.co` -> Profile -> DhanHQ Trading APIs.
  * Update `DHAN_ACCESS_TOKEN=your_new_token` in `/root/Risk-Management/.env` and run `sudo systemctl restart risk-manager`.
* **Resolution (Automatic - Recommended):**
  * Set up TOTP on `web.dhan.co` -> Profile -> DhanHQ Trading APIs -> Setup TOTP.
  * Paste your 6-digit PIN and the TOTP alphanumeric secret key into your `.env`:
    ```env
    DHAN_PIN=your_dhan_pin
    DHAN_TOTP_SECRET=your_totp_secret
    ```
  * Restart the service. The system will automatically fetch a fresh token on startup and renew it every 12 hours.

### 4. Managing Auto-Restarts and Daily Maintenance
The VPS has a systemd timer setup to automatically restart the risk manager at 8:45 AM IST every morning to refresh API tokens. 

* **To stop auto-restarting completely** (e.g., over weekends or holiday periods):
  ```bash
  # Stop and disable the daily 8:45 AM IST restart timer
  sudo systemctl stop risk-manager-restart.timer
  sudo systemctl disable risk-manager-restart.timer
  
  # Disable the main risk-manager from starting on server boot
  sudo systemctl disable risk-manager
  ```
* **To turn auto-restarts back on**:
  ```bash
  # Re-enable and start the daily restart timer and boot-service
  sudo systemctl enable --now risk-manager-restart.timer risk-manager
  ```
* **To check timer status**:
  ```bash
  sudo systemctl status risk-manager-restart.timer
  ```

