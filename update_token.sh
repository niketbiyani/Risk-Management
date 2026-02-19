#!/bin/bash
# Quick script to update Dhan access token
# Usage: ./update_token.sh YOUR_NEW_TOKEN

if [ -z "$1" ]; then
    echo "Usage: ./update_token.sh YOUR_NEW_ACCESS_TOKEN"
    echo ""
    echo "Get your token from https://web.dhan.co (API section)"
    exit 1
fi

NEW_TOKEN="$1"
ENV_FILE="$(dirname "$0")/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: .env file not found at $ENV_FILE"
    exit 1
fi

# Replace the token line
sed -i "s|^DHAN_ACCESS_TOKEN=.*|DHAN_ACCESS_TOKEN=$NEW_TOKEN|" "$ENV_FILE"
echo "Token updated in .env"

# Restart the service if it's running under systemd
if systemctl is-active --quiet risk-manager 2>/dev/null; then
    sudo systemctl restart risk-manager
    echo "Service restarted"
    echo ""
    echo "Verify:"
    sudo systemctl status risk-manager --no-pager | head -5
else
    # Check if dashboard is running manually
    if pgrep -f "python.*main.py\|python.*dashboard.py" > /dev/null; then
        echo ""
        echo "NOTE: Platform is running manually. Restart it to use the new token:"
        echo "  pkill -f 'python.*main.py'; sleep 1; python3 main.py &"
    else
        echo "Token saved. Start the platform with: python3 main.py"
    fi
fi
