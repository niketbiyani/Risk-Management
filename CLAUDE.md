# Claude Code Context — Risk Management Platform

This file exists so a new Claude session can pick up exactly where the last one left off.

---

## What This Project Is

A single-VPS trade management platform for **Nifty/SENSEX options scalping** on 15-second charts via Dhan broker. The user trades exclusively **two-legged credit spreads** (bear call, bull put) and occasional naked shorts.

**Primary file:** `dashboard.py` — ~5400 lines, single file containing Flask backend + all inline HTML/CSS/JS.

**Key files:**
| File | Purpose |
|---|---|
| `dashboard.py` | Flask app, all HTML/CSS/JS inline, all API routes |
| `dhan_api.py` | Dhan REST API wrapper + MarketFeed/OrderUpdate WebSocket async wrappers |
| `monitor.py` | Background position monitor, executes spreads, manages SL/TP, WebSocket callbacks |
| `trade_manager.py` | Spread detection, SL/TP logic, pending spread queue, position cache |
| `trade_journal.py` | SQLite-backed trade journal — analytics + detailed entries with screenshots |
| `instrument_cache.py` | Local SQLite cache of all Dhan instruments |
| `state_manager.py` | Encrypted daily state, risk rules, trade history in memory |
| `risk_engine.py` | Risk enforcement — max loss, max trades, lockout logic |
| `config.py` | Reads `.env` settings |
| `main.py` | Entry point |

**Branch for all work:** `claude/laughing-fermi-9HTg6`

**Deploy:** VPS runs `git pull origin claude/laughing-fermi-9HTg6` then `sudo systemctl restart risk-manager`

**Logs:** `tail -f /root/Risk-Management/platform.log`

---

## Current Feature State

### Option Chain
- Supports **NIFTY** (NSE_FNO, IDX_I) and **SENSEX** (BSE, BSE_FNO) — BANKNIFTY was removed
- REST poll every **2 seconds** (server-side 2s cache to prevent Dhan rate-limit) — for OI/IV and full chain data
- **Real-time LTP for ATM±10 strikes** via MarketFeed WebSocket (<50ms) — updates cells in-place without re-rendering table
- SENSEX: two-pass `ticker_data` fetch (limit ~9 IDs per call, 191 strikes total)
  - Pass 1: sample every 10th strike (coarse, finds ATM area)
  - Pass 2: fetch ATM±8 strikes precisely
  - Synthetic spot via put-call parity (Dhan has no BSE index LTP endpoint)
- **Known issue:** Some SENSEX ATM strikes return 0 from `ticker_data` (Dhan API inconsistency). User types price manually in quick bar.

### Spread Quick Bar (bottom of option chain panel)
Each chain row has **S** and **B** pill buttons. Clicking them populates the quick bar:

**Inputs:**
- `sqb-sell-price` — editable, pre-fills from LTP, updates on every WebSocket tick. Dirty flag `_sqbSellPriceDirty` prevents tick overwrite after manual edit. Resets on new leg selection.
- `sqb-sell-sl` — mandatory SL (gold border). Must be > sell price to enable execute.
- `sqb-buy-price` — editable, pre-fills from buy leg LTP, updates on ticks. Dirty flag `_sqbBuyPriceDirty`.

**Auto-qty:** `lots = floor(maxLoss / ((sl - sellPrice) × lotSize))`

**Buttons:**
- **⚡ EXECUTE LMT** — BUY hedge at MARKET first (200ms pause), then SELL at typed LIMIT price
- **⚡ MKT** — BUY hedge at MARKET, then SELL at MARKET (enabled with just SL + qty)
- **ARM TRIGGER** — queue spread to fire when sell leg LTP ≤ trigger price
- **SELL MKT** — single-leg sell only (emergency: hedge filled, sell rejected)
- **BUY MKT** — single-leg buy only

**Order sequence:** BUY hedge always goes first (reduces margin requirement), 200ms pause, then SELL. After fill: exchange-level `STOP_LOSS_MARKET` BUY order placed at Dhan (fires even if VPS crashes) + monitored SL as backup. Backend: `monitor.py _execute_spread()`.

**product_type:** Always `"MARGIN"` (NRML). **Never change to INTRADAY/MIS** — user explicitly requires this.

### Snapshot Chart (DOM panel)
- TradingView Lightweight Charts v4.1.3 from `cdn.jsdelivr.net`
- 1-minute OHLCV candles via Dhan `intraday_minute_data`
- Shows **full trading day** + previous trading day candles
- **Timezone:** Dhan returns IST strings ("09:15:00"). Server is UTC. Fix: parse naively with `.timestamp()` — chart displays correct IST times. Do not add `timezone` to timeScale (v4 doesn't support it).
- **Live bar updates:** driven by WebSocket tick from `oc_ltp` SocketIO event — updates on every tick
- Auto-loads and switches to Chart tab when user selects a sell leg via [S] button
- `[Depth]` / `[Chart]` tab toggle in DOM panel header

### Position Table
- Shows all open positions from Dhan (regardless of where they were opened)
- Per-position **EXIT MKT** button and **EXIT...** inline form with 25%/50%/75%/100% qty buttons (round to nearest lot)
- Global **EXIT ALL** button
- All exit paths go through `_placeExitOrder()` in JS

### WebSocket Integration (complete)

**MarketFeed (LTP ticks):**
- `dhan_api.start_market_feed_async(instruments, callback)` — daemon thread, auto-reconnects every 5s
- `monitor._start_market_feed()` — called on startup
- `monitor._refresh_market_feed(positions)` — called each poll tick, restarts feed if positions changed
- `monitor._on_market_tick(tick_data)` — emits `oc_ltp` SocketIO for chain display, checks SL/TP triggers
- `monitor._sl_tp_executing` set prevents double-fire
- `/api/option_chain/subscribe_ltp` — merges ATM±10 OC instruments with position instruments, (re)starts feed

**OrderUpdate (order status):**
- `dhan_api.start_order_updates_async(callback)` — daemon thread, auto-reconnects every 5s
- `monitor._on_order_update(update)` — emits `order_update` and `SPREAD_FAILED` SocketIO events instantly
- Dashboard JS: filled → success toast, rejected → error toast

**SL/TP latency:** <50ms | **Order status latency:** <200ms

**Tick field names (still to confirm):**
`RAW TICK: %s` debug log is in `_on_market_tick` — check `platform.log` with open positions to confirm field names (`security_id`/`securityId`/`Security Id` and `LTP`/`ltp`/`last_price`), then remove the log line.

### Order Rejection Detection
- **WebSocket path (primary):** `_on_order_update` fires instantly on REJECTED status
- **REST fallback (still in place):** polls order book 2.5s after placing spread — remove once WebSocket confirmed

### Trade Journal
Two-part system sharing one SQLite DB (`trade_journal.db`):

**Part 1 — Analytics (existing, in dashboard sidebar):**
- `trade_journal.py` `TradeJournal` class — `trades`, `daily_summary`, `pnl_snapshots` tables
- Populated by `risk_engine.py` → `state_manager.record_trade()` when trades complete
- Dashboard sidebar shows Today / History / Analytics tabs
- API: `GET /api/journal/trades`, `/api/journal/daily_summaries`, `/api/journal/analytics`

**Part 2 — Detailed entries with screenshots (new, at `/journal`):**
- `trade_entries` table in same SQLite DB — one row per spread/naked trade, tracks entry+exit prices, screenshots, notes
- Screenshots stored as PNG files in `journal_screenshots/` directory
- **Entry screenshot:** captured in browser JS at moment of EXECUTE — fetches Nifty 1m candles (`/api/chart/nifty`), renders 150 candles to 900×200 offscreen canvas with gold ENTRY price line, POSTs PNG to server
- **Exit screenshot:** captured same way with blue EXIT line — triggered by:
  - `sl_tp_triggered` SocketIO event (SL/TP hit)
  - `_placeExitOrder()` success (EXIT MKT / EXIT LMT / partial exit buttons)
- `_journalOpenEntries` JS map: `sell_security_id → entry_id` — links entry to exit within same browser session
- **Spread detection:** both `_spreadSellLeg` + `_spreadBuyLeg` set → spread; only sell leg (SELL MKT) → naked
- Journal page at `http://VPS:5555/journal` (opens in new tab via 📓 Journal button in dashboard header)
- Auto-refreshes every 30s; filter by All/Open/Winners/Losers; expandable cards with screenshots + notes

**API endpoints:**
- `POST /api/journal/entry` — create entry
- `PUT /api/journal/entry/<id>` — update exit or notes (`action: 'exit'` or `action: 'notes'`)
- `GET /api/journal/entries` — list entries
- `POST /api/journal/screenshot` — receive base64 PNG, save to file, return filename
- `GET /api/journal/screenshots/<filename>` — serve PNG file
- `GET /api/chart/nifty` — today's Nifty index 1m candles (security_id=13, NSE_EQ, instrument_type=INDEX)

### Panels Hidden (code kept, just display:none)
- Naked Order entry panel — superseded by quick bar single-leg buttons
- Spread Entry tab — superseded by quick bar

---

## Key Technical Gotchas

### Dhan API quirks
- `ticker_data` for BSE_FNO requires **integer** security IDs, not strings. `{'BSE_FNO': [123]}` works, `{'BSE_FNO': ['123']}` fails silently.
- `ticker_data` limit: **~9 IDs per call**. More than ~10 returns empty/failure.
- `option_chain` API only works for NSE indices (IDX_I). BSE not supported — use two-pass `ticker_data` instead.
- No Dhan REST endpoint for BSE index (SENSEX) spot price — derive via put-call parity.
- `intraday_minute_data` requires `from_date` and `to_date` params (today's date).
- MarketFeed exchange segment ints: NSE_FNO=1, BSE_FNO=2, NSE_EQ=3, BSE_EQ=4 (verify against SDK).
- NIFTY index for chart data: security_id=`13`, exchange_segment=`NSE_EQ`, instrument_type=`INDEX`.

### Python triple-quoted strings with JS
- Onclick handlers with string args need `\\'` in Python to produce `\'` in JS output.
- Example: `onclick="spreadSelectLeg(\\'sell\\', ...)"`
- The `/journal` route returns a plain string (not an f-string) since it has no Python variables to interpolate — avoids `{{` / `}}` escaping issues.

### dashboard.py structure
- All HTML is a Python triple-quoted string (the Flask route returns it)
- CSS is inline in a `<style>` block
- JS is inline in `<script>` blocks
- Line numbers shift as you edit — always grep for function names rather than relying on line numbers
- `_build_journal_page()` returns a plain string for the `/journal` route

### trade_journal.py structure
- Single `TradeJournal` class, single SQLite connection with `threading.Lock()`
- `_create_tables()` creates all 4 tables on init (safe to add new tables here — uses `CREATE TABLE IF NOT EXISTS`)
- `SCREENSHOTS_DIR` = `<project_root>/journal_screenshots/` — created on import
- `save_screenshot(data_url)` extracts base64, saves as `<uuid>.png`, returns filename

### Service management
- Logs go to `/root/Risk-Management/platform.log` (not journalctl)
- Dhan rate-limits token generation to once per 2 minutes — if service restart-loops, `stop` it, `sleep 130`, then `start`
- Clear log before restart: `> /root/Risk-Management/platform.log`

---

## Pending / Next Work

- **Confirm WebSocket tick field names** — check `RAW TICK:` in platform.log with open positions, then remove the debug log line from `monitor._on_market_tick()`
- **Remove REST fallbacks** once WebSocket confirmed:
  - `check_sl_tp_triggers` call in `_tick()` (monitor.py)
  - 2.5s sleep + order poll block in `_execute_spread()` (monitor.py)
- **Journal: naked short entry** — SELL MKT button path not yet wired to `createJournalEntry()` (only spread EXECUTE is wired). Low priority.
- **Journal: session persistence** — `_journalOpenEntries` is a JS in-memory map; refreshing the browser loses the entry_id→security_id link, so exit screenshots won't attach. Fix: store open entry_ids in localStorage or add a server-side lookup endpoint.
- **SENSEX ATM price fallback** — when `ticker_data` returns 0 for ATM strikes, try `intraday_minute_data` last close
- **Hotkeys** — discussed but not started
- **Spot price real-time** — index spot still updates every 2s via REST (low priority)

---

## Research / Experiments (not in platform)

- `generate_pe_demo.py` — generates `demo_pe_rejections.html`: ATM PE 1m candle chart with rejection detection markers, 20 EMA (blue), 50 EMA (gold), RSI(14), MACD(12,26,9). Serve with `python3 -m http.server 8447`. Accuracy was 59% at 10-candle horizon — not reliable enough to trade on alone.
- `analyse_rejections.py` — rejection accuracy on Nifty futures (31% — abandoned)
- `analyse_rejections_pe.py` — rejection accuracy on ATM PE (59% at 10 candles)
- `journal_demo.html` — standalone demo of the journal UI layout (superceded by live `/journal` route)

---

## VPS Workflow

```bash
# Pull latest
git pull origin claude/laughing-fermi-9HTg6

# Restart service (clean)
sudo systemctl stop risk-manager && > /root/Risk-Management/platform.log && sudo systemctl start risk-manager

# Watch logs
tail -f /root/Risk-Management/platform.log

# Check if running
sudo systemctl status risk-manager

# Serve demo files (separate process)
nohup python3 -m http.server 8447 --directory /root/Risk-Management > /dev/null 2>&1 &
```

Dashboard: `http://YOUR_VPS_IP:5555`
Journal: `http://YOUR_VPS_IP:5555/journal`

---

## How to Resume With a New Claude Session

1. Open this repo in Claude Code (web or CLI)
2. Say: *"Read CLAUDE.md and continue development on the risk management platform"*
3. Claude will read this file and have full context to continue

The active branch is `claude/laughing-fermi-9HTg6`. All work goes there.
