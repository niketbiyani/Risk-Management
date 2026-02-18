#!/bin/bash
# Install the Risk Management Platform as a systemd service.
# Run this script once: sudo bash install_service.sh
#
# After installation:
#   sudo systemctl start risk-manager     # Start the platform
#   sudo systemctl stop risk-manager      # Stop the platform
#   sudo systemctl restart risk-manager   # Restart
#   sudo systemctl status risk-manager    # Check status
#   journalctl -u risk-manager -f         # View live logs
#
# The service will auto-start on boot and restart on crash.

set -e

WORK_DIR="/home/user/Risk-Management"
VENV_DIR="${WORK_DIR}/venv"

# Ensure venv exists
if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install -r "${WORK_DIR}/requirements.txt"
fi

# Create systemd service file
cat > /etc/systemd/system/risk-manager.service << 'EOF'
[Unit]
Description=Trade Risk Management Platform
After=network.target

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

# Reload systemd and enable the service
systemctl daemon-reload
systemctl enable risk-manager

echo ""
echo "Service installed successfully!"
echo ""
echo "Commands:"
echo "  sudo systemctl start risk-manager     - Start the platform"
echo "  sudo systemctl stop risk-manager      - Stop the platform"
echo "  sudo systemctl restart risk-manager   - Restart"
echo "  sudo systemctl status risk-manager    - Check status"
echo "  journalctl -u risk-manager -f         - View live logs"
echo ""
echo "The platform will auto-start on boot and restart if it crashes."
echo "Dashboard will be at http://localhost:5555"
