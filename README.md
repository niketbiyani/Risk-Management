# Nifty Options Risk Management Platform

A single-VPS trade management platform for Nifty/SENSEX options scalping via Dhan broker. Designed for **two-legged credit spreads** (bear call, bull put) on 15-second charts with sub-second order execution and prop-firm-style risk enforcement.

Dashboard: `http://YOUR_VPS_IP:5555`  
Journal: `http://YOUR_VPS_IP:5555/journal`

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Web Dashboard (:5555)                         │
│   Option Chain │ Spread Quick Bar │ Positions │ DOM │ Journal    │
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
│   spreads   │   │   queue          │   │ • Analytics          │
│ • WS tick   │   │ • Position cache │   │                      │
│   routing   │   │ • Exchange SL    │   │                      │
│             │   │   order tracking │   │                      │
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
```

---

## Features

### Option Chain

- Supports **NIFTY** (NSE_FNO) and **SENSEX** (BSE_FNO)
- Full chain rendered with CE/PE LTP, OI, IV, strike
- Each row has **[S]** and **[B]** pill buttons for one-click spread leg selection
- **Real-time LTP** for ATM±10 strikes via single DepthWebSocket (<50ms latency) — updates cells in-place without re-rendering
- REST poll every 2s for **OI and IV data only** (LTP no longer comes from REST)
- ATM centering with **hysteresis** (±30pts) to prevent flickering at strike boundaries
- **SENSEX**: two-pass `ticker_data` fetch (Dhan limit ~9 IDs/call) — pass 1 samples every 10th strike to find ATM area, pass 2 fetches ATM±8 precisely. Spot derived from nearest SENSEX FUTIDX LTP (background thread every 3s)
- Feed auto-restarts after page refresh (60s stale check) and after underlying switch (force flag)

### Spread Quick Bar

One-click spread entry at the bottom of the option chain panel.

**Workflow:** Click **[S]** on sell leg → Click **[B]** on buy leg → Enter SL → Click Execute

**Inputs:**
- **Sell price** — pre-fills from LTP, live-updates on every tick. Dirty flag prevents overwrite after manual edit
- **SL** — mandatory (gold border). Must be > sell price. Enables execute buttons
- **Buy price** — pre-fills from buy leg LTP, live-updates on ticks
- **Max Loss ₹** — auto-calculates lot quantity: `lots = floor(maxLoss / ((sl - sellPrice) × lotSize))`
- **Override qty** — manual quantity override

**Execute buttons:**
| Button | Behaviour |
|--------|-----------|
| ⚡ EXECUTE LMT | BUY hedge @ MARKET → 200ms → SELL @ typed LIMIT price |
| ⚡ MKT | BUY hedge @ MARKET → 200ms → SELL @ MARKET |
| ARM TRIGGER | Queue spread — fires when sell leg LTP ≤ trigger price |
| SELL MKT | Emergency single-leg SELL (hedge filled, sell rejected) |
| BUY MKT | Emergency single-leg BUY (to close naked short) |

**Order sequence (critical):**
1. BUY hedge at MARKET (reduces margin requirement)
2. 200ms pause
3. SELL at LIMIT or MARKET
4. 2.5s poll to confirm no RMS rejection
5. Only if confirmed filled: place exchange-level `STOP_LOSS_MARKET` BUY order at Dhan + register monitored SL

**product_type:** Always `"MARGIN"` (NRML). Never INTRADAY/MIS.

### Exchange SL Order Management

After a spread fills, a `STOP_LOSS_MARKET` BUY order sits live at Dhan as hard protection (fires even if VPS crashes). The platform tracks its order ID and manages it through the position lifecycle:

- **Full manual exit** → exchange SL cancelled before exit order is placed (prevents spurious re-entry)
- **Partial exit** → old SL cancelled, new `STOP_LOSS_MARKET` placed for remaining quantity at same trigger price
- SL order ID stored in `TradeManager._sl_tp_orders[security_id].exchange_sl_order_id`
- API: `POST /api/order/cancel_sl/<security_id>`, `POST /api/order/replace_sl/<security_id>`

### Snapshot Chart (DOM Panel)

- TradingView Lightweight Charts v4.1.3 (CDN: `cdn.jsdelivr.net`)
- 1-minute OHLCV candlesticks via Dhan `intraday_minute_data`
- Shows **today + previous trading day** candles
- **Live bar** updates driven by WebSocket tick every tick
- Auto-loads and switches to Chart tab when sell leg selected via [S]
- `[Depth]` / `[Chart]` tab toggle in DOM panel header
- **Timezone note:** Dhan returns IST strings. Parsed naively with `.timestamp()` — correct IST display without adding timezone to timeScale (not supported in v4)

### Order Book & Positions

- All open positions shown regardless of where they were entered
- Per-position: **EXIT MKT**, inline **EXIT...** form with 25%/50%/75%/100% qty presets (rounded to nearest lot)
- Global **EXIT ALL** button
- Order book with real-time status updates via OrderUpdate WebSocket

### WebSocket Integration

**DepthWebSocket** (`dhan_api.py`) — single connection to `wss://depth-api-feed.dhan.co/twentydepth`:
- Handles both **20-level DOM depth** (for the selected instrument) and **multi-instrument LTP ticks** (for option chain ATM±10 + open positions)
- Merged into one connection to avoid Dhan error 805 (max concurrent WebSocket limit)
- Auto-reconnects every 5s
- Binary packet parser: HEADER_SIZE=50 bytes, parses feed code, security ID, price

**OrderUpdate WebSocket** (`dhan_api.py`):
- Fires instantly on order fill/rejection
- `monitor._on_order_update()` emits `order_update` SocketIO event
- Dashboard: filled → success toast, rejected → error toast with reason

### Risk Management (Prop-Firm Style)

- **Daily Max Loss** — locks out when total P&L hits limit
- **Profit Target** — optional lockout when target reached
- **Profit Lock** — locks a % floor once P&L crosses threshold (e.g. made ₹10k → lock ₹5k as floor)
- **Trailing Drawdown** — tracks HWM of realized P&L, locks out if drawdown exceeds %
- **Cooldown Timer** — enforced pause after losses or consecutive losing trades
- **Max Open Positions / Max Order Quantity / Max Single Trade Risk**

**Lockout sequence:** cancel all pending orders → close all positions at market → activate Dhan Kill Switch → encrypt and lock state (cannot be manually overridden)

### Trade Journal

Two-part system sharing one SQLite DB (`trade_journal.db`):

**Analytics sidebar** (dashboard):
- `trades`, `daily_summary`, `pnl_snapshots` tables
- Today / History / Analytics tabs
- APIs: `GET /api/journal/trades`, `/api/journal/daily_summaries`, `/api/journal/analytics`

**Detailed entry journal** (`/journal`):
- `trade_entries` table — one row per spread/naked trade with entry+exit prices, screenshots, notes
- **Entry screenshot:** captured at EXECUTE moment — fetches Nifty 1m candles, renders 150 candles to offscreen 900×200 canvas with gold ENTRY price line, POSTs PNG to server
- **Exit screenshot:** captured on SL/TP hit (`sl_tp_triggered` SocketIO) or manual exit (`_placeExitOrder` success), with blue EXIT line
- **Session persistence:** `_journalOpenEntries` JS map links `sell_security_id → entry_id`. On browser refresh, `GET /api/journal/open_entry/<security_id>` recovers the entry_id from SQLite so exit screenshots still attach
- Journal page auto-refreshes every 30s; filter by All/Open/Winners/Losers; expandable cards with screenshots + notes

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

# Logs
tail -f /root/Risk-Management/platform.log

# Status
sudo systemctl status risk-manager
```

> **Note:** Dhan rate-limits token generation to once per 2 minutes. If the service restart-loops, stop it, wait 130s, then start.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Full risk status, positions, P&L |
| `/api/positions` | GET | Open positions with SL/TP |
| `/api/order/place` | POST | Place single order |
| `/api/order/place_spread` | POST | Execute or queue a spread |
| `/api/order/cancel_sl/<sid>` | POST | Cancel exchange SL order before full exit |
| `/api/order/replace_sl/<sid>` | POST | Replace exchange SL with new quantity after partial exit |
| `/api/option_chain` | GET | Fetch option chain data |
| `/api/option_chain/subscribe_ltp` | POST | Subscribe instruments to LTP WebSocket feed |
| `/api/chart/<security_id>` | GET | Intraday 1m OHLC candles (today + prev day) |
| `/api/chart/nifty` | GET | Nifty index 1m candles (for journal screenshots) |
| `/api/journal/entry` | POST | Create journal entry |
| `/api/journal/entry/<id>` | PUT | Update exit price / notes |
| `/api/journal/entries` | GET | List journal entries |
| `/api/journal/open_entry/<sid>` | GET | Find open entry by sell security_id (refresh recovery) |
| `/api/journal/screenshot` | POST | Upload entry/exit screenshot PNG |
| `/api/sl` | POST | Set stop loss on position |
| `/api/tp` | POST | Set take profit on position |
| `/api/exit_all` | POST | Emergency close all positions |
