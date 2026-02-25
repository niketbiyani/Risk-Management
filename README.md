# Trade Management Platform

Prop-firm style risk management for Nifty options trading on Dhan. Enforces daily loss limits, profit locks, and trailing drawdowns with tamper-proof state that **cannot be overridden** once triggered.

## Features

### Risk Management (Prop-Firm Style)
- **Daily Max Loss Limit** — Locks out trading when total P&L hits the loss limit
- **Profit Target** — Optional lockout when profit target is reached (prevent giving back gains)
- **Profit Lock** — Once realized P&L crosses a threshold (e.g. ₹10,000), locks a percentage (e.g. 50%) as a floor. If P&L falls below the floor, account locks for the day
- **Trailing Drawdown** — Tracks high water mark of total P&L. If drawdown from HWM exceeds the configured percentage, locks out
- **Max Open Positions** — Prevents overexposure
- **Max Order Quantity** — Hard limit per order
- **Max Single Trade Risk** — Blocks orders exceeding individual trade risk limit

### Lockout Enforcement
When lockout triggers:
1. All pending orders are cancelled
2. All open positions are closed at market
3. Dhan's Kill Switch API is activated (blocks ALL orders at broker level)
4. State is encrypted and locked — **cannot be manually edited or overridden**

### Trade Management
- **Stop Loss / Take Profit** — Set per-position from the dashboard
- **Trailing Stop Loss** — Moves SL as profit increases
- **Credit Spread Detection** — Automatically identifies bull put, bear call, bull call, bear put spreads and iron condors
- **Projected P&L** — Shows expected P&L at various underlying prices for options positions
- **Spread Analytics** — Max profit, max loss, breakeven points for detected spreads
- **Emergency Exit** — One-click close all positions

### Dashboard
- Real-time web dashboard with live P&L, risk meters, and position tracking
- Color-coded risk indicators (green → yellow → red)
- Lockout banner when account is locked
- Spread visualization with leg-level details
- Market Depth tab with 20-level order book, wall detection, and imbalance analysis
- Live option chain with auto-refresh

## Setup (First Time Only)

### 1. Create Virtual Environment & Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

Copy the example env file and add your Dhan credentials:

```bash
cp .env.example .env
```

Edit `.env`:
```
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_access_token

# Risk limits (adjust to your needs)
DAILY_MAX_LOSS=5000
DAILY_PROFIT_TARGET=20000
MAX_OPEN_POSITIONS=5
MAX_SINGLE_TRADE_RISK=2000

# Profit lock: lock 50% once you make 10k
PROFIT_LOCK_THRESHOLD=10000
PROFIT_LOCK_PERCENTAGE=50

# Trailing drawdown: lock out if drawdown exceeds 50% from high water mark
TRAILING_DRAWDOWN_ENABLED=true
TRAILING_DRAWDOWN_PERCENTAGE=50
```

Get your Dhan access token from https://web.dhan.co (API section).

## Running the Platform

### Option A: Auto-Start Service (Recommended)

Install once, then it runs automatically on boot and survives sleep/restart:

```bash
sudo bash install_service.sh
```

After that, the platform starts automatically. Control it with:

```bash
sudo systemctl start risk-manager      # Start
sudo systemctl stop risk-manager       # Stop
sudo systemctl restart risk-manager    # Restart
sudo systemctl status risk-manager     # Check status
journalctl -u risk-manager -f          # View live logs
```

### Option B: Manual Start

```bash
cd ~/Risk-Management
source venv/bin/activate
python main.py
```

### Other Run Modes

```bash
# Monitor only (no web UI)
python main.py --monitor

# Check current status without starting
python main.py --status
```

Dashboard opens at `http://localhost:5555`

## How It Works

### Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Web Dashboard (:5555)                  │
│      Real-time P&L / Risk / Positions / Spreads / DOM    │
└─────────────────────────┬────────────────────────────────┘
                          │ SocketIO
┌─────────────────────────▼────────────────────────────────┐
│                   Position Monitor                        │
│     Polls positions every 2s during market hours          │
│                                                           │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐│
│  │ Risk Engine  │  │Trade Manager │  │Order Interceptor ││
│  │             │  │              │  │                   ││
│  │• Loss limit │  │• SL/TP mgmt  │  │• Pre-trade checks ││
│  │• Profit lock│  │• Spread      │  │• Risk estimation  ││
│  │• Drawdown   │  │  detection   │  │• Order blocking   ││
│  │             │  │• Projections │  │                   ││
│  └──────┬──────┘  └──────────────┘  └──────────────────┘│
│         │                                                 │
│  ┌──────▼──────┐                                         │
│  │State Manager│  ← Encrypted, tamper-proof               │
│  │ (AES-128)   │  ← Auto-resets daily                     │
│  │             │  ← Cannot bypass lockout                  │
│  └─────────────┘                                          │
└────────────┬─────────────────────────┬───────────────────┘
             │ REST API                │ WebSocket
┌────────────▼─────────────────────────▼───────────────────┐
│                      Dhan API                             │
│  Orders / Positions / Kill Switch / Option Chain / Depth  │
└──────────────────────────────────────────────────────────┘
```

### Trading Workflow

You continue trading normally on Dhan charts (web/app). The platform monitors your account:

1. **Monitor** polls positions every 2 seconds
2. **Risk Engine** evaluates P&L against all configured rules
3. If any limit is breached → **lockout sequence** executes automatically
4. Lockout closes everything and activates Dhan kill switch
5. **State is encrypted** — deleting the state file won't help (it resets to locked)

### Profit Lock Example

```
Config: PROFIT_LOCK_THRESHOLD=10000, PROFIT_LOCK_PERCENTAGE=50

Timeline:
09:30  P&L = ₹0     → Trading normally
10:15  P&L = ₹8,000  → 2k to lock threshold
10:45  P&L = ₹12,000 → PROFIT LOCK ACTIVATED, floor = ₹6,000
11:00  P&L = ₹9,000  → Still above floor (₹6k), trading continues
11:30  P&L = ₹5,500  → BELOW FLOOR → LOCKOUT (close all, kill switch)
```

### Trailing Drawdown Example

```
Config: TRAILING_DRAWDOWN_PERCENTAGE=50, PROFIT_LOCK_THRESHOLD=10000

Timeline:
10:00  Total P&L = ₹15,000 → HWM = ₹15,000, drawdown limit = ₹7,500
10:30  Total P&L = ₹12,000 → Drawdown = ₹3,000 (within limit)
11:00  Total P&L = ₹18,000 → HWM moves to ₹18,000, limit = ₹9,000
11:30  Total P&L = ₹8,500  → Drawdown = ₹9,500 > ₹9,000 → LOCKOUT
```

## Dashboard API

The dashboard exposes REST endpoints for programmatic access:

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Full risk status |
| `/api/sl` | POST | Set stop loss |
| `/api/tp` | POST | Set take profit |
| `/api/exit` | POST | Exit single position |
| `/api/exit_all` | POST | Emergency close all |
| `/api/projections` | POST | Calculate option P&L projections |
| `/api/depth-diag` | GET | Depth WebSocket diagnostics |

## Options-Specific Notes

### Credit Spreads
The system automatically detects vertical spreads and iron condors from your positions. It shows:
- Net premium collected/paid
- Max profit and max loss
- Breakeven points
- Per-leg P&L

### Latency Considerations
Since you scalp, latency matters. The SL/TP management runs in the monitor loop (every 2s). For sub-second execution:
- Use Dhan's built-in bracket/cover orders for time-critical SL/TP
- The platform's SL/TP is best for trailing stops and soft targets
- Set `MONITOR_INTERVAL=1` in .env for faster polling (uses more API quota)

### Nifty Lot Sizes
The system respects Dhan's slice order API for quantities exceeding exchange freeze limits. Set `MAX_ORDER_QUANTITY` based on your risk tolerance (Nifty lot = 25, BankNifty lot = 15).

---

## Dhan Market Depth (20-Level)

A second tab on the dashboard that shows real-time 20-level market depth (order book)
from Dhan's WebSocket feed. Uses the same Dhan access token — no extra API keys needed.

### What It Shows

- **20-level order book** — bids and asks with heatmap coloring and wall markers
- **Order book imbalance** — at 5, 10, and 20 levels (centered diverging bars)
- **Wall detection** — highlights abnormally large orders (3x avg)
- **Absorption tracking** — alerts when a wall is being eaten through
- **Pull detection** — alerts when a wall disappears before price reaches it
- **Cumulative delta** — chart of bid vs ask aggression over time

### Setup

The Market Depth tab is included by default. If it's not showing after an update, run the patcher:

```bash
python3 apply_depth_patch.py
bash stop.sh && bash start.sh
```

`DEPTH_ENABLED` defaults to `true`. To disable, add to `.env`:
```
DEPTH_ENABLED=false
```

### Troubleshooting

| Issue | Fix |
|-------|-----|
| Market Depth tab not showing | Check `DEPTH_ENABLED=true` in .env (default), restart |
| Data shows "--" | Market may be closed (9:15-15:30 IST), or depth WebSocket is reconnecting |
| Patcher shows warnings | Safe to ignore if the feature works. Check specific warning messages |
