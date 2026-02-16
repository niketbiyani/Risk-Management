"""
Web Dashboard for the Trade Management Platform.
Real-time monitoring via Flask + SocketIO with auto-refresh.
Shows P&L, risk status, positions, spreads, and trade management controls.
"""

import json
import logging
import threading
import time

from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO

from config import Config

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = "risk-mgmt-dashboard"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Reference to monitor instance (set at startup)
_monitor = None


def set_monitor(monitor):
    global _monitor
    _monitor = monitor


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trade Risk Management</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: #0a0e17;
            color: #e0e6ed;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #1a1f2e 0%, #0d1117 100%);
            border-bottom: 1px solid #21262d;
            padding: 16px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { font-size: 20px; font-weight: 600; color: #f0f6fc; }
        .header .status-badge {
            padding: 6px 16px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .status-active { background: #0d4429; color: #3fb950; border: 1px solid #238636; }
        .status-locked { background: #4a1d1d; color: #f85149; border: 1px solid #da3633; animation: pulse 1.5s infinite; }
        .status-cooldown { background: #3d2e00; color: #d29922; border: 1px solid #9e6a03; }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }

        .grid { display: grid; gap: 16px; padding: 20px 24px; }
        .grid-main { grid-template-columns: repeat(4, 1fr); }
        .grid-detail { grid-template-columns: repeat(2, 1fr); }

        .card {
            background: #161b22;
            border: 1px solid #21262d;
            border-radius: 12px;
            padding: 20px;
        }
        .card h3 {
            font-size: 12px;
            text-transform: uppercase;
            color: #8b949e;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }
        .card .value {
            font-size: 28px;
            font-weight: 700;
            font-variant-numeric: tabular-nums;
        }
        .card .sub { font-size: 13px; color: #8b949e; margin-top: 4px; }

        .positive { color: #3fb950; }
        .negative { color: #f85149; }
        .neutral { color: #8b949e; }
        .warning { color: #d29922; }

        .progress-bar {
            height: 6px;
            background: #21262d;
            border-radius: 3px;
            margin-top: 12px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.5s ease, background 0.3s ease;
        }

        .lockout-banner {
            background: linear-gradient(135deg, #4a1d1d, #2d0f0f);
            border: 1px solid #da3633;
            border-radius: 12px;
            padding: 24px;
            margin: 20px 24px 0;
            text-align: center;
            display: none;
        }
        .lockout-banner.active { display: block; }
        .lockout-banner h2 { color: #f85149; font-size: 24px; margin-bottom: 8px; }
        .lockout-banner p { color: #f0f6fc; font-size: 14px; }

        .cooldown-banner {
            background: linear-gradient(135deg, #3d2e00, #2d2200);
            border: 1px solid #9e6a03;
            border-radius: 12px;
            padding: 20px;
            margin: 20px 24px 0;
            text-align: center;
            display: none;
        }
        .cooldown-banner.active { display: block; }
        .cooldown-timer {
            font-size: 48px;
            font-weight: 700;
            color: #d29922;
            font-variant-numeric: tabular-nums;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        th {
            text-align: left;
            padding: 10px 12px;
            color: #8b949e;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.5px;
            border-bottom: 1px solid #21262d;
        }
        td {
            padding: 10px 12px;
            border-bottom: 1px solid #161b22;
            font-variant-numeric: tabular-nums;
        }
        tr:hover { background: #1c2128; }

        .spread-card {
            background: #1c2128;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 12px;
        }
        .spread-type {
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            color: #58a6ff;
            margin-bottom: 8px;
        }

        .sl-tp-form {
            display: flex;
            gap: 8px;
            align-items: center;
            margin-top: 8px;
        }
        .sl-tp-form input {
            background: #0d1117;
            border: 1px solid #30363d;
            color: #e0e6ed;
            padding: 6px 10px;
            border-radius: 6px;
            width: 100px;
            font-size: 13px;
        }
        .sl-tp-form button {
            padding: 6px 14px;
            border-radius: 6px;
            border: none;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-sl { background: #da3633; color: white; }
        .btn-tp { background: #238636; color: white; }
        .btn-danger { background: #da3633; color: white; }
        .btn-sm { padding: 4px 10px; font-size: 12px; }

        .trade-log {
            max-height: 300px;
            overflow-y: auto;
        }
        .trade-entry {
            padding: 8px 12px;
            border-bottom: 1px solid #161b22;
            font-size: 13px;
            display: flex;
            justify-content: space-between;
        }

        .footer {
            text-align: center;
            padding: 16px;
            color: #484f58;
            font-size: 12px;
            border-top: 1px solid #21262d;
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>Risk Management Dashboard</h1>
        <div>
            <span id="market-time" style="color:#8b949e;font-size:13px;margin-right:16px;"></span>
            <span id="status-badge" class="status-badge status-active">ACTIVE</span>
        </div>
    </div>

    <div id="lockout-banner" class="lockout-banner">
        <h2>ACCOUNT LOCKED</h2>
        <p id="lockout-reason"></p>
        <p style="margin-top:8px;color:#8b949e;">Trading is disabled for the rest of the day. This cannot be overridden.</p>
    </div>

    <div id="cooldown-banner" class="cooldown-banner">
        <p style="color:#d29922;font-weight:600;margin-bottom:8px;">COOLDOWN ACTIVE</p>
        <div class="cooldown-timer" id="cooldown-timer">00:00</div>
        <p id="cooldown-reason" style="color:#8b949e;margin-top:8px;font-size:13px;"></p>
    </div>

    <!-- Main P&L Cards -->
    <div class="grid grid-main">
        <div class="card">
            <h3>Total P&L</h3>
            <div class="value" id="total-pnl">₹0</div>
            <div class="sub" id="pnl-breakdown">R: ₹0 | U: ₹0</div>
        </div>
        <div class="card">
            <h3>Loss Buffer</h3>
            <div class="value" id="loss-remaining">₹0</div>
            <div class="sub" id="loss-limit-info">of ₹0 limit</div>
            <div class="progress-bar">
                <div class="progress-fill" id="loss-bar" style="width:0%;background:#3fb950;"></div>
            </div>
        </div>
        <div class="card">
            <h3>Profit Lock</h3>
            <div class="value" id="profit-lock-value">INACTIVE</div>
            <div class="sub" id="profit-lock-info"></div>
        </div>
        <div class="card">
            <h3>Win Rate</h3>
            <div class="value" id="win-rate">0%</div>
            <div class="sub" id="trade-stats">0 trades</div>
        </div>
    </div>

    <!-- Risk Meters -->
    <div class="grid grid-detail">
        <div class="card">
            <h3>Trailing Drawdown</h3>
            <div style="display:flex;justify-content:space-between;align-items:baseline;">
                <div>
                    <div class="value" id="hwm-value" style="font-size:22px;">₹0</div>
                    <div class="sub">High Water Mark</div>
                </div>
                <div style="text-align:right;">
                    <div id="drawdown-value" style="font-size:22px;font-weight:700;">₹0</div>
                    <div class="sub">Current Drawdown</div>
                </div>
            </div>
            <div class="progress-bar" style="margin-top:16px;">
                <div class="progress-fill" id="drawdown-bar" style="width:0%;background:#3fb950;"></div>
            </div>
            <div class="sub" style="margin-top:4px;" id="drawdown-info"></div>
        </div>

        <div class="card">
            <h3>Trade History</h3>
            <div class="trade-log" id="trade-log">
                <div style="color:#484f58;padding:20px;text-align:center;">No trades yet</div>
            </div>
        </div>
    </div>

    <!-- Positions Table -->
    <div style="padding:0 24px;">
        <div class="card">
            <h3>Open Positions</h3>
            <table>
                <thead>
                    <tr>
                        <th>Instrument</th>
                        <th>Qty</th>
                        <th>Avg Price</th>
                        <th>LTP</th>
                        <th>P&L</th>
                        <th>SL / TP</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="positions-body">
                    <tr><td colspan="7" style="text-align:center;color:#484f58;">No open positions</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Spreads -->
    <div style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <h3>Detected Spreads</h3>
            <div id="spreads-container">
                <div style="color:#484f58;text-align:center;padding:12px;">No spreads detected</div>
            </div>
        </div>
    </div>

    <div class="footer">
        Trade Management Platform | Risk data refreshes every {{ interval }}s | State is encrypted and tamper-proof
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
    <script>
        const socket = io();

        function fmt(n) {
            if (n === null || n === undefined) return '₹0';
            const sign = n < 0 ? '-' : '';
            return sign + '₹' + Math.abs(n).toLocaleString('en-IN', {maximumFractionDigits: 0});
        }

        function fmtPct(n) {
            return (n || 0).toFixed(1) + '%';
        }

        function updateDashboard(data) {
            // Status badge
            const badge = document.getElementById('status-badge');
            if (data.lockout.active) {
                badge.className = 'status-badge status-locked';
                badge.textContent = 'LOCKED OUT';
                document.getElementById('lockout-banner').classList.add('active');
                document.getElementById('lockout-reason').textContent = data.lockout.reason;
            } else if (data.cooldown.active) {
                badge.className = 'status-badge status-cooldown';
                badge.textContent = 'COOLDOWN';
            } else {
                badge.className = 'status-badge status-active';
                badge.textContent = 'ACTIVE';
                document.getElementById('lockout-banner').classList.remove('active');
            }

            // Cooldown banner
            const cdBanner = document.getElementById('cooldown-banner');
            if (data.cooldown.active) {
                cdBanner.classList.add('active');
                const secs = data.cooldown.remaining_seconds;
                const m = Math.floor(secs / 60);
                const s = secs % 60;
                document.getElementById('cooldown-timer').textContent =
                    String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
                document.getElementById('cooldown-reason').textContent = data.cooldown.reason;
            } else {
                cdBanner.classList.remove('active');
            }

            // P&L cards
            const totalPnl = data.pnl.total;
            const totalEl = document.getElementById('total-pnl');
            totalEl.textContent = fmt(totalPnl);
            totalEl.className = 'value ' + (totalPnl >= 0 ? 'positive' : 'negative');
            document.getElementById('pnl-breakdown').textContent =
                'R: ' + fmt(data.pnl.realized) + ' | U: ' + fmt(data.pnl.unrealized);

            // Loss buffer
            const lossRemaining = data.limits.loss_remaining;
            const lossEl = document.getElementById('loss-remaining');
            lossEl.textContent = fmt(lossRemaining);
            lossEl.className = 'value ' + (lossRemaining > data.limits.daily_max_loss * 0.3 ? 'positive' : lossRemaining > 0 ? 'warning' : 'negative');
            document.getElementById('loss-limit-info').textContent = 'of ' + fmt(data.limits.daily_max_loss) + ' limit';
            const lossPct = Math.min(100, Math.max(0, data.limits.loss_used_pct));
            const lossBar = document.getElementById('loss-bar');
            lossBar.style.width = lossPct + '%';
            lossBar.style.background = lossPct > 70 ? '#f85149' : lossPct > 40 ? '#d29922' : '#3fb950';

            // Profit lock
            const plEl = document.getElementById('profit-lock-value');
            const plInfo = document.getElementById('profit-lock-info');
            if (data.profit_lock.active) {
                plEl.textContent = fmt(data.profit_lock.floor);
                plEl.className = 'value positive';
                plInfo.textContent = 'Locked floor | Buffer: ' + fmt(data.profit_lock.buffer);
            } else {
                plEl.textContent = fmt(data.profit_lock.distance);
                plEl.className = 'value neutral';
                plInfo.textContent = 'to ' + fmt(data.profit_lock.threshold) + ' lock threshold';
            }

            // Win rate
            document.getElementById('win-rate').textContent = fmtPct(data.trades.win_rate);
            document.getElementById('trade-stats').textContent =
                data.trades.total + ' trades (W:' + data.trades.winners + ' L:' + data.trades.losers + ')';

            // Trailing drawdown
            if (data.trailing_drawdown.enabled) {
                document.getElementById('hwm-value').textContent = fmt(data.trailing_drawdown.high_water_mark);
                document.getElementById('drawdown-value').textContent = fmt(data.trailing_drawdown.current_drawdown);
                const ddLimit = data.trailing_drawdown.drawdown_limit;
                const ddCurrent = data.trailing_drawdown.current_drawdown;
                const ddPct = ddLimit > 0 ? Math.min(100, (ddCurrent / ddLimit) * 100) : 0;
                const ddBar = document.getElementById('drawdown-bar');
                ddBar.style.width = ddPct + '%';
                ddBar.style.background = ddPct > 70 ? '#f85149' : ddPct > 40 ? '#d29922' : '#3fb950';
                document.getElementById('drawdown-info').textContent =
                    'Limit: ' + fmt(ddLimit) + ' | Buffer: ' + fmt(data.trailing_drawdown.buffer);
            }

            // Positions table
            updatePositions(data.positions || []);

            // Spreads
            updateSpreads(data.spreads || []);

            // Time
            document.getElementById('market-time').textContent = new Date().toLocaleTimeString('en-IN');
        }

        function updatePositions(positions) {
            const tbody = document.getElementById('positions-body');
            const open = positions.filter(p => p.netQty !== 0);
            if (open.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#484f58;">No open positions</td></tr>';
                return;
            }
            let html = '';
            for (const p of open) {
                const pnl = (p.realizedProfit || 0) + (p.unrealizedProfit || 0);
                const pnlClass = pnl >= 0 ? 'positive' : 'negative';
                const sid = p.securityId || '';
                html += '<tr>';
                html += '<td>' + (p.tradingSymbol || sid) + '</td>';
                html += '<td>' + (p.netQty || 0) + '</td>';
                html += '<td>₹' + (p.avgPrice || 0).toFixed(2) + '</td>';
                html += '<td>₹' + (p.lastTradedPrice || 0).toFixed(2) + '</td>';
                html += '<td class="' + pnlClass + '">' + fmt(pnl) + '</td>';
                html += '<td id="sltp-' + sid + '">-</td>';
                html += '<td>';
                html += '<button class="btn-sl btn-sm" onclick="promptSL(\'' + sid + '\', \'' + (p.exchangeSegment||'') + '\', \'' + (p.productType||'') + '\', ' + Math.abs(p.netQty||0) + ', ' + (p.netQty > 0 ? 1 : -1) + ')">SL</button> ';
                html += '<button class="btn-tp btn-sm" onclick="promptTP(\'' + sid + '\', \'' + (p.exchangeSegment||'') + '\', \'' + (p.productType||'') + '\', ' + Math.abs(p.netQty||0) + ', ' + (p.netQty > 0 ? 1 : -1) + ')">TP</button> ';
                html += '<button class="btn-danger btn-sm" onclick="exitPosition(\'' + sid + '\', \'' + (p.exchangeSegment||'') + '\', \'' + (p.productType||'') + '\', ' + Math.abs(p.netQty||0) + ', ' + (p.netQty > 0 ? 1 : -1) + ')">EXIT</button>';
                html += '</td>';
                html += '</tr>';
            }
            tbody.innerHTML = html;
        }

        function updateSpreads(spreads) {
            const container = document.getElementById('spreads-container');
            if (spreads.length === 0) {
                container.innerHTML = '<div style="color:#484f58;text-align:center;padding:12px;">No spreads detected</div>';
                return;
            }
            let html = '';
            for (const s of spreads) {
                const pnlClass = s.current_pnl >= 0 ? 'positive' : 'negative';
                html += '<div class="spread-card">';
                html += '<div class="spread-type">' + s.type.replace(/_/g, ' ') + '</div>';
                html += '<div style="display:flex;gap:24px;margin-bottom:8px;">';
                html += '<div><span style="color:#8b949e;">P&L:</span> <span class="' + pnlClass + '">' + fmt(s.current_pnl) + '</span></div>';
                html += '<div><span style="color:#8b949e;">Max Profit:</span> <span class="positive">' + fmt(s.max_profit) + '</span></div>';
                html += '<div><span style="color:#8b949e;">Max Loss:</span> <span class="negative">' + fmt(s.max_loss) + '</span></div>';
                html += '<div><span style="color:#8b949e;">Premium:</span> ' + fmt(s.net_premium) + '</div>';
                if (s.breakevens && s.breakevens.length > 0) {
                    html += '<div><span style="color:#8b949e;">BE:</span> ' + s.breakevens.map(b => b.toFixed(0)).join(', ') + '</div>';
                }
                html += '</div>';
                html += '<table><thead><tr><th>Type</th><th>Strike</th><th>Qty</th><th>Entry</th><th>LTP</th><th>P&L</th></tr></thead><tbody>';
                for (const leg of s.legs) {
                    const legPnlClass = leg.pnl >= 0 ? 'positive' : 'negative';
                    html += '<tr>';
                    html += '<td>' + leg.option_type + '</td>';
                    html += '<td>' + leg.strike + '</td>';
                    html += '<td>' + leg.qty + '</td>';
                    html += '<td>₹' + leg.entry.toFixed(2) + '</td>';
                    html += '<td>₹' + leg.ltp.toFixed(2) + '</td>';
                    html += '<td class="' + legPnlClass + '">' + fmt(leg.pnl) + '</td>';
                    html += '</tr>';
                }
                html += '</tbody></table></div>';
            }
            container.innerHTML = html;
        }

        function promptSL(sid, exSeg, prodType, qty, direction) {
            const price = prompt('Enter Stop Loss price:');
            if (price && !isNaN(price)) {
                const trailing = confirm('Enable trailing stop loss?');
                let trailPts = 0, trailTrigger = 0;
                if (trailing) {
                    trailPts = parseFloat(prompt('Trail by how many points?') || '0');
                    trailTrigger = parseFloat(prompt('Start trailing after profit of (points)?') || '0');
                }
                fetch('/api/sl', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({security_id: sid, price: parseFloat(price), trailing, trail_points: trailPts, trail_trigger: trailTrigger})
                }).then(r => r.json()).then(d => console.log('SL set:', d));
            }
        }

        function promptTP(sid, exSeg, prodType, qty, direction) {
            const price = prompt('Enter Take Profit price:');
            if (price && !isNaN(price)) {
                fetch('/api/tp', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({security_id: sid, price: parseFloat(price)})
                }).then(r => r.json()).then(d => console.log('TP set:', d));
            }
        }

        function exitPosition(sid, exSeg, prodType, qty, direction) {
            if (confirm('Exit this position at market?')) {
                fetch('/api/exit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({security_id: sid, exchange_segment: exSeg, product_type: prodType, quantity: qty, direction: direction})
                }).then(r => r.json()).then(d => console.log('Exit:', d));
            }
        }

        // Socket.IO real-time updates
        socket.on('status_update', function(data) {
            updateDashboard(data);
        });

        // Initial fetch
        fetch('/api/status').then(r => r.json()).then(updateDashboard);

        // Fallback polling
        setInterval(function() {
            fetch('/api/status').then(r => r.json()).then(updateDashboard);
        }, {{ interval * 1000 }});
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML, interval=Config.MONITOR_INTERVAL)


@app.route("/api/status")
def api_status():
    if _monitor:
        return jsonify(_monitor.get_status())
    return jsonify({"error": "Monitor not initialized"})


@app.route("/api/sl", methods=["POST"])
def api_set_sl():
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json
    _monitor.trade_mgr.set_stop_loss(
        security_id=data["security_id"],
        sl_price=data["price"],
        trailing=data.get("trailing", False),
        trail_points=data.get("trail_points", 0),
        trail_trigger=data.get("trail_trigger", 0),
    )
    return jsonify({"status": "ok", "message": f"SL set at {data['price']}"})


@app.route("/api/tp", methods=["POST"])
def api_set_tp():
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json
    _monitor.trade_mgr.set_take_profit(
        security_id=data["security_id"],
        tp_price=data["price"],
    )
    return jsonify({"status": "ok", "message": f"TP set at {data['price']}"})


@app.route("/api/exit", methods=["POST"])
def api_exit_position():
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json
    txn_type = "SELL" if data["direction"] == 1 else "BUY"
    try:
        result = _monitor.api.place_order(
            security_id=data["security_id"],
            exchange_segment=data["exchange_segment"],
            transaction_type=txn_type,
            quantity=data["quantity"],
            order_type="MARKET",
            product_type=data["product_type"],
            price=0,
        )
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/exit_all", methods=["POST"])
def api_exit_all():
    """Emergency exit all positions."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    try:
        cancel_results = _monitor.api.cancel_all_pending_orders()
        close_results = _monitor.api.close_all_positions()
        return jsonify({
            "status": "ok",
            "cancelled_orders": len(cancel_results),
            "closed_positions": len(close_results),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/projections", methods=["POST"])
def api_projections():
    """Calculate projected P&L for a position at different underlying prices."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json
    positions = _monitor._last_positions
    target_pos = None
    for p in positions:
        if str(p.get("securityId", "")) == str(data.get("security_id", "")):
            target_pos = p
            break
    if not target_pos:
        return jsonify({"error": "Position not found"}), 404

    target_prices = data.get("target_prices", [])
    projections = _monitor.trade_mgr.calculate_projections(target_pos, target_prices)
    return jsonify({"projections": projections})


def emit_status_update(status_data: dict):
    """Push status update to all connected dashboard clients."""
    socketio.emit("status_update", status_data)


def run_dashboard(monitor):
    """Start the dashboard web server."""
    set_monitor(monitor)
    socketio.run(app, host=Config.DASHBOARD_HOST, port=Config.DASHBOARD_PORT,
                 debug=False, allow_unsafe_werkzeug=True)
