# Claude Code - Project Context & Handoff Guide

This file provides all context needed for a new Claude Code session to continue
work on this project without needing the previous conversation history.

## Project Overview

**Trade Management Platform** — A prop-firm style risk management system for
Nifty options trading on Dhan (primary broker). Enforces daily loss limits,
profit locks, trailing drawdowns, and cooldowns with tamper-proof encrypted
state. Optionally integrates Fyers DOM Analyzer for real-time market depth.

**Owner/User:** Niket Biyani
**Deployment:** VPS (manual + systemd), accessed via web dashboard
**Primary branch:** `master`
**Fyers feature branch:** `claude/integrate-fyers-websockets-wDrFY`

---

## File Architecture

```
Risk-Management/
│
├── CORE APPLICATION
│   ├── main.py                 # Entry point. Starts monitor thread + Flask dashboard
│   ├── config.py               # All settings from .env (typed, with defaults)
│   ├── dashboard.py            # Flask web UI (~3800 lines). SocketIO real-time updates
│   │                           # Contains ALL HTML/CSS/JS inline as a Python string
│   │                           # (DASHBOARD_HTML template + Flask routes)
│   └── .env.example            # Template for environment variables
│
├── TRADING & RISK ENGINE
│   ├── dhan_api.py             # Dhan broker API wrapper (orders, positions, kill switch)
│   ├── risk_engine.py          # Core risk calculations, lockout logic
│   ├── monitor.py              # Polls Dhan every 2s during market hours (9:15-15:30 IST)
│   ├── trade_manager.py        # SL/TP management, spread detection, trailing stops
│   ├── order_interceptor.py    # Pre-trade risk checks, blocks risky orders
│   ├── pnl_tracker.py          # Realized & unrealized P&L calculation
│   └── trade_journal.py        # Trade history logging to JSON
│
├── STATE & PERSISTENCE
│   ├── state_manager.py        # AES-128 encrypted state (lockout, HWM, profit floor)
│   └── instrument_cache.py     # Caches Dhan option symbols for faster lookup
│
├── TOKEN MANAGEMENT
│   ├── token_manager.py        # Auto-refresh Dhan tokens via PIN + TOTP
│   └── update_token.sh         # Manual token update script
│
├── FYERS DOM ANALYZER (OPTIONAL — enabled by FYERS_ENABLED=true in .env)
│   ├── fyers_auth.py           # OAuth 2.0 flow with Fyers API
│   ├── fyers_database.py       # Encrypted token storage (SQLAlchemy + Fernet)
│   ├── fyers_websocket.py      # Real-time 50-level order book via WebSocket
│   ├── msg.proto               # Protobuf schema for Fyers TBT data
│   ├── msg_pb2.py              # Compiled protobuf (DO NOT edit, regenerate from .proto)
│   └── apply_fyers_patch.py    # Patcher: adds Fyers to base dashboard.py
│
├── STARTUP & SERVICE
│   ├── start.sh                # Launch in background, health check, PID tracking
│   ├── stop.sh                 # Graceful shutdown
│   ├── status.sh               # Check if running
│   └── install_service.sh      # Install as systemd service
│
├── TESTING
│   ├── test_risk_safety.py     # Unit tests for risk engine
│   ├── test_stress_safety.py   # 80 stress tests for all safety parameters
│   └── simulate_safety.py      # Interactive risk scenario simulation
│
└── DOCS
    ├── README.md               # Full setup & feature documentation
    ├── COMMANDS.md             # Quick reference for CLI operations
    └── CLAUDE.md               # This file (session continuity)
```

---

## How dashboard.py Works

The dashboard is a **single-file Flask app** with an inline HTML template
(`DASHBOARD_HTML` — a large triple-quoted Python string). It contains:

- **HTML/CSS** for the entire UI (risk meters, positions, spreads, option chain,
  order placement, trade journal, DOM analyzer)
- **JavaScript** inline in `<script>` blocks:
  - First `<script>` in `<head>`: Global functions (switchOrderTab,
    switchJournalTab, switchPage) — must be globally accessible for onclick handlers
  - Second `<script>` before `</body>`: CDN loaders (Socket.IO, Chart.js),
    all application logic (polling, SL/TP, option chain, DOM analyzer)
- **Flask routes** for REST API endpoints
- **SocketIO** for real-time push updates

### Important: Two `<script>` blocks

1. **Early block (in `<head>`)** — tab switching functions defined here so they're
   globally available for `onclick="switchPage('dom')"` etc. These MUST stay
   in the early block or onclick handlers can't find them.

2. **Main block (before `</body>`)** — all app logic, event handlers, API calls.
   CDN scripts (Socket.IO, Chart.js) loaded asynchronously.

---

## Fyers Integration Architecture

The Fyers DOM Analyzer is an **optional addon** that adds a second tab to the
dashboard. It's integrated via `apply_fyers_patch.py` — a patcher that modifies
the base dashboard.py to add Fyers support without merge conflicts.

### How the Patcher Works

`apply_fyers_patch.py` modifies 5 files:
1. **requirements.txt** — adds websockets, protobuf, argon2-cffi, sqlalchemy
2. **config.py** — adds FYERS_ENABLED, FYERS_API_KEY, etc.
3. **main.py** — adds Fyers midnight cleanup scheduler
4. **.env.example** — adds Fyers configuration section
5. **dashboard.py** — adds:
   - Fyers conditional imports
   - Page tab navigation (Risk Dashboard / DOM Analyzer tabs)
   - switchPage() in early `<script>` block
   - page-risk wrapper div
   - DOM Analyzer HTML (page-dom div)
   - DOM Analyzer JavaScript
   - Fyers Flask routes (/fyers/connect, /fyers/callback, /fyers/logout, etc.)
   - Fyers state in index() template variables

The patcher is **idempotent** (safe to run multiple times) and includes a
**repair function** that fixes known issues on already-patched files.

### Patcher Anchors

The patcher finds specific text anchors in the source files to insert code.
If the user's local changes move these anchors, the patcher may fail. Key anchors:

| File | Anchor | Purpose |
|------|--------|---------|
| dashboard.py | `</script>\n</head>` | Insert switchPage() |
| dashboard.py | `<h1>Risk Management Dashboard</h1>` | Add page tabs |
| dashboard.py | `<div id="lockout-banner"...>` | Open page-risk wrapper |
| dashboard.py | `<div class="footer">` | Close page-risk + add DOM HTML |
| dashboard.py | `</script>\n</body>\n</html>` | Insert DOM analyzer JS |
| dashboard.py | `socketio.run(app` | Add Fyers routes |
| config.py | `DASHBOARD_HOST` | Insert Fyers config vars |
| main.py | `main():` | Add Fyers midnight scheduler |

### Data Flow

```
Fyers OAuth Login ──→ fyers_auth.py ──→ access_token stored in fyers_database.py
                                                    │
                                                    ▼
dashboard.py ────→ fyers_websocket.py ────→ Fyers WebSocket (wss://rtsocket-api.fyers.in)
      │                    │
      │                    │ protobuf messages (msg_pb2.py)
      │                    ▼
      │           50-level order book maintained in memory
      │                    │
      │                    │ SocketIO emit('dom_update', data)
      ◄────────────────────┘
      │
      ▼
DOM Analyzer tab shows: bids/asks, imbalance %, spread, LTP
```

---

## User's VPS Setup

The user's VPS runs from commit that includes their own local changes.
Key thing to know: **the user applies changes via the patcher on their VPS**.

### Deployment Workflow

1. User's VPS has `master` branch with their own local commits
2. New features are developed on the feature branch
3. New files (fyers_auth.py, etc.) are copied via:
   ```bash
   git fetch origin claude/integrate-fyers-websockets-wDrFY
   git checkout origin/claude/integrate-fyers-websockets-wDrFY -- \
     apply_fyers_patch.py fyers_auth.py fyers_database.py \
     fyers_websocket.py msg.proto msg_pb2.py
   ```
4. Patcher modifies existing files:
   ```bash
   python3 apply_fyers_patch.py
   ```
5. Restart:
   ```bash
   bash stop.sh && bash start.sh
   ```

### Important: Why the Patcher Exists

The user has local commits on `master` that aren't on the remote. A simple
merge/rebase of the feature branch would conflict with their changes. The
patcher approach lets us inject Fyers code into their modified dashboard.py
without knowing the exact state of their file.

---

## Known Issues & Past Bugs

### Fixed

1. **switchPage not defined** — Function was originally placed inside the main
   `<script>` block. If the patcher's regex anchor didn't match (user had code
   between `initOptionChain();` and `</script>`), the function was never
   inserted but the tab buttons were. Fixed by: (a) moving switchPage to early
   `<script>` block, (b) adding repair_dashboard() to fix already-patched files.

2. **API refresh stuck on "Refreshing..."** — The fetch call to
   `/api/token/refresh` had no timeout. If Dhan API hung, the button stayed
   stuck forever. Fixed by adding 30s AbortController timeout.

3. **Patcher JS anchor too fragile** — Original regex required
   `initOptionChain();\n    </script>` to be adjacent. Changed to use
   `</script>\n</body>\n</html>` (end of file) which is always stable.

### Configuration Notes

- **FYERS_SYMBOL** must match Fyers format: `NSE:NIFTY25JULFUT` (update monthly)
- **FYERS_LOT_SIZE** must match the symbol's lot size (NIFTY=75, BANKNIFTY=15)
- **FYERS_REDIRECT_URL** must match what's configured in Fyers API app settings
- Fyers tokens are daily (expire at midnight). Midnight cleanup in main.py
  handles logout automatically

---

## Testing

```bash
# Run all safety tests
python3 test_risk_safety.py
python3 test_stress_safety.py

# Interactive simulation
python3 simulate_safety.py

# Verify patcher works on clean base
git show master:dashboard.py > /tmp/test_dash.py
# ... (set up temp dir, run patcher, check output)
```

---

## Environment Variables Quick Reference

### Required (Dhan)
| Variable | Description |
|----------|-------------|
| `DHAN_CLIENT_ID` | Dhan client ID |
| `DHAN_ACCESS_TOKEN` | Dhan API token (refreshed daily) |

### Optional (Auto Token Refresh)
| Variable | Description |
|----------|-------------|
| `DHAN_PIN` | 6-digit Dhan login PIN |
| `DHAN_TOTP_SECRET` | TOTP secret from Dhan 2FA setup |

### Optional (Fyers DOM Analyzer)
| Variable | Default | Description |
|----------|---------|-------------|
| `FYERS_ENABLED` | `false` | Enable Fyers DOM Analyzer |
| `FYERS_API_KEY` | — | Fyers App ID |
| `FYERS_API_SECRET` | — | Fyers App Secret |
| `FYERS_REDIRECT_URL` | `http://127.0.0.1:5555/fyers/callback` | OAuth callback URL |
| `FYERS_WEBSOCKET_URL` | `wss://rtsocket-api.fyers.in/versova` | WebSocket endpoint |
| `FYERS_SYMBOL` | `NSE:NIFTY25JULFUT` | Symbol for DOM analysis |
| `FYERS_LOT_SIZE` | `50` | Lot size for the symbol |
| `FYERS_DATABASE_URL` | `sqlite:///fyers_auth.db` | Token storage location |
| `FYERS_API_KEY_PEPPER` | — | Encryption pepper for token storage |

### Risk Settings
| Variable | Default | Description |
|----------|---------|-------------|
| `DAILY_MAX_LOSS` | `5000` | Max daily loss (INR) before lockout |
| `DAILY_PROFIT_TARGET` | `20000` | Profit target (INR) |
| `MAX_OPEN_POSITIONS` | `5` | Max concurrent positions |
| `MAX_SINGLE_TRADE_RISK` | `2000` | Max risk per trade (INR) |
| `MAX_ORDER_QUANTITY` | `1800` | Max lots per order |
| `PROFIT_LOCK_THRESHOLD` | `10000` | P&L level to activate profit lock |
| `PROFIT_LOCK_PERCENTAGE` | `50` | % of profit to lock as floor |
| `TRAILING_DRAWDOWN_ENABLED` | `true` | Enable HWM-based drawdown limit |
| `TRAILING_DRAWDOWN_PERCENTAGE` | `50` | Max drawdown % from HWM |
| `COOLDOWN_AFTER_LOSS` | `300` | Pause seconds after a loss |
| `COOLDOWN_AFTER_CONSECUTIVE_LOSSES` | `600` | Pause after N consecutive losses |
| `CONSECUTIVE_LOSS_COUNT` | `3` | N for consecutive loss trigger |
| `DASHBOARD_PORT` | `5555` | Web UI port |
| `DASHBOARD_HOST` | `0.0.0.0` | Bind address |
| `MONITOR_INTERVAL` | `2` | Position poll interval (seconds) |
