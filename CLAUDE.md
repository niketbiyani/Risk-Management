# Claude Code Context — Risk Management Platform

This file exists so a new Claude session can pick up exactly where the last one left off.

---

## What This Project Is

A single-VPS trade management platform for **Nifty/SENSEX options scalping** on 15-second charts via Dhan broker. The user trades exclusively **two-legged credit spreads** (bear call, bull put) and occasional naked shorts.

**Primary file:** `dashboard.py` — ~5600 lines, single file containing Flask backend + all inline HTML/CSS/JS.

**Key files:**
| File | Purpose |
|---|---|
| `dashboard.py` | Flask app, all HTML/CSS/JS inline, all API routes |
| `dhan_api.py` | Dhan REST API wrapper + DepthWebSocket (LTP + DOM) + OrderUpdate WebSocket |
| `monitor.py` | Background position monitor, executes spreads, manages SL/TP, WebSocket callbacks |
| `trade_manager.py` | Spread detection, SL/TP logic, exchange SL order tracking, pending spread queue, position cache |
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
- **Real-time LTP for ATM±10 strikes** via DepthWebSocket (<50ms) — updates cells in-place without re-rendering table
- ATM hysteresis: `abs(spot - prev_atm_strike) > strike_interval * 0.6` — prevents flickering at strike boundaries
- `_ocRenderedKey` JS variable tracks chain structure (expiry + strikes); determines in-place update vs full DOM rebuild
- SENSEX: two-pass `ticker_data` fetch (limit ~9 IDs per call, 191 strikes total)
  - Pass 1: sample every 10th strike (coarse, finds ATM area)
  - Pass 2: fetch ATM±8 strikes precisely
  - Spot price from background thread `_start_bse_spot_updater` — fetches nearest SENSEX FUTIDX LTP every 3s via REST, stored in `_bse_last_spot`. Put-call parity is calculated but does NOT overwrite `_bse_last_spot` (known issue: BSE ticker_data returns 0 for ATM strikes)
- Feed restart logic:
  - `_oc_last_feed_start` — if >60s since last feed start, restart on any subscribe call (handles page refresh)
  - `force: true` flag sent by JS when underlying changes (NIFTY↔SENSEX)
  - `_oc_ltp_subscribed` set only grows (never-shrink subscription keeps strikes warm)
- **Known issue:** Some SENSEX ATM strikes return 0 from `ticker_data` — user types price manually in quick bar

### Spread Quick Bar (bottom of option chain panel)

Each chain row has **S** and **B** pill buttons. Clicking them populates the quick bar.

**Inputs:**
- `sqb-sell-price` — editable, pre-fills from LTP, updates on every WebSocket tick. Dirty flag `_sqbSellPriceDirty` prevents tick overwrite after manual edit. Resets on new leg selection
- `sqb-sell-sl` — mandatory SL (gold border). Must be > sell price to enable execute
- `sqb-buy-price` — editable, pre-fills from buy leg LTP, updates on ticks. Dirty flag `_sqbBuyPriceDirty`
- **Max Loss ₹** — auto-calculates: `lots = floor(maxLoss / ((sl - sellPrice) × lotSize))`
- **Override qty** — manual quantity override

**Buttons:**
- **⚡ EXECUTE LMT** — BUY hedge at MARKET first (200ms pause), then SELL at typed LIMIT price
- **⚡ MKT** — BUY hedge at MARKET, then SELL at MARKET (enabled with just SL + qty)
- **ARM TRIGGER** — queue spread to fire when sell leg LTP ≤ trigger price
- **SELL MKT** — single-leg sell only (emergency: hedge filled, sell rejected)
- **BUY MKT** — single-leg buy only

**Order sequence:** BUY hedge always first (reduces margin), 200ms pause, then SELL. After the 2.5s rejection poll confirms fill: exchange-level `STOP_LOSS_MARKET` BUY placed at Dhan + monitored SL registered as backup.

**product_type:** Always `"MARGIN"` (NRML). **Never change to INTRADAY/MIS** — user explicitly requires this.

### Exchange SL Order Lifecycle

After a spread fills, a `STOP_LOSS_MARKET` BUY sits live at Dhan as hard protection. The platform tracks it through the position lifecycle:

- **On fill:** `sl_order_id` stored in `TradeManager._sl_tp_orders[security_id].exchange_sl_order_id`
- **Full manual exit:** `POST /api/order/cancel_sl/<sid>` cancels the SL before placing exit order (prevents spurious re-entry if price later hits trigger)
- **Partial exit:** `POST /api/order/replace_sl/<sid>` cancels old SL + places new `STOP_LOSS_MARKET` for remaining quantity at same trigger price. Trigger price fetched from server (`_sl_tp_orders[sid].stop_loss_price`), no need to send from client
- **SL hit naturally (SL/TP path):** position closes via monitor, no cancel needed

**JS flow:** `_placeExitOrder(sid, ..., fullQty)` → if `qty >= fullQty`: cancel_sl first, then exit. If `qty < fullQty`: exit, then replace_sl with `remaining_qty = fullQty - qty`.

### Snapshot Chart (DOM Panel)

- TradingView Lightweight Charts v4.1.3 from `cdn.jsdelivr.net`
- 1-minute OHLCV candles via Dhan `intraday_minute_data`
- Shows **today + previous trading day** candles (two API calls on load)
- **Timezone:** Dhan returns IST strings ("09:15:00"). Server is UTC. Fix: parse naively with `.timestamp()` — chart displays correct IST times. Do NOT add `timezone` to timeScale (v4 doesn't support it)
- **Live bar updates:** driven by WebSocket tick from `oc_ltp` SocketIO event — updates OHLC on every tick
- Auto-loads and switches to Chart tab when user selects sell leg via [S] button
- `[Depth]` / `[Chart]` tab toggle in DOM panel header
- `_lwCurrentSecurity` tracks which instrument the chart is showing
- 60s auto-refresh interval to pick up completed candles

### Position Table

- Shows all open positions from Dhan (regardless of where opened)
- Per-position **EXIT MKT** and **EXIT...** inline form with 25%/50%/75%/100% qty buttons (rounded to nearest lot)
- Global **EXIT ALL** button
- All exit paths go through `_placeExitOrder(sid, exSeg, prodType, qty, direction, orderType, price, fullQty)`
- `fullQty` determines whether to cancel_sl (full) or replace_sl (partial)

### WebSocket Integration

**DepthWebSocket** (`dhan_api.py`) — single connection to `wss://depth-api-feed.dhan.co/twentydepth`:
- Merged LTP feed + DOM feed into one connection to avoid Dhan error 805 (max concurrent WS exceeded)
- `set_ltp_instruments(instruments, callback)` — subscribes additional instruments for LTP ticks without stopping/restarting DOM subscription
- `_send_subscribe(ws)` — builds RequestCode 23 payload with depth instrument + LTP instruments combined
- Binary parser: HEADER_SIZE=50 bytes, FEED_BID=4. Routes packets by `security_id`: if in `_ltp_sids` → call `_ltp_callback`, if matches `_security_id` → parse full 20-level depth
- Auto-reconnects every 5s

**OrderUpdate WebSocket** (`dhan_api.py`):
- `start_order_updates_async(callback)` — daemon thread, auto-reconnects every 5s
- `monitor._on_order_update(update)` — emits `order_update` and `SPREAD_FAILED` SocketIO events instantly
- Dashboard JS: filled → success toast, rejected → error toast

**SL/TP latency:** <50ms | **Order status latency:** <200ms

**Tick field names (still to confirm):**
`RAW TICK: %s` debug log is in `_on_market_tick` — check `platform.log` with open positions to confirm field names (`security_id`/`securityId`/`Security Id` and `LTP`/`ltp`/`last_price`), then remove the log line.

### Order Rejection Detection

- **WebSocket path (primary):** `_on_order_update` fires instantly on REJECTED status
- **REST fallback (still in place):** 2.5s sleep + order poll block in `_execute_spread()` — this is also used to gate SL placement (SL only placed after confirmation). Remove the status-checking part once WebSocket confirmed reliable; keep the SL gating

### Trade Journal

Two-part system sharing one SQLite DB (`trade_journal.db`):

**Part 1 — Analytics (dashboard sidebar):**
- `trade_journal.py` `TradeJournal` class — `trades`, `daily_summary`, `pnl_snapshots` tables
- Populated by `risk_engine.py` → `state_manager.record_trade()` when trades complete
- Dashboard sidebar shows Today / History / Analytics tabs
- API: `GET /api/journal/trades`, `/api/journal/daily_summaries`, `/api/journal/analytics`

**Part 2 — Detailed entries with screenshots (`/journal`):**
- `trade_entries` table — one row per spread/naked trade, tracks entry+exit prices, screenshots, notes
- Screenshots stored as PNG files in `journal_screenshots/` directory
- **Entry screenshot:** captured in browser JS at moment of EXECUTE — fetches Nifty 1m candles (`/api/chart/nifty`), renders 150 candles to 900×200 offscreen canvas with gold ENTRY price line, POSTs PNG to server
- **Exit screenshot:** captured same way with blue EXIT line — triggered by:
  - `sl_tp_triggered` SocketIO event (SL/TP hit)
  - `_placeExitOrder()` success (EXIT MKT / EXIT LMT / partial exit buttons)
- `_journalOpenEntries` JS map: `sell_security_id → entry_id` — links entry to exit within browser session
- **Refresh recovery:** `closeJournalEntry()` calls `GET /api/journal/open_entry/<security_id>` when entry_id not in local map — queries most recent open `trade_entries` row for that sell leg. Exit screenshots attach correctly even after browser refresh mid-trade
- **Spread detection:** both `_spreadSellLeg` + `_spreadBuyLeg` set → spread; only sell leg (SELL MKT) → naked
- Journal page at `http://VPS:5555/journal` (opens in new tab via 📓 Journal button)
- Auto-refreshes every 30s; filter by All/Open/Winners/Losers; expandable cards with screenshots + notes

**API endpoints:**
- `POST /api/journal/entry` — create entry
- `PUT /api/journal/entry/<id>` — update exit or notes (`action: 'exit'` or `action: 'notes'`)
- `GET /api/journal/entries` — list entries
- `GET /api/journal/open_entry/<security_id>` — find most recent open entry by sell leg security_id
- `POST /api/journal/screenshot` — receive base64 PNG, save to file, return filename
- `GET /api/journal/screenshots/<filename>` — serve PNG file
- `GET /api/chart/nifty` — today's Nifty index 1m candles (security_id=13, NSE_EQ, instrument_type=INDEX)

### Panels Hidden (code kept, just display:none)

- Naked Order entry panel — superseded by quick bar single-leg buttons
- Spread Entry tab — superseded by quick bar

---

## Key Technical Gotchas

### Dhan API Quirks

- `ticker_data` for BSE_FNO requires **integer** security IDs, not strings. `{'BSE_FNO': [123]}` works, `{'BSE_FNO': ['123']}` fails silently
- `ticker_data` limit: **~9 IDs per call**. More than ~10 returns empty/failure
- `option_chain` API only works for NSE indices (IDX_I). BSE not supported — use two-pass `ticker_data` instead
- No Dhan REST endpoint for BSE index (SENSEX) spot price — use nearest SENSEX FUTIDX LTP via background thread
- `intraday_minute_data` requires `from_date` and `to_date` params (today's date as string "YYYY-MM-DD")
- DepthWebSocket exchange segment ints: NSE_FNO=1, BSE_FNO=2, NSE_EQ=3, BSE_EQ=4
- NIFTY index for chart data: security_id=`13`, exchange_segment=`NSE_EQ`, instrument_type=`INDEX`
- Dhan accepts order submissions synchronously but rejects via RMS ~1-2s later. Always poll order status before treating an order as filled
- Dhan rate-limits token generation to once per 2 minutes — if service restart-loops, stop, sleep 130s, then start
- Dhan error 805: "max active WS connections exceeded" — only one DepthWebSocket connection allowed. LTP feed and DOM feed merged into the same connection

### dashboard.py Structure

- All HTML is a Python triple-quoted f-string (the Flask route returns it)
- CSS is inline in a `<style>` block
- JS is inline in `<script>` blocks
- Line numbers shift as you edit — always grep for function names rather than relying on line numbers
- `_build_journal_page()` returns a plain string (not f-string) for the `/journal` route — avoids `{{`/`}}` escaping issues since there are no Python variables to interpolate
- Onclick handlers with string args need `\\'` in Python to produce `\'` in JS: `onclick="fn(\\'val\\')"`

### monitor.py — _execute_spread() Flow

```
Step 1: BUY hedge @ MARKET
Step 2: 200ms sleep
Step 3: SELL @ LIMIT or MARKET
Step 4: 2.5s sleep (wait for RMS rejection)
Step 5: Poll order book — check hedge_status and sell_status
Step 6: If any_rejected → emit SPREAD_FAILED, return (NO SL placed)
Step 7: If filled → place STOP_LOSS_MARKET BUY (exchange SL) + set_stop_loss() (monitored SL)
Step 8: update_spread_status(FILLED), emit SPREAD_FILLED
```

**Critical:** SL is placed AFTER rejection confirmation (step 7), not before. This prevents a spurious 3rd order when spreads are rejected by RMS.

### trade_manager.py — StopLossTarget

```python
@dataclass
class StopLossTarget:
    position_security_id: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    trailing_sl: bool = False
    trailing_sl_points: float = 0.0
    trailing_sl_trigger: float = 0.0
    current_sl_price: Optional[float] = None
    exchange_sl_order_id: Optional[str] = None  # Dhan order ID for exchange STOP_LOSS_MARKET
    is_active: bool = True
```

`set_stop_loss(security_id, sl_price, exchange_sl_order_id="")` — pass the Dhan order ID when registering after spread fills.

### trade_journal.py Structure

- Single `TradeJournal` class, single SQLite connection with `threading.Lock()`
- `_create_tables()` creates all 4 tables on init (safe to add new tables here — uses `CREATE TABLE IF NOT EXISTS`)
- `SCREENSHOTS_DIR` = `<project_root>/journal_screenshots/` — created on import
- `save_screenshot(data_url)` extracts base64, saves as `<uuid>.png`, returns filename

### BSE Spot Updater (`dashboard.py`)

`_start_bse_spot_updater()` runs a daemon thread that fetches nearest SENSEX FUTIDX LTP every 3s and stores it in `_bse_last_spot`. The `global _bse_last_spot` declaration **must be inside the `_run()` inner function** (not just inside `_start_bse_spot_updater`) — Python ignores `global` in outer scope for assignments in inner functions.

---

## Pending / Next Work

- **Confirm WebSocket tick field names** — check `RAW TICK:` in platform.log with open positions, then remove the debug log line from `monitor._on_market_tick()`
- **Remove REST fallbacks** once WebSocket order rejection confirmed reliable:
  - `check_sl_tp_triggers` call in `_tick()` (monitor.py) — redundant once WebSocket SL/TP confirmed
  - Keep the 2.5s poll in `_execute_spread()` — it gates SL placement, not just rejection detection
- **Journal: naked short entry** — SELL MKT button path not yet wired to `createJournalEntry()` (only spread EXECUTE is wired). Low priority
- **SENSEX ATM price fallback** — when `ticker_data` returns 0 for ATM strikes, try `intraday_minute_data` last close
- **Hotkeys** — discussed but not started
- **Spot price real-time** — index spot still updates every 2s via REST (low priority)

---

## Research / Experiments (not in platform)

- `generate_pe_demo.py` — generates `demo_pe_rejections.html`: ATM PE 1m candle chart with rejection detection markers, 20 EMA (blue), 50 EMA (gold), RSI(14), MACD(12,26,9). Accuracy was 59% at 10-candle horizon — not reliable enough to trade on alone
- `analyse_rejections.py` — rejection accuracy on Nifty futures (31% — abandoned)
- `analyse_rejections_pe.py` — rejection accuracy on ATM PE (59% at 10 candles)
- `journal_demo.html` — standalone demo of journal UI layout (superseded by live `/journal` route)

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
Journal: `http://YOUR_VPS_IP:5555/journal`

---

## How to Resume With a New Claude Session

1. Open this repo in Claude Code (web or CLI)
2. Say: *"Read CLAUDE.md and continue development on the risk management platform"*
3. Claude will read this file and have full context to continue

The active branch is `claude/laughing-fermi-9HTg6`. All work goes there.
