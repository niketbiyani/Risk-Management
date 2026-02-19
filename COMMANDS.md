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

| Setting                    | What it does                          | Default |
|----------------------------|---------------------------------------|---------|
| DAILY_MAX_LOSS             | Max loss before trading stops (INR)   | 5000    |
| DAILY_PROFIT_TARGET        | Profit target for the day (INR)       | 20000   |
| MAX_OPEN_POSITIONS         | Max concurrent positions              | 5       |
| MAX_SINGLE_TRADE_RISK      | Max risk per trade (INR)              | 2000    |
| MAX_ORDER_QUANTITY         | Max lots per order                    | 1800    |

After changing settings, restart: `sudo systemctl restart risk-manager`

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| DH-901 (token expired) | Should auto-fix on next restart. Manual: `python3 token_manager.py` |
| DH-906 (market closed) | Normal outside 9:15 AM - 3:30 PM IST |
| DH-905 (insufficient margin) | Check fund balance on Dhan |
| Dashboard not loading | `sudo systemctl status risk-manager` to check |
| Service won't start | Check logs: `tail -50 platform.log` |
| Token auto-refresh failing | Verify DHAN_PIN and DHAN_TOTP_SECRET in .env |

---

## Quick Status Check

```bash
# Platform status
python3 main.py --status

# Service status
sudo systemctl status risk-manager

# Is it running?
pgrep -f "python.*main.py" && echo "RUNNING" || echo "NOT RUNNING"
```
