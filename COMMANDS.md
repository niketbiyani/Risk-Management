# Risk Management Platform - Quick Reference

All commands assume you're in `/home/user/Risk-Management`.

---

## One-Time Setup (Zero-Touch Mode)

Set this up once and the platform runs itself forever — auto-starts on boot,
auto-refreshes API tokens, auto-restarts on crash. No daily manual intervention.

### Step 1: Set up TOTP on your Dhan account

1. Go to https://web.dhan.co -> **Profile** -> **DhanHQ Trading APIs**
2. Click **Setup TOTP**
3. Scan the QR code with your authenticator app (Google Authenticator, etc.)
4. **Copy the TOTP secret key** (the base32 string, looks like `JBSWY3DPEHPK3PXP`)

### Step 2: Add your credentials to .env

```bash
nano .env
```

Add these two lines:

```
DHAN_PIN=123456                          # Your 6-digit Dhan login PIN
DHAN_TOTP_SECRET=JBSWY3DPEHPK3PXP       # The TOTP secret from Step 1
```

### Step 3: Install the service

```bash
sudo bash install_service.sh
```

### Step 4: Start it

```bash
sudo systemctl start risk-manager
```

**That's it.** The platform will now:
- Auto-start on every server boot
- Auto-refresh the API token on every startup (via PIN + TOTP)
- Renew the token every 12 hours while running
- Restart at 8:45 AM IST daily (fresh token before market open)
- Auto-restart within 10 seconds if it crashes

Dashboard: `http://YOUR_SERVER_IP:5555`

---

## Service Commands (after one-time setup)

```bash
sudo systemctl start risk-manager      # Start
sudo systemctl stop risk-manager       # Stop
sudo systemctl restart risk-manager    # Restart
sudo systemctl status risk-manager     # Check status
journalctl -u risk-manager -f          # Live logs
```

---

## Manual Mode (without service)

If you prefer to run manually without the systemd service:

```bash
cd /home/user/Risk-Management
source venv/bin/activate

# If DHAN_PIN + DHAN_TOTP_SECRET are set, token refreshes automatically:
python3 main.py &

# If not set, update token manually first:
./update_token.sh PASTE_YOUR_NEW_TOKEN_HERE
python3 main.py &

# Verify
curl -s http://localhost:5555/api/status | python3 -m json.tool
```

---

## Token Management

### Auto mode (recommended)

With `DHAN_PIN` and `DHAN_TOTP_SECRET` in `.env`, tokens are refreshed
automatically. You never need to touch the token again.

### Manual mode (if auto not configured)

```bash
# Generate a new token at https://web.dhan.co (API section)
./update_token.sh PASTE_YOUR_NEW_TOKEN_HERE
```

### Force refresh token manually

```bash
python3 token_manager.py
```

### Test if current token works

```bash
python3 -c "
from config import Config
from dhan_api import DhanAPI
api = DhanAPI()
r = api.get_fund_limits()
if r.get('status') == 'success':
    print('Token OK  |  Balance:', r['data'].get('availabelBalance'))
else:
    print('Token FAILED:', r)
"
```

---

## Start / Stop / Force Kill

```bash
# -- With systemd service --
sudo systemctl start risk-manager
sudo systemctl stop risk-manager
sudo systemctl restart risk-manager

# -- Manual mode --
python3 main.py &                                          # Start
pkill -f "python.*main.py"                                 # Stop
pkill -9 -f "python.*main.py"                              # Force kill
pkill -f "python.*main.py"; sleep 1; python3 main.py &     # Restart
```

---

## View Logs

```bash
# Live logs (Ctrl+C to stop watching)
tail -f platform.log

# Last 50 lines
tail -50 platform.log

# Search for errors
grep -i error platform.log | tail -20

# Search for token refresh activity
grep -i "token\|refresh\|renew" platform.log | tail -20

# Search for order activity
grep -i "place_order\|order_id\|DH-" platform.log | tail -20
```

---

## Risk Settings (edit .env)

```bash
nano .env
```

| Setting                    | What it does                                                    | Default |
|----------------------------|-----------------------------------------------------------------|---------|
| `DAILY_MAX_LOSS`           | Maximum loss allowed before trading is locked out (INR)          | 3500    |
| `DAILY_PROFIT_TARGET`      | Daily profit target for optional auto-lockout (INR)              | 20000   |
| `MAX_OPEN_POSITIONS`       | Maximum number of simultaneous open positions                   | 5       |
| `MAX_SINGLE_TRADE_RISK`    | Maximum estimated risk per trade (INR)                           | 1200    |
| `MAX_ORDER_QUANTITY`       | Maximum quantity allowed per order                              | 1800    |
| `MAX_DAILY_TRADES`         | Maximum number of unique filled/partially-filled orders         | 35      |
| `PROFIT_LOCK_THRESHOLD`    | Realized P&L level required to trigger trailing Profit Lock (INR)| 3000    |
| `PROFIT_LOCK_PERCENTAGE`   | Percentage of peak realized profit locked in (e.g. 50%)         | 50      |
| `TRAILING_DRAWDOWN_ENABLED`| Enable trailing drawdown protection                             | true    |
| `TRAILING_DRAWDOWN_PCT`    | Drawdown allowance (percent) from peak realized HWM              | 50      |

*After changing settings, restart the service to apply: `sudo systemctl restart risk-manager`*
*Note: Mid-day updates to `MAX_DAILY_TRADES` are automatically synchronized on restart without resetting your active daily progress.*

---

## Dynamic Trade Limit Extensions

The platform allows you to dynamically extend your daily trade count limit by **+10** during the day if you are close to lockout or already locked out.

*   **How to trigger**: Click the `+10 Trades` button on the main dashboard Win Rate card or on the chart-trading status bar.
*   **What it does**: Increments your daily maximum trades limit by `10` (e.g., from `50` to `60`) in memory and automatically clears any active lockout caused by the trade limit rule, resuming trading instantly.

---

## Resetting Lockout States (Manual Override)

If you get locked out (e.g., during testing, false alarms, or deliberate risk overrides), you can manually reset the lockout flags to active status using one of two methods:

### Method A: Offline Reset (Recommended when the service is stopped)
If the service is stopped, run this script to reset the lockout flags directly inside today's encrypted file on disk. This prevents the service from liquidating your positions on startup:
```bash
# 1. Stop the manager
sudo systemctl stop risk-manager

# 2. Reset lockout flag on disk
./venv/bin/python reset_lockout.py

# 3. Start the manager
sudo systemctl start risk-manager
```

### Method B: Online Reset (While the service is running)
If the service is running and you want to clear a lockout live on the fly, send a secure POST request to the administrative endpoint:
```bash
curl -X POST http://localhost:5555/api/admin/reset_lockout
```

---

## How Profit Lock & Trailing Drawdown Calculate P&L

All risk rules are calculated strictly using **Net Realized P&L** and **Net Total P&L** (after subtracting unique order execution charges of ₹20 per order). This ensures your triggers reflect your actual cash balance:

$$\text{Net Realized P\&L} = \text{Gross Realized P\&L} - (\text{Unique Filled Orders} \times \text{Brokerage Fee})$$

1.  **Profit Lock (Realized Protection)**:
    *   Triggers when **Net Realized P&L** reaches `PROFIT_LOCK_THRESHOLD` (e.g. ₹3,000).
    *   Locks in `PROFIT_LOCK_PERCENTAGE` of peak (e.g. at ₹3,000 peak, it locks in ₹1,500).
    *   Ratchets up as peak net profit increases. Locks out trading if net realized P&L falls below this floor.
2.  **Trailing Drawdown (Open Protection)**:
    *   Triggers only *after* HWM reaches the profit lock threshold.
    *   Drawdown measures how far your **Total Net P&L (realized + open unrealized - charges)** falls from your peak Net HWM.
    *   Locks out trading *immediately while positions are still open* if you draw down more than the percentage allowance.

---

## Quick Status Diagnostics

You can inspect the active risk management parameters, daily P&L balances, trailing drawdown levels, and lockout flags currently written to disk without starting the web server.

Run the status utility:
```bash
./venv/bin/python main.py --status
```

Verify service daemon state:
```bash
# General status
sudo systemctl status risk-manager

# Is it running?
pgrep -f "python.*main.py" && echo "RUNNING" || echo "NOT RUNNING"
```
