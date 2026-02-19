# Risk Management Platform - Quick Reference

All commands assume you're in `/home/user/Risk-Management`.

---

## Daily Startup (Before 9:15 AM IST)

```bash
# 1. Activate virtual environment
cd /home/user/Risk-Management
source venv/bin/activate

# 2. Generate a new token at https://api.dhan.co
#    Copy the token, then run:
./update_token.sh PASTE_YOUR_NEW_TOKEN_HERE

# 3. Start the dashboard
python3 dashboard.py &

# 4. Verify it's running
curl -s http://localhost:5555/api/status | python3 -m json.tool
```

Dashboard will be at: `http://YOUR_SERVER_IP:5555`

---

## Update API Token (without restarting)

```bash
# Option A: Use the helper script
./update_token.sh PASTE_YOUR_NEW_TOKEN_HERE

# Option B: Edit manually
nano .env
# Change the DHAN_ACCESS_TOKEN=... line, save (Ctrl+O, Enter, Ctrl+X)
# Then restart (see below)
```

---

## Start / Stop / Restart Dashboard

```bash
# Start
python3 dashboard.py &

# Check if running
ps aux | grep dashboard.py

# Graceful stop
pkill -f dashboard.py

# Force kill (if graceful stop doesn't work)
pkill -9 -f dashboard.py

# Restart (stop + start)
pkill -f dashboard.py; sleep 1; python3 dashboard.py &
```

---

## View Logs

```bash
# Live logs (follow mode, Ctrl+C to stop watching)
tail -f platform.log

# Last 50 lines
tail -50 platform.log

# Search for errors
grep -i error platform.log | tail -20

# Search for order activity
grep -i "place_order\|order_id\|DH-" platform.log | tail -20
```

---

## Check Token Status

```bash
# Quick test: does the API respond?
python3 -c "
from dotenv import load_dotenv; import os; load_dotenv()
from dhanhq import dhanhq
dhan = dhanhq(os.getenv('DHAN_CLIENT_ID'), os.getenv('DHAN_ACCESS_TOKEN'))
r = dhan.get_fund_limits()
if r['status'] == 'success':
    print('Token OK  |  Balance:', r['data']['availabelBalance'])
else:
    print('Token FAILED:', r)
"
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

After changing settings, restart the dashboard for them to take effect.

---

## Install as System Service (run once, optional)

This makes the platform auto-start on boot and auto-restart on crash:

```bash
sudo bash install_service.sh
```

After installing, use these instead of the manual commands above:

```bash
sudo systemctl start risk-manager      # Start
sudo systemctl stop risk-manager       # Stop
sudo systemctl restart risk-manager    # Restart
sudo systemctl status risk-manager     # Check status
journalctl -u risk-manager -f          # Live logs
```

And to update the token daily:
```bash
./update_token.sh PASTE_YOUR_NEW_TOKEN_HERE
# (this updates .env AND restarts the service automatically)
```

---

## Troubleshooting

| Error Code | Meaning                          | Fix                                      |
|------------|----------------------------------|------------------------------------------|
| DH-901     | Token expired or invalid         | Generate new token at https://api.dhan.co |
| DH-906     | Market is closed                 | Wait for market hours (9:15 AM-3:30 PM)  |
| DH-905     | Insufficient margin              | Check fund balance                        |
| Connection | Dashboard not reachable          | Check if running: `ps aux \| grep dashboard` |

---

## Typical Daily Workflow

1. **8:30 AM** - Generate new API token at https://api.dhan.co
2. **8:30 AM** - Run `./update_token.sh YOUR_TOKEN`
3. **8:30 AM** - Start dashboard: `python3 dashboard.py &`
4. **9:15 AM** - Market opens, start trading via dashboard
5. **3:30 PM** - Market closes
6. **Evening** - Check logs: `tail -50 platform.log`
