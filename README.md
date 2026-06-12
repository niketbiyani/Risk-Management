# Nifty Options Risk Management Platform

A single-VPS trade management platform for Nifty/SENSEX options scalping via Dhan broker. Designed for **two-legged credit spreads** (bear call, bull put) on 15-second charts with sub-second order execution and prop-firm-style risk enforcement.

Dashboard: `http://YOUR_VPS_IP:5555`  
Trade Analyser: `http://YOUR_VPS_IP:5556`

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Web Dashboard (:5555)                         │
│  Option Chain │ Spread Quick Bar │ Positions │ DOM/Chart         │
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
│   spreads   │   │   queue          │   │ • Analytics sidebar  │
│ • WS tick   │   │ • Position cache │   │                      │
│   routing   │   │ • Exchange SL    │   │                      │
│ • Auto-     │   │   order tracking │   │                      │
│   journal   │   │                  │   │                      │
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
│  Clean trade P&L · CSV import from Dhan trade book              │
│  /api/trades?date=YYYY-MM-DD · /api/dates · /api/import         │
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
- ATM centering with **hysteresis** (`abs(spot - prev_atm) > strike_interval * 0.6`) — prevents flickering at boundaries
- `_ocRenderedKey` JS variable tracks chain structure (expiry + strikes); controls in-place update vs full DOM rebuild
- **SENSEX**: two-pass `ticker_data` fetch (Dhan limit ~9 IDs/call, 191 total strikes)
  - Pass 1: sample every 10th strike → finds ATM area
  - Pass 2: fetch ATM±8 strikes precisely
  - Spot price from background thread (`_start_bse_spot_updater`) — nearest SENSEX FUTIDX LTP every 3s, stored in `_bse_last_spot`
- Feed auto-restarts after page refresh (60s stale check) and on underlying switch (force flag)
- **Known issue:** Some SENSEX ATM strikes return 0 from `ticker_data` — user types price manually in quick bar

### Spread Quick Bar

One-click spread entry docked at the bottom of the option chain panel.

**Workflow:** Click **[S]** on sell leg → Click **[B]** on buy leg → Enter SL → Click Execute

**Inputs:**
| Field | Behaviour |
|-------|-----------|
| Sell price | Pre-fills from LTP, updates on every tick. Dirty flag `_sqbSellPriceDirty` stops tick overwrite after manual edit. Resets on new leg selection |
| SL | Mandatory (gold border). Must be > sell price to enable execute buttons |
| Buy price | Pre-fills from buy leg LTP, tick-updates. Dirty flag `_sqbBuyPriceDirty` |
| Max Loss ₹ | Auto-calculates lots: `floor(maxLoss / ((sl − sellPrice) × lotSize))` |
| Override qty | Bypass auto-calc |

**Execute buttons:**
| Button | Behaviour |
|--------|-----------|
| ⚡ EXECUTE LMT | BUY hedge @ MARKET → 200ms pause → SELL @ typed LIMIT price |
| ⚡ MKT | BUY hedge @ MARKET → 200ms pause → SELL @ MARKET |
| ARM TRIGGER | Queue spread — fires when sell leg LTP ≤ trigger price |
| SELL MKT | Emergency single-leg SELL (use when hedge filled but sell rejected) |
| BUY MKT | Emergency single-leg BUY (close naked short) |

**Order sequence (critical):**
1. BUY hedge at MARKET (reduces margin requirement first)
2. 200ms pause
3. SELL at LIMIT or MARKET
4. 2.5s poll to confirm no RMS rejection
5. Only after confirmed filled: place exchange-level `STOP_LOSS_MARKET` BUY + register monitored SL

**product_type:** Always `"MARGIN"` (NRML). **Never INTRADAY/MIS** — user explicitly requires this.

### Exchange SL Order Management

After a spread fills, a `STOP_LOSS_MARKET` BUY order sits live at Dhan as hard protection (fires even if VPS crashes). The platform tracks its order ID through the position lifecycle:

- **Full exit** → `POST /api/order/cancel_sl/<sid>` cancels the exchange SL before placing exit (prevents spurious re-entry)
- **Partial exit** → `POST /api/order/replace_sl/<sid>` cancels old SL + places new `STOP_LOSS_MARKET` for remaining quantity at same trigger price
- **SL hit naturally** → position closes via monitor loop, no cancel needed
- Order ID stored in `TradeManager._sl_tp_orders[security_id].exchange_sl_order_id`

**JS flow:** `_placeExitOrder(sid, ..., fullQty)` → if `qty >= fullQty`: cancel_sl then exit. If `qty < fullQty`: exit then replace_sl with `remaining_qty = fullQty - qty`.

### Snapshot Chart (DOM Panel)

- TradingView Lightweight Charts v4.1.3 (CDN: `cdn.jsdelivr.net`)
- 1-minute OHLCV candlesticks via Dhan `intraday_minute_data`
- Shows **today + previous trading day** candles (two API calls on load)
- **Live bar** updates driven by WebSocket tick on every price change
- Auto-loads and switches to Chart tab when sell leg selected via [S]
- `[Depth]` / `[Chart]` tab toggle in DOM panel header
- **Timezone:** Dhan returns IST strings. Parsed naively with `.timestamp()` — correct IST display. Do NOT add `timezone` to timeScale (not supported in v4)
- `_lwCurrentSecurity` tracks which instrument the chart is showing
- 60s auto-refresh interval to pick up completed candles

### Position Table

- All open Dhan positions, regardless of where entered
- Per-position: **EXIT MKT** button and inline **EXIT...** form with 25%/50%/75%/100% qty presets (rounded to nearest lot)
- Global **EXIT ALL** button
- All exits go through `_placeExitOrder(sid, exchangeSeg, prodType, qty, direction, orderType, price, fullQty)`

### WebSocket Integration

**DepthWebSocket** — single connection to `wss://depth-api-feed.dhan.co/twentydepth`:
- One connection handles both **20-level DOM** (selected instrument) and **LTP ticks** (option chain ATM±10 + open positions)
- Merged because Dhan error 805 = max concurrent WebSocket connections exceeded
- Binary packet parser: HEADER_SIZE=50 bytes, FEED_BID=4
- Routes by security_id: `_ltp_sids` → LTP callback; depth instrument → full 20-level parse
- Auto-reconnects every 5s

**OrderUpdate WebSocket** — fires instantly on order state changes:
- Fills → success toast in dashboard
- Rejections → error toast with reason, `SPREAD_FAILED` SocketIO event
- Latency: <200ms from Dhan RMS to dashboard notification

### Risk Management (Prop-Firm Style)

| Rule | Behaviour |
|------|-----------|
| Daily Max Loss | Lockout when total P&L (realized + unrealized) hits limit |
| Daily Profit Target | Optional lockout when target is reached |
| Profit Lock | Once realized P&L crosses threshold, locks a % floor (e.g. ₹10k earned → ₹5k locked) |
| Trailing Drawdown | Tracks HWM of **realized** P&L; locks out if total P&L falls by > % from HWM |
| Cooldown Timer | Enforced pause after single loss or N consecutive losses |
| Max Open Positions | Blocks new trades above limit |
| Max Single Trade Risk | Blocks orders exceeding per-trade risk ₹ |

**Trailing Drawdown note:** HWM is based on **realized P&L only** (not unrealized). Unrealized fluctuates on every tick and would cause false lockouts if included in HWM calculation. Drawdown is still measured against total P&L so paper losses do count toward the limit.

**Lockout sequence:** cancel all pending orders → close all positions at market → activate Dhan Kill Switch → encrypt and persist state (tamper-proof, resets at next IST trading day)

### Trade Analyser (port 5556)

A separate app handles all trade P&L analysis:
- `GET /api/trades?date=YYYY-MM-DD` — list of trades with entry/exit prices, P&L, legs
- `GET /api/dates` — dates for which trade data exists
- `GET /api/import` — re-imports today's trades from Dhan trade book
- Dashboard `/journal` button redirects to `http://<host>:5556`
- Dashboard analytics sidebar proxies from trade-analyser's `/api/trades` endpoint

### Auto-Journal (monitor.py)

The monitor automatically creates journal entries from live order data, with deduplication:
- `_auto_journal_orders()` runs on every position poll cycle
- DB-level dedup: `get_today_security_ids()` checked before creating any entry
- In-memory `_journaled_order_ids` set prevents same order being processed twice
- Second pass: scans open entries and closes them when matching BUY orders appear
- **Order data fallback chain:** trade_book → order_book → trade_history → `order_cache.json`
  - Order book clears after market close — `order_cache.json` persisted to disk during session
  - Trade history endpoint uses DD-MM-YYYY format (Dhan requirement)

### CSV Import (Dhan Trade Book)

Upload a Dhan trade book export CSV via the dashboard to backfill the journal:
- `POST /api/journal/upload_csv` — accepts multipart file upload
- Expected columns: `Trade #`, `Stock Name`, `Transaction`, `Product Type`, `Quantity`, `Price (₹)`, `Net Amount (₹)`, `Timestamp`
- Timestamp format: `11 Jun 2026 12:45:52` (`%d %b %Y %H:%M:%S`)
- Bidirectional FIFO pairing: one queue per symbol; each row pairs with the next opposite-transaction row
- Correctly handles both SELL-first (spread) and BUY-first (hedge leg first) sequences
- Lot sizes: NIFTY=75, SENSEX=20, BANKNIFTY=35

---

## State Management

State is encrypted and tamper-proof (`state_manager.py` + `cryptography.fernet`):
- AES-128-CBC encryption + SHA-256 HMAC integrity hash
- State auto-resets at **IST midnight** (not UTC) — uses explicit UTC+5:30 timezone
- Lockouts cannot be reversed within the trading day (by design)
- Emergency unlock: `POST /api/admin/unlock` (for testing/recovery only)
- Key stored in `state/.state_key` (600 permissions) or `STATE_ENCRYPTION_KEY` env var

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
```

> **Dhan rate limit:** Token generation is limited to once per 2 minutes. If the service restart-loops, stop it, wait 130s, then start.

---

## API Reference

### Status & Positions

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Full risk status, positions, P&L, lockout state |
| `/api/positions` | GET | Open positions with SL/TP levels |

### Orders

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/order/place` | POST | Place a single order |
| `/api/order/place_spread` | POST | Execute spread immediately or queue with trigger price |
| `/api/order/cancel_sl/<sid>` | POST | Cancel exchange SL order before full exit |
| `/api/order/replace_sl/<sid>` | POST | Replace exchange SL with new quantity after partial exit |
| `/api/order/calculate_size` | POST | Auto-calc lot quantity from max risk + SL width |
| `/api/sl` | POST | Set monitored stop loss on position |
| `/api/tp` | POST | Set monitored take profit on position |
| `/api/exit_all` | POST | Emergency close all open positions |

### Option Chain & Charts

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/option_chain` | GET | Fetch option chain (OI, IV, strikes) |
| `/api/option_chain/subscribe_ltp` | POST | Subscribe instruments to LTP WebSocket feed |
| `/api/chart/<security_id>` | GET | Intraday 1m OHLC candles (today + prev day) |
| `/api/chart/nifty` | GET | Nifty index 1m candles (security_id=13, NSE_EQ, INDEX) |

### Journal

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/journal/entry` | POST | Create a journal entry |
| `/api/journal/entry/<id>` | PUT | Update exit price (`action: 'exit'`) or notes (`action: 'notes'`) |
| `/api/journal/entries` | GET | List journal entries |
| `/api/journal/open_entry/<sid>` | GET | Find most recent open entry by sell security_id |
| `/api/journal/screenshot` | POST | Upload entry/exit screenshot (base64 PNG) |
| `/api/journal/screenshots/<filename>` | GET | Serve screenshot PNG |
| `/api/journal/daily_summaries` | GET | Daily P&L summary (proxied from trade-analyser) |
| `/api/journal/analytics` | GET | Aggregated analytics (proxied from trade-analyser) |
| `/api/journal/upload_csv` | POST | Import Dhan trade book CSV export |
| `/api/journal/clear_date` | POST | Delete all journal entries for a given date |

### Admin

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/unlock` | POST | Emergency state reset (bypasses lockout — use carefully) |
