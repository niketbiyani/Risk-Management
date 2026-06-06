# Claude Code Context — Risk Management Platform

This file exists so a new Claude session can pick up exactly where the last one left off.

---

## What This Project Is

A single-VPS trade management platform for **Nifty/SENSEX options scalping** on 15-second charts via Dhan broker. The user trades exclusively **two-legged credit spreads** (bear call, bull put).

**Primary file:** `dashboard.py` — ~4700 lines, single file containing Flask backend + all inline HTML/CSS/JS.

**Other key files:**
- `dhan_api.py` — Dhan REST API wrapper + WebSocket async wrappers
- `monitor.py` — background position monitor, executes spreads, manages SL/TP, WebSocket callbacks
- `trade_manager.py` — spread detection, SL/TP logic, pending spread queue, position cache
- `instrument_cache.py` — local SQLite cache of all Dhan instruments
- `config.py` — reads `.env` settings
- `main.py` — entry point

**Branch for all work:** `claude/laughing-fermi-9HTg6`

**Deploy:** VPS runs `git pull origin claude/laughing-fermi-9HTg6` then `sudo systemctl restart risk-manager`

**Logs:** `tail -f /root/Risk-Management/platform.log`

---

## Current Feature State

### Option Chain
- Supports **NIFTY** (NSE_FNO, IDX_I) and **SENSEX** (BSE, BSE_FNO) — BANKNIFTY was removed
- REST poll every **2 seconds** (server-side 2s cache to prevent Dhan rate-limit) — for OI/IV and full chain data
- **Real-time LTP for ATM±10 strikes** via MarketFeed WebSocket (<50ms) — updates cells in-place without re-rendering table
- SENSEX: uses two-pass LTP fetch (Dhan `ticker_data` limit ~9 IDs per call, 191 strikes total)
  - Pass 1: sample every 10th strike (coarse, finds ATM area)
  - Pass 2: fetch ATM±8 strikes precisely
  - Synthetic spot via put-call parity (Dhan has no BSE index LTP endpoint)
- **Known issue:** Some SENSEX ATM strikes return 0 from `ticker_data` (Dhan API inconsistency). User types price manually in quick bar.

### Spread Quick Bar (bottom of option chain panel)
Each chain row has **S** and **B** pill buttons. Clicking them populates the quick bar:

**Inputs:**
- `sqb-sell-price` — editable, pre-fills from LTP and updates on every WebSocket tick. Dirty flag `_sqbSellPriceDirty` prevents tick updates after manual entry. Resets on new leg selection.
- `sqb-sell-sl` — mandatory SL (gold border). Must be > sell price to enable execute.
- `sqb-buy-price` — editable, pre-fills from buy leg LTP, also updates on ticks. Dirty flag `_sqbBuyPriceDirty`.

**Auto-qty:** `lots = floor(maxLoss / ((sl - sellPrice) × lotSize))`

**Buttons:**
- **⚡ EXECUTE LMT** — BUY hedge at MARKET first (200ms pause), then SELL at typed LIMIT price
- **⚡ MKT** — BUY hedge at MARKET, then SELL at MARKET (enabled with just SL + qty)
- **ARM TRIGGER** — queue spread to fire when sell leg LTP ≤ trigger price
- **SELL MKT** — single-leg sell only (emergency: hedge filled, sell rejected)
- **BUY MKT** — single-leg buy only

**Order sequence:** BUY hedge always goes first (reduces margin requirement), 200ms pause, then SELL. After fill: exchange-level `STOP_LOSS_MARKET` BUY order placed at Dhan (fires even if VPS crashes) + monitored SL as backup. Backend: `monitor.py _execute_spread()`.

**product_type:** Always `"MARGIN"` (NRML). Never change to INTRADAY/MIS — user explicitly requires this.

### Snapshot Chart (DOM panel)
- TradingView Lightweight Charts v4.1.3 from `cdn.jsdelivr.net`
- 1-minute OHLCV candles via Dhan `intraday_minute_data`
- Shows **full trading day** + previous trading day candles
- **Timezone:** Dhan returns IST strings ("09:15:00"). Server is UTC. Fix: parse naively with `.timestamp()` — chart displays correct IST times (v4 has no timezone support, do not add `timezone` to timeScale).
- **Live bar updates:** now driven by WebSocket tick from `oc_ltp` SocketIO event — updates on every tick, not just every 2s
- Auto-loads and switches to chart tab when user selects a sell leg via [S] button
- `[Depth]` / `[Chart]` tab toggle in DOM panel header

### Position Table
- Shows all open positions from Dhan (regardless of where they were opened)
- Per-position **EXIT MKT** button and **EXIT...** inline form with 25%/50%/75%/100% qty buttons (round to nearest lot)
- Global **EXIT ALL** button

### WebSocket Integration (Phase 1 + 2 — complete)

**MarketFeed (LTP ticks):**
- `dhan_api.start_market_feed_async(instruments, callback)` — daemon thread, auto-reconnects every 5s
- `monitor._start_market_feed()` — called on startup, subscribes open position instruments
- `monitor._refresh_market_feed(positions)` — called each poll tick, restarts feed if positions changed
- `monitor._on_market_tick(tick_data)` — fast callback: emits `oc_ltp` for chain display, checks SL/TP triggers
- `monitor._sl_tp_executing` set prevents double-fire
- `/api/option_chain/subscribe_ltp` — endpoint called by browser after chain renders; merges ATM±10 OC instruments with position instruments and (re)starts feed

**OrderUpdate (order status):**
- `dhan_api.start_order_updates_async(callback)` — daemon thread, auto-reconnects every 5s
- `monitor._on_order_update(update)` — emits `order_update` and `SPREAD_FAILED` SocketIO events instantly
- Dashboard JS listens: filled → toast, rejected → error toast

**SL/TP latency:** <50ms (was 2s REST poll)
**Order status latency:** <200ms (was 2.5s sleep + poll)

**Tick field names (verify on first run with open positions):**
Log `RAW TICK: %s` in `_on_market_tick` — try `security_id`/`securityId`/`Security Id` and `LTP`/`ltp`/`last_price`. Remove debug log once confirmed.

### Order Rejection Detection
- **WebSocket path (primary):** `_on_order_update` fires instantly when Dhan pushes REJECTED status
- **REST fallback (still in place):** After placing spread, polls order book 2.5s later — to be removed once WebSocket path confirmed working
- Old SL/TP REST poll in `_tick()` also still present as fallback

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

### Python triple-quoted strings with JS
- Onclick handlers with string args need `\\'` in Python to produce `\'` in JS output.
- Example: `onclick="spreadSelectLeg(\\'sell\\', ...)"`

### dashboard.py structure
- All HTML is a Python triple-quoted string (the Flask route returns it)
- CSS is inline in a `<style>` block
- JS is inline in `<script>` blocks
- Line numbers shift as you edit — always grep for function names rather than relying on line numbers

### Service management
- Logs go to `/root/Risk-Management/platform.log` (not journalctl)
- Dhan rate-limits token generation to once per 2 minutes — if service restart-loops, `stop` it, `sleep 130`, then `start`
- Clear log before restart to avoid duplicate lines: `> /root/Risk-Management/platform.log`

---

## Pending / Next Work

- **Confirm WebSocket tick field names** — check `RAW TICK:` in platform.log with open positions, lock down field names, remove debug log
- **Remove REST fallbacks** once WebSocket confirmed working:
  - `check_sl_tp_triggers` call in `_tick()` (monitor.py)
  - 2.5s sleep + order poll block in `_execute_spread()` (monitor.py)
- **SENSEX ATM price fallback** — when `ticker_data` returns 0 for ATM strikes, try `intraday_minute_data` last close
- **Hotkeys** — discussed but not started
- **Spot price real-time** — index spot still updates every 2s via REST (low priority)

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
```

Dashboard: `http://YOUR_VPS_IP:5555`

---

## How to Resume With a New Claude Session

1. Open this repo in Claude Code (web or CLI)
2. Say: *"Read CLAUDE.md and continue development on the risk management platform"*
3. Claude will read this file and have full context to continue

The active branch is `claude/laughing-fermi-9HTg6`. All work goes there.
