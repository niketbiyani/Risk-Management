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

# Restart the service
sudo systemctl restart risk-manager
echo "Service restarted"
echo ""
echo "Verify:"
sudo systemctl status risk-manager --no-pager | head -5
