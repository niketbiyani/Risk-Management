# Claude Code Context — Risk Management Platform

This file exists so a new Claude session can pick up exactly where the last one left off.

---

## What This Project Is

A single-VPS trade management platform for **Nifty/SENSEX options scalping** on 15-second charts via Dhan broker. The user trades exclusively **two-legged credit spreads** (bear call, bull put).

**Primary file:** `dashboard.py` — ~4400 lines, single file containing Flask backend + all inline HTML/CSS/JS.

**Other key files:**
- `dhan_api.py` — Dhan REST API wrapper
- `monitor.py` — background position monitor, executes spreads, manages SL/TP
- `trade_manager.py` — spread detection, SL/TP logic, pending spread queue
- `instrument_cache.py` — local SQLite cache of all Dhan instruments
- `config.py` — reads `.env` settings
- `main.py` — entry point

**Branch for all work:** `claude/laughing-fermi-9HTg6`

**Deploy:** VPS runs `git pull origin claude/laughing-fermi-9HTg6` then `sudo systemctl restart risk-manager`

---

## Current Feature State (as of last session)

### Option Chain
- Supports **NIFTY** (NSE_FNO, IDX_I) and **SENSEX** (BSE, BSE_FNO) — BANKNIFTY was removed
- Refreshes every **2 seconds** (server-side 2s cache to prevent Dhan rate-limit)
- SENSEX: uses two-pass LTP fetch (Dhan `ticker_data` limit ~9 IDs per call, 191 strikes total)
  - Pass 1: sample every 10th strike (coarse, finds ATM area)
  - Pass 2: fetch ATM±8 strikes precisely
  - Synthetic spot via put-call parity (Dhan has no BSE index LTP endpoint)
- **Known issue:** Some SENSEX ATM strikes return 0 from `ticker_data` (Dhan API inconsistency). User types price manually in quick bar.

### Spread Quick Bar (bottom of option chain panel)
Each chain row has **S** and **B** pill buttons. Clicking them populates the quick bar:

**Inputs:**
- `sqb-sell-price` — editable, pre-fills from LTP (blank if LTP=0). Dirty flag `_sqbSellPriceDirty` prevents chain refresh overwriting manual entry.
- `sqb-sell-sl` — mandatory SL (gold border). Must be > sell price to enable execute.
- `sqb-buy-price` — editable, pre-fills from buy leg LTP.

**Auto-qty:** `lots = floor(maxLoss / ((sl - sellPrice) × lotSize))`

**Buttons:**
- **⚡ EXECUTE LMT** — BUY hedge at MARKET first (200ms pause), then SELL at typed LIMIT price
- **⚡ MKT** — BUY hedge at MARKET, then SELL at MARKET (enabled with just SL + qty)
- **ARM TRIGGER** — queue spread to fire when sell leg LTP ≤ trigger price
- **SELL LMT / SELL MKT** — single-leg sell only (emergency: hedge filled, sell rejected)
- **BUY LMT / BUY MKT** — single-leg buy only

**Order sequence:** BUY hedge always goes first (reduces margin requirement), 200ms pause, then SELL. Backend: `monitor.py _execute_spread()`.

### Snapshot Chart (DOM panel)
- TradingView Lightweight Charts v4.1.3 from `cdn.jsdelivr.net`
- 1-minute OHLCV candles via Dhan `intraday_minute_data`
- Shows **full trading day** (all candles, no truncation)
- **Timezone:** Dhan returns IST strings ("09:15:00"). Server is UTC. Fix: parse naively with `.timestamp()` — chart displays correct IST times without any offset math (v4 has no timezone support).
- Live bar updates: option chain refresh every 2s extracts sell leg LTP, accumulates into current 1-min bar OHLC
- Auto-loads and switches to chart tab when user selects a sell leg via [S] button
- `[Depth]` / `[Chart]` tab toggle in DOM panel header

### Order Rejection Detection
- After placing spread, polls order book 2.5s later
- If `orderStatus == 'REJECTED'`: emits `SPREAD_FAILED` socket event, shows error toast
- Prevents false "Spread filled" toast when exchange rejects (e.g. insufficient funds)

---

## Key Technical Gotchas

### Dhan API quirks
- `ticker_data` for BSE_FNO requires **integer** security IDs, not strings. `{'BSE_FNO': [123]}` works, `{'BSE_FNO': ['123']}` fails silently.
- `ticker_data` limit: **~9 IDs per call**. More than ~10 returns empty/failure.
- `option_chain` API only works for NSE indices (IDX_I). BSE not supported — use two-pass `ticker_data` instead.
- No Dhan REST endpoint for BSE index (SENSEX) spot price — derive via put-call parity.
- `intraday_minute_data` requires `from_date` and `to_date` params (today's date).

### Python triple-quoted strings with JS
- Onclick handlers with string args need `\\'` in Python to produce `\'` in JS output.
- Example: `onclick="spreadSelectLeg(\\'sell\\', ...)"`

### dashboard.py structure
- All HTML is a Python triple-quoted string (the Flask route returns it)
- CSS is inline in a `<style>` block
- JS is inline in `<script>` blocks
- Line numbers shift as you edit — always grep for function names rather than relying on line numbers

---

## What Was Being Worked On Last

Everything in the spread quick bar is complete. The last changes added:
1. EXECUTE LMT / EXECUTE MKT buttons (both spread execution modes)
2. SELL LMT / SELL MKT / BUY LMT / BUY MKT single-leg exit buttons
3. Chart full-day view + IST timezone fix
4. 200ms BUY→SELL delay (up from 100ms for safety)

**Next logical things to work on** (not started):
- Live chart bar update wiring (currently updates only when option chain refreshes — could also update on DOM WebSocket price tick)
- SENSEX ATM price fallback (try `intraday_minute_data` last-close if `ticker_data` returns 0)
- Position-level quick exit buttons in the positions table (currently only global exit-all)

---

## VPS Workflow

```bash
# Pull latest
git pull origin claude/laughing-fermi-9HTg6

# Restart service
sudo systemctl restart risk-manager

# Watch logs
journalctl -u risk-manager -f

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
