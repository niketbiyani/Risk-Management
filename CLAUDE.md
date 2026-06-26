# Claude Code Context — Risk Management Platform

This file exists so a new Claude session can pick up exactly where the last one left off.

---

## What This Project Is

A single-VPS trade management platform for **Nifty/SENSEX options scalping** on 15-second charts via Dhan broker. The user trades exclusively **two-legged credit spreads** (bear call, bull put) and occasional naked shorts.

**Primary file:** `dashboard.py` — ~6200 lines, single file containing Flask backend + all inline HTML/CSS/JS.

**Key files:**
| File | Purpose |
|---|---|
| `dashboard.py` | Flask app, all HTML/CSS/JS inline, all API routes |
| `dhan_api.py` | Dhan REST API wrapper + DepthWebSocket (LTP + DOM) + OrderUpdate WebSocket |
| `monitor.py` | Background position monitor, executes spreads, manages SL/TP, auto-journals orders |
| `trade_manager.py` | Spread detection, SL/TP logic, exchange SL order tracking, pending spread queue, position cache |
| `trade_journal.py` | SQLite-backed trade journal — analytics + detailed entries with screenshots |
| `instrument_cache.py` | Local SQLite cache of all Dhan instruments |
| `state_manager.py` | Encrypted daily state (IST-aware), risk rules, trade history in memory |
| `risk_engine.py` | Risk enforcement — max loss, profit lock, trailing drawdown, lockout logic |
| `config.py` | Reads `.env` settings |
| `main.py` | Entry point |

**Branch for all work:** `claude/laughing-fermi-9HTg6`

**Deploy:** VPS runs `git pull origin claude/laughing-fermi-9HTg6` then `sudo systemctl restart risk-manager`

**Logs:** `tail -f /root/Risk-Management/platform.log`

---

## Infrastructure

### VPS Access

- Dashboard: `http://88.208.255.34` (port 80 via Nginx, password protected)
- Trade Analyser: `http://88.208.255.34:5556` (port 5556, open)
- Direct ports 5555 blocked by UFW firewall (Nginx proxies everything)

### Nginx Setup

- Config: `/etc/nginx/sites-available/trader`
- Password file: `/etc/nginx/.htpasswd`
- Proxies `/` → port 5555 (dashboard), `/analyser/` → port 5556 (trade analyser — has 404 issue, see below)
- Reset password: `sudo htpasswd /etc/nginx/.htpasswd trader`

### Firewall (UFW)

- Port 22: ALLOW (SSH)
- Port 80: ALLOW (Nginx)
- Port 5555: DENY (direct access blocked)
- Port 5556: ALLOW (trade analyser — direct until prefix routing fixed)

### Services

- `risk-manager` — main dashboard (port 5555)
- `trade-analyser` — trade analyser (port 5556, `/root/trade-analyser/`)
- `nginx` — reverse proxy + auth

### Known Infrastructure Issue

The trade-analyser app doesn't support a URL prefix (`/analyser/`), so Nginx proxying via `/analyser/` returns 404 for internal links. **Fix needed in the trade-analyser repo:** add `APPLICATION_ROOT = '/analyser'` config and `ProxyFix` middleware. Until then, trade-analyser is accessed directly at port 5556, and the `/journal` redirect points to `http://<host>:5556`.

---

## Current Feature State

### Option Chain

- Supports **NIFTY** (NSE_FNO, IDX_I) and **SENSEX** (BSE, BSE_FNO) — BANKNIFTY was removed
- REST poll every **2 seconds** for OI/IV and full chain structure — LTP no longer comes from REST (moved to DepthWebSocket)
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
  - `_oc_ltp_subscribed` set only grows (never-shrink keeps strikes warm)
- **Known issue:** Some SENSEX ATM strikes return 0 from `ticker_data` — user types price manually in quick bar
- **Mobile:** Option chain is hidden on screens <768px (`.desktop-only` CSS class)

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

### Equity Curve

Replaced the old intraday P&L chart (tick-by-tick line) and Trade History card.

- **Location:** Compact version (180px) sits next to the Trailing Drawdown panel in the `grid grid-detail` div
- **Data source:** `/api/equity_curve` endpoint — fetches today's closed trades from trade-analyser, sorted by exit time
- **Display:** One dot per closed trade (green = profit, red = loss), connected by blue cumulative realized P&L line
- **Lockout floor:** Red dashed horizontal line shown when profit lock is active
- **Summary:** Win/loss count + win rate shown in card header (e.g. `7W / 3L (70%)`)
- **Refresh:** Every 60s independently of the 2s status poll (no tick noise — only updates when trades close)
- **Canvas IDs:** `equity-chart` (compact), `equity-chart-full` removed (was duplicate)
- **JS functions:** `initEquityCharts()`, `updateEquityCharts(eqData)`, `_applyEqData()`, `refreshEquityCurve()`

### Trailing Drawdown Panel

Redesigned to show "gap to lockout" as primary metric.

- **Card IDs:** `dd-card`, `dd-badge`, `dd-inactive`, `dd-active`
- **Key elements:** `dd-gap` (big number = how far total P&L is above the lockout floor), `dd-floor`, `dd-hwm`, `drawdown-bar`
- **Color logic:** gap < 25% of limit → red, < 60% → amber, ≥ 60% → green
- **Inactive state:** shown when realized P&L < `PROFIT_LOCK_THRESHOLD` (₹10,000)
- **Active state:** shown once HWM established; bar fills left-to-right as gap shrinks

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

### Risk Engine (risk_engine.py)

All risk checks run in `evaluate_pnl(realized, unrealized)` every monitor cycle:

1. **Daily max loss** — `total_pnl <= -DAILY_MAX_LOSS` → lockout
2. **Profit lock activation** — `realized_pnl >= PROFIT_LOCK_THRESHOLD` → lock floor at `PROFIT_LOCK_PERCENTAGE`%
3. **Profit lock floor breach** — `realized_pnl < floor` → lockout
4. **Trailing drawdown** — `HWM - total_pnl >= HWM * TRAILING_DRAWDOWN_PERCENTAGE / 100` → lockout

**Trailing drawdown design (important):**
- HWM tracks **realized P&L only** — `realized_pnl > hwm` → advance HWM
- Drawdown is measured as `max(0, hwm - total_pnl)` — unrealized losses count against you
- This prevents HWM from ratcheting up on paper gains (which would cause the display to jump and potentially trigger false lockouts when unrealized swings back)
- **Root cause of past false lockouts:** Dhan's `realizedProfit` field = `(sellAvg - buyAvg) × totalQty` — averages ALL buys/sells per instrument across the day, NOT FIFO. Inflates realized by ~40% on active scalping days. Fix: use trade-analyser FIFO calculation instead (see below)

### Realized P&L Source (Critical)

Dhan's `realizedProfit` is **unreliable for scalpers**. It computes `(sellAvg - buyAvg) × totalQty` — averaging all re-entries, not FIFO per trade. On active scalping days this inflates realized P&L by ~40%, causing false HWM advances and premature trailing drawdown lockouts.

**Fix in `monitor.py`:**
- `_refresh_analyser_realized()` — calls trade-analyser `/api/import` (POST) then `/api/trades?date=today`, sums `pnl` for `status == "CLOSED"` trades
- Runs every 60s in a background thread
- `_analyser_realized_pnl` used as realized P&L; Dhan's `unrealizedProfit` still used for unrealized
- Falls back to Dhan `realizedProfit` if trade-analyser unavailable (`_analyser_realized_pnl is None`)

### State Management (state_manager.py)

- Fernet-encrypted daily state file: AES-128-CBC + SHA-256 HMAC integrity
- **IST-aware date:** `_today_ist()` uses explicit UTC+5:30 timezone — state resets at IST midnight, not UTC midnight (VPS is UTC)
- All `date.today()` replaced with `_today_ist()` in state comparisons
- Lockout is permanent within the trading day — state cannot be tampered to bypass
- Emergency override: `POST /api/admin/unlock` resets locked state (for testing only)
- Surgical HWM reset: `POST /api/admin/reset_hwm` — resets only `high_water_mark`, `trailing_drawdown_active`, `profit_lock_active`, `profit_lock_floor`, `profit_lock_level` without clearing full state

### Auto-Journal (monitor.py)

The monitor automatically creates journal entries from live order data, with full deduplication:

- `_auto_journal_orders(orders)` runs on every position poll cycle
- **Seeding on startup:** existing order IDs from today loaded into `_journaled_order_ids` to skip already-journaled orders
- **DB-level dedup:** `get_today_security_ids()` checked before creating any entry — prevents duplicates even after restart
- **In-memory dedup:** `_journaled_order_ids` set grows throughout the session
- **Second pass:** scans open entries, closes them when matching BUY orders arrive later
- Lot sizes: NIFTY=75, SENSEX=20, BANKNIFTY=35

**Order data fallback chain** (market close / after hours):
1. `get_trade_book()` — Dhan execution database, persists after market close
2. `get_order_book()` — live session only, clears after close
3. `get_trade_history(from_date, to_date)` — historical, DD-MM-YYYY format required
4. `order_cache.json` — disk snapshot written during session, reloaded on restart

**Trade book normalization (`_normalize_trade_book`):**
- Converts `tradedPrice` → `price`, `tradedQuantity` → `filledQty`
- Handles Dhan trade book field names vs order book field names

**CSV import (`_process_csv_trades`):**
- Bidirectional FIFO pairing: one queue per symbol
- Each row pairs with next opposite-transaction row for same symbol
- Handles BUY-first sequences (hedge placed first) correctly
- Remaining unpaired SELLs → OPEN entries

### Trade Analyser (port 5556)

A separate app (`niketbiyani/trade-analyser`) handles all trade P&L analysis:
- `GET /api/trades?date=YYYY-MM-DD` — list of trades with entry/exit prices, P&L, legs
- `GET /api/dates` — dates for which trade data exists
- `POST /api/import` — re-imports today's trades from Dhan trade book (requires `Content-Type: application/json` + `{}` body)

**Dashboard integration:**
- `/journal` route redirects to `http://<host>:5556` (302 redirect) — direct port, not via Nginx prefix
- `ANALYSER_URL = "http://localhost:5556"` constant in dashboard.py
- `_analyser_trades(days)` helper fetches from trade-analyser per day, auto-triggers import if today missing
- `_analyser_import()` triggers import; safe to call any time
- `/api/equity_curve` endpoint fetches today's closed trades from analyser for the equity curve chart
- Analytics page fetches via `/api/analyser/dates` and `/api/analytics/day_trades?date=YYYY-MM-DD`

### Analytics Page (`/analytics`)

- **FY toggle** at top — shows stats for selected financial year (Apr–Mar). Defaults to current FY. Navigate with `‹` `›` arrows. Label format: `FY 2025-26`
- **Stats cards** — Total Days, Total Trades, Overall P&L, Trade Win Rate, Day Win Rate, Avg Daily P&L, Profitable Days, Losing Days, Best Day, Worst Day — all filtered to selected FY
- **Calendar** — monthly view, click any day to see day detail
- **Day detail panel** — day stats + trade table with CE/PE filter tabs + equity curve at bottom
- **Day equity curve** — one dot per closed trade (green/red), cumulative P&L line. Destroys and redraws on each day selection. Uses Chart.js loaded from CDN
- **CE/PE tabs** — `filterDayTrades("all"|"CE"|"PE")` — uses `&quot;` HTML entity (not `\'`) due to triple-quoted Python string context
- **Silent background refresh** — `setInterval(loadAll(true), 120000)` — refreshes data every 2 min without spinner
- **Mobile responsive** — 2-column stat grid, compact calendar cells, smaller fonts on screens <600px

### Mobile Support

Two approaches available:

1. **Auto-detect on main URL (`/`):** CSS `@media (max-width: 768px)` hides `.desktop-only` sections (option chain + DOM, Today's Trades table). Core cards (P&L, risk, positions, equity curve) remain visible. Positions, pending orders, equity curve all functional.

2. **Dedicated mobile page (`/mobile`):** Purpose-built touch-optimised view. Single-column layout, large touch targets. Shows: P&L summary, trailing drawdown bar, positions with 50%/EXIT ALL buttons, pending spreads with cancel, equity curve chart. Auto-refreshes every 3s. No option chain, no DOM.

### CSV Import via Dashboard

Upload a Dhan trade book export CSV at `POST /api/journal/upload_csv`:
- Multipart file upload
- Expected columns: `Trade #`, `Stock Name`, `Transaction`, `Product Type`, `Quantity`, `Price (₹)`, `Net Amount (₹)`, `Timestamp`
- Timestamp format: `11 Jun 2026 12:45:52` → parsed with `%d %b %Y %H:%M:%S`
- Bidirectional FIFO pairing → correctly handles 32 rows = 16 paired trades

### Trade Journal (dashboard sidebar + screenshots)

Two-part system sharing one SQLite DB (`trade_journal.db`):

**Part 1 — Analytics (dashboard sidebar):**
- `trade_journal.py` `TradeJournal` class — `trades`, `daily_summary`, `pnl_snapshots` tables
- Populated by `risk_engine.py` → `state_manager.record_trade()` when trades complete
- Data now proxied from trade-analyser for accuracy

**Part 2 — Detailed entries with screenshots:**
- `trade_entries` table — one row per spread/naked trade, tracks entry+exit prices, screenshots, notes
- Screenshots stored as PNG files in `journal_screenshots/` directory
- **Entry screenshot:** captured in browser JS at moment of EXECUTE — fetches Nifty 1m candles (`/api/chart/nifty`), renders 150 candles to 900×200 offscreen canvas with gold ENTRY price line, POSTs PNG to server
- **Exit screenshot:** captured same way with blue EXIT line — triggered by:
  - `sl_tp_triggered` SocketIO event (SL/TP hit)
  - `_placeExitOrder()` success (EXIT MKT / EXIT LMT / partial exit buttons)
- `_journalOpenEntries` JS map: `sell_security_id → entry_id` — links entry to exit within browser session
- **Refresh recovery:** `closeJournalEntry()` calls `GET /api/journal/open_entry/<security_id>` when entry_id not in local map — queries most recent open `trade_entries` row for that sell leg
- **Spread detection:** both `_spreadSellLeg` + `_spreadBuyLeg` set → spread; only sell leg (SELL MKT) → naked

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
- `intraday_minute_data` requires `from_date` and `to_date` params as strings "YYYY-MM-DD"
- `get_trade_history(from_date, to_date)` requires **DD-MM-YYYY** format (not YYYY-MM-DD)
- DepthWebSocket exchange segment ints: NSE_FNO=1, BSE_FNO=2, NSE_EQ=3, BSE_EQ=4
- NIFTY index for chart data: security_id=`13`, exchange_segment=`NSE_EQ`, instrument_type=`INDEX`
- Dhan accepts order submissions synchronously but rejects via RMS ~1-2s later. Always poll order status before treating an order as filled
- Dhan rate-limits token generation to once per 2 minutes — if service restart-loops, stop, sleep 130s, then start
- Dhan error 805: "max active WS connections exceeded" — only one DepthWebSocket connection allowed. LTP feed and DOM feed merged into the same connection
- Order book (`get_order_book`) includes CANCELLED and REJECTED orders (SL orders) — do not use for journal pairing. Use trade book (executed trades only)
- Order book clears after market close — trade book (`get_trade_book`) persists and should be the primary source
- `realizedProfit` from positions API is WRONG for scalpers — uses average price not FIFO. Use trade-analyser instead

### dashboard.py Structure

- All HTML is a Python triple-quoted f-string (the Flask route returns it)
- CSS is inline in a `<style>` block
- JS is inline in `<script>` blocks
- Line numbers shift as you edit — always grep for function names rather than relying on line numbers
- Analytics page (`_build_analytics_page()`) is a plain `'''` string (not f-string) — `\'` inside it renders as literal backslash-quote in HTML, breaking JS. Use `&quot;` HTML entity for quoted strings inside onclick handlers in that context
- Onclick handlers in f-string context need `\\'` in Python to produce `\'` in JS: `onclick="fn(\\'val\\')"`
- `/journal` route redirects to `http://<host>:5556` (direct port, not Nginx proxy)

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
- `get_today_security_ids()` — returns set of security_ids already journaled today (used for dedup)

### state_manager.py — IST Timezone Fix

VPS runs UTC. Market day is IST. Using `date.today()` would give the wrong date after 18:30 UTC (midnight IST).

```python
@staticmethod
def _today_ist() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")
```

All state date comparisons and resets use `_today_ist()` instead of `date.today()`.

### BSE Spot Updater (`dashboard.py`)

`_start_bse_spot_updater()` runs a daemon thread that fetches nearest SENSEX FUTIDX LTP every 3s and stores it in `_bse_last_spot`. The `global _bse_last_spot` declaration **must be inside the `_run()` inner function** (not just inside `_start_bse_spot_updater`) — Python ignores `global` in outer scope for assignments in inner functions.

### Trailing Drawdown HWM Design

HWM = **realized P&L only**. Drawdown = `max(0, hwm - total_pnl)`.

Before this fix: HWM used `total_pnl` (realized + unrealized). Every WebSocket tick that marked an open position to profit would advance the HWM, so when price moved back the drawdown would spike — causing the display to jump and risking false lockouts.

Now: HWM only advances when you close a winning trade. Unrealized gains don't move the HWM; unrealized losses still count in the drawdown calculation.

Additionally: Dhan's `realizedProfit` was inflating HWM by using average-price math instead of FIFO. Fixed by sourcing realized P&L from trade-analyser instead.

### Memory & System

- VPS: 848MB total RAM, 1GB swap added
- `risk_guardian` process was killed (not needed) — freed ~50MB
- Logging: Python `FileHandler` writes to `platform.log`. systemd `StandardOutput` must NOT also point to `platform.log` — causes duplicate log lines. Check `/etc/systemd/system/risk-manager.service` to ensure `StandardOutput` is not set to the same file.

---

## Pending / Next Work

- **Confirm WebSocket tick field names** — check `RAW TICK:` in platform.log with open positions to confirm field names (`security_id`/`securityId`/`Security Id` and `LTP`/`ltp`/`last_price`), then remove the debug log line from `monitor._on_market_tick()`
- **Remove REST fallbacks** once WebSocket order rejection confirmed reliable:
  - `check_sl_tp_triggers` call in `_tick()` (monitor.py) — redundant once WebSocket SL/TP confirmed
  - Keep the 2.5s poll in `_execute_spread()` — it gates SL placement, not just rejection detection
- **Remove diagnostic logs** added for debugging: `ORDER BOOK RAW`, `TRADE BOOK RAW`, `ORDER TIME FIELDS` in monitor.py/dhan_api.py
- **Journal: naked short entry** — SELL MKT button path not yet wired to `createJournalEntry()` (only spread EXECUTE is wired). Low priority
- **SENSEX ATM price fallback** — when `ticker_data` returns 0 for ATM strikes, try `intraday_minute_data` last close
- **Trade-analyser prefix fix** — add `APPLICATION_ROOT = '/analyser'` + `ProxyFix` to trade-analyser app so Nginx can proxy at `/analyser/` and port 5556 can be firewalled
- **Hotkeys** — discussed but not started
- **Spot price real-time** — index spot still updates every 2s via REST (low priority)

---

## Research / Experiments (not in platform)

- `generate_pe_demo.py` — generates `demo_pe_rejections.html`: ATM PE 1m candle chart with rejection detection markers, 20 EMA (blue), 50 EMA (gold), RSI(14), MACD(12,26,9). Accuracy was 59% at 10-candle horizon — not reliable enough to trade on alone
- `analyse_rejections.py` — rejection accuracy on Nifty futures (31% — abandoned)
- `analyse_rejections_pe.py` — rejection accuracy on ATM PE (59% at 10 candles)
- `journal_demo.html` — standalone demo of journal UI layout (superseded by trade-analyser redirect)

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

# Reset password
sudo htpasswd /etc/nginx/.htpasswd trader

# Check firewall
sudo ufw status

# Check memory
free -h
```

Dashboard: `http://88.208.255.34` (via Nginx, password protected)
Trade Analyser: `http://88.208.255.34:5556` (direct)
Analytics: `http://88.208.255.34/analytics`
Mobile view: `http://88.208.255.34/mobile`

---

## How to Resume With a New Claude Session

1. Open this repo in Claude Code (web or CLI)
2. Say: *"Read CLAUDE.md and continue development on the risk management platform"*
3. Claude will read this file and have full context to continue

The active branch is `claude/laughing-fermi-9HTg6`. All work goes there.
