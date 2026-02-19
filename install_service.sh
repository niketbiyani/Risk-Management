#!/bin/bash
# Install the Risk Management Platform as a systemd service with auto token refresh.
# Run this script once: sudo bash install_service.sh
#
# After installation:
#   sudo systemctl start risk-manager     # Start the platform
#   sudo systemctl stop risk-manager      # Stop the platform
#   sudo systemctl restart risk-manager   # Restart
#   sudo systemctl status risk-manager    # Check status
#   journalctl -u risk-manager -f         # View live logs
#
# The service will:
#   - Auto-start on boot
#   - Auto-restart on crash (after 10s)
#   - Auto-refresh the API token on every startup (via PIN + TOTP)
#   - Renew the token every 12 hours while running
#   - Restart itself daily at 8:45 AM IST (fresh token for market open)

set -e

WORK_DIR="/home/user/Risk-Management"
VENV_DIR="${WORK_DIR}/venv"
PYTHON="${VENV_DIR}/bin/python"

# Ensure venv exists
if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "${VENV_DIR}"
fi

# Install/upgrade dependencies
echo "Installing dependencies..."
"${VENV_DIR}/bin/pip" install -r "${WORK_DIR}/requirements.txt" --quiet

# ── Main Platform Service ──────────────────────────────────────────────

cat > /etc/systemd/system/risk-manager.service << 'EOF'
[Unit]
Description=Trade Risk Management Platform
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=user
WorkingDirectory=/home/user/Risk-Management
ExecStart=/home/user/Risk-Management/venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/user/Risk-Management/platform.log
StandardError=append:/home/user/Risk-Management/platform.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# ── Daily Pre-Market Restart Timer ─────────────────────────────────────
# Restarts the service at 8:45 AM IST daily so it gets a fresh token
# well before market opens at 9:15 AM.

cat > /etc/systemd/system/risk-manager-restart.service << 'EOF'
[Unit]
Description=Restart Risk Manager for fresh token before market open

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart risk-manager
EOF

cat > /etc/systemd/system/risk-manager-restart.timer << 'EOF'
[Unit]
Description=Daily pre-market restart of Risk Manager (8:45 AM IST)

[Timer]
OnCalendar=*-*-* 08:45:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── Reload and Enable ──────────────────────────────────────────────────

systemctl daemon-reload
systemctl enable risk-manager
systemctl enable risk-manager-restart.timer

echo ""
echo "============================================"
echo "  Installation Complete!"
echo "============================================"
echo ""
echo "Services installed:"
echo "  risk-manager               - Main platform (auto-start on boot)"
echo "  risk-manager-restart.timer - Daily 8:45 AM restart (fresh token)"
echo ""
echo "Commands:"
echo "  sudo systemctl start risk-manager     - Start the platform"
echo "  sudo systemctl stop risk-manager      - Stop the platform"
echo "  sudo systemctl restart risk-manager   - Restart"
echo "  sudo systemctl status risk-manager    - Check status"
echo "  journalctl -u risk-manager -f         - View live logs"
echo ""
echo "IMPORTANT: For zero-touch operation, set these in .env:"
echo "  DHAN_PIN=your_6_digit_pin"
echo "  DHAN_TOTP_SECRET=your_totp_secret_from_dhan"
echo ""
echo "Dashboard: http://localhost:5555"
echo ""
