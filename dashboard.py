"""
Web Dashboard for the Trade Management Platform.
Real-time monitoring via Flask + SocketIO with auto-refresh.
Shows P&L, risk status, positions, spreads, order placement, and trade management controls.
"""

import json
import logging
import threading
import time
from datetime import date, datetime, timezone, timedelta

from flask import Flask, render_template_string, jsonify, request, redirect
from flask_socketio import SocketIO

from config import Config
from broker_api import DepthWebSocket

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = "risk-mgmt-dashboard"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

@app.before_request
def handle_options_preflight():
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        return response

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# Reference to monitor instance and instrument cache (set at startup)
_monitor = None
_instrument_cache = None

# Depth of Market state
_depth_ws = None
_depth_timer_running = False
_depth_timer_lock = threading.Lock()  # Prevents multiple emit loops
_depth_cred_version = 0  # Tracks credential version to detect token refresh
_depth_subscribe_gen = 0  # Incremented on each new subscription to reset no-data state


def set_monitor(monitor):
    global _monitor, _depth_ws
    _monitor = monitor
    _start_bse_spot_updater()
    # Create depth WS now so OC LTP subscribe works even before DOM is opened
    if _depth_ws is None:
        from broker_api import DepthWebSocket
        token = monitor.api._context.get_access_token()
        client_id = monitor.api._context.get_client_id()
        _depth_ws = DepthWebSocket(token, client_id)


def set_instrument_cache(cache):
    global _instrument_cache
    _instrument_cache = cache


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <script>
        if (window.location.pathname.indexOf('chart-trading') >= 0) {
            document.documentElement.classList.add('chart-trading-mode');
            window.addEventListener('DOMContentLoaded', function() {
                if (typeof switchDomTab === 'function') {
                    switchDomTab('chart');
                }
            });
        }
    </script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Trade Risk Management</title>
    <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
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
        .btn-buy { background: #238636; color: white; }
        .btn-sell { background: #da3633; color: white; }
        .btn-danger { background: #da3633; color: white; }
        .btn-neutral { background: #30363d; color: #e0e6ed; }
        .btn-sm { padding: 4px 10px; font-size: 12px; }
        .btn-xs { padding: 2px 8px; font-size: 11px; border-radius: 4px; }
        .oc-s-btn { color:#f85149; border:1px solid #f85149; border-radius:3px; padding:1px 4px; font-size:10px; font-weight:700; cursor:pointer; margin-right:2px; background:#1a0808; line-height:1.4; transition:background 0.15s; }
        .oc-b-btn { color:#3fb950; border:1px solid #3fb950; border-radius:3px; padding:1px 4px; font-size:10px; font-weight:700; cursor:pointer; margin-right:3px; background:#081a08; line-height:1.4; transition:background 0.15s; }
        .oc-s-btn:hover, .oc-s-active { background:#6b1a1a !important; }
        .oc-b-btn:hover, .oc-b-active { background:#1a4d1a !important; }
        .oc-leg-sell-selected { background:#2d1117 !important; }
        .oc-leg-buy-selected  { background:#0d2117 !important; }

        /* ── Chart Trading Fullscreen Overrides ────────────────── */
        .chart-trading-mode .header { display: none !important; }
        .chart-trading-mode .grid { display: none !important; }
        .chart-trading-mode .desktop-only { display: none !important; }
        .chart-trading-mode #trading-desktop-container { display: block !important; }
        .chart-trading-mode #oc-scroll-container {
            max-height: none !important;
            height: calc(100vh - 150px) !important;
        }
        .chart-trading-mode #dom-chart-canvas {
            height: calc(100vh - 230px) !important;
        }
        .chart-trading-mode body {
            padding: 8px 0 !important;
        }
        .chart-trading-mode #dom-tab-depth { display: none !important; }
        .chart-trading-mode #dom-tab-chart { display: none !important; }
        .chart-trading-mode #dom-chart { display: none !important; }
        .chart-trading-mode #dom-analysis { display: none !important; }
        .chart-trading-mode #dom-title-text { display: none !important; }
        .chart-trading-mode #chart-title-text { display: block !important; }
        .chart-trading-mode #chart-trading-status-bar { display: flex !important; }

        /* ── Mobile responsive ───────────────────────────────────── */
        @media (max-width: 768px) {
            body { font-size: 13px; }
            .desktop-only { display: none !important; }
            .page-header { padding: 10px 12px; }
            .page-header h1 { font-size: 18px; }
            .grid { grid-template-columns: 1fr !important; }
            .grid-detail { grid-template-columns: 1fr !important; }
            .main-content { padding: 0 8px !important; }
            .card { padding: 12px !important; }
            table { font-size: 11px; }
            th, td { padding: 5px 6px !important; }
            .btn-sell, .btn-buy, .btn-neutral { padding: 8px 12px; font-size: 12px; }
            h3 { font-size: 14px !important; }
        }

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

        /* Order Tabs */
        .order-tab { transition: all 0.2s; }
        .order-tab:hover { color: #e0e6ed !important; }

        .form-input {
            background: #0d1117;
            border: 1px solid #30363d;
            color: #e0e6ed;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 13px;
            width: 100%;
        }
        .form-input:focus { border-color: #58a6ff; outline: none; }
        .form-input::placeholder { color: #484f58; }
        .form-select {
            background: #0d1117;
            border: 1px solid #30363d;
            color: #e0e6ed;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 13px;
            width: 100%;
            cursor: pointer;
        }
        .form-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }

        /* Instrument Search Dropdown */
        .search-wrapper { position: relative; }
        .search-results {
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 0 0 8px 8px;
            max-height: 300px;
            overflow-y: auto;
            z-index: 100;
            display: none;
        }
        .search-item {
            padding: 10px 12px;
            cursor: pointer;
            border-bottom: 1px solid #21262d;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .search-item:hover { background: #1c2128; }
        .search-item .sym { font-weight: 600; font-size: 13px; }
        .search-item .meta { color: #8b949e; font-size: 11px; }

        /* Calc Results */
        .calc-results {
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 16px;
            margin-top: 12px;
            display: none;
        }
        .calc-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
        .calc-item .label { font-size: 11px; color: #8b949e; text-transform: uppercase; }
        .calc-item .val { font-size: 18px; font-weight: 700; margin-top: 2px; font-variant-numeric: tabular-nums; }

        /* Quick SL/TP buttons */
        .quick-btns { display: flex; gap: 4px; flex-wrap: wrap; }
        .quick-btns button {
            padding: 2px 8px;
            font-size: 11px;
            border-radius: 4px;
            border: none;
            cursor: pointer;
            font-weight: 600;
            white-space: nowrap;
        }

        /* Inline SL/TP form row */
        .inline-sltp {
            background: #1c2128;
            padding: 12px;
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
        }
        .inline-sltp input {
            background: #0d1117;
            border: 1px solid #30363d;
            color: #e0e6ed;
            padding: 4px 8px;
            border-radius: 4px;
            width: 90px;
            font-size: 12px;
        }

        /* Toast notification */
        .toast-container {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1000;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .toast {
            padding: 12px 20px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            animation: slideIn 0.3s ease, fadeOut 0.3s ease 3.7s;
            max-width: 350px;
        }
        .toast-success { background: #0d4429; border: 1px solid #238636; color: #3fb950; }
        .toast-error { background: #4a1d1d; border: 1px solid #da3633; color: #f85149; }
        .toast-warning { background: #3d2e00; border: 1px solid #9e6a03; color: #d29922; }
        .toast-info { background: #0c2d6b; border: 1px solid #1f6feb; color: #58a6ff; }
        @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
        @keyframes fadeOut { from { opacity: 1; } to { opacity: 0; } }

        .footer {
            text-align: center;
            padding: 16px;
            color: #484f58;
            font-size: 12px;
            border-top: 1px solid #21262d;
            margin-top: 20px;
        }
    </style>
    <script>
        // Global JS error handler - shows errors on page for debugging
        window._jsErrors = [];
        window.onerror = function(msg, src, line, col, err) {
            window._jsErrors.push(msg + ' (line ' + line + ')');
            var banner = document.getElementById('js-error-banner');
            if (banner) {
                banner.style.display = 'block';
                banner.querySelector('pre').textContent = window._jsErrors.join('\\n');
            }
            return false;
        };

        // Tab switching defined early so tabs always work even if main script has errors
        function switchOrderTab(tab) {
            var naked = document.getElementById('tab-naked');
            var spread = document.getElementById('tab-spread');
            if (naked) naked.style.display = tab === 'naked' ? '' : 'none';
            if (spread) spread.style.display = tab === 'spread' ? '' : 'none';
            document.querySelectorAll('.order-tab').forEach(function(t) {
                var isActive = t.getAttribute('data-tab') === tab;
                t.style.borderBottomColor = isActive ? '#58a6ff' : 'transparent';
                t.style.color = isActive ? '#58a6ff' : '#8b949e';
            });
        }
    </script>
</head>
<body>
    <!-- JS Error Banner (hidden unless errors occur) -->
    <div id="js-error-banner" style="display:none;background:#4a1d1d;border:1px solid #da3633;color:#f85149;padding:12px 24px;font-size:13px;">
        <strong>JavaScript Error:</strong> <pre style="margin:4px 0 0;white-space:pre-wrap;color:#e0e6ed;font-size:12px;"></pre>
    </div>
    <div class="header">
        <h1>Risk Management Dashboard</h1>
        <div style="display:flex;align-items:center;gap:16px;">
            <span id="market-time" style="color:#8b949e;font-size:13px;"></span>
            <div style="display:flex;align-items:center;gap:8px;">
                <button id="mute-btn" onclick="toggleMute()" style="background:none;border:1px solid #30363d;color:#8b949e;padding:4px 8px;border-radius:6px;cursor:pointer;font-size:14px;">&#x1f50a;</button>
                <input type="range" id="volume-slider" min="0" max="100" value="30" style="-webkit-appearance:none;width:70px;height:4px;background:#30363d;border-radius:2px;outline:none;cursor:pointer;">
            </div>
            <a href="/journal" target="_blank" style="font-size:12px;padding:4px 10px;border-radius:6px;border:1px solid #30363d;color:#8b949e;text-decoration:none;cursor:pointer;" title="Open Trade Journal">&#x1F4D3; Journal</a>
            <a href="/analytics" target="_blank" style="font-size:12px;padding:4px 10px;border-radius:6px;border:1px solid #30363d;color:#8b949e;text-decoration:none;cursor:pointer;" title="Analytics">&#x1F4CA; Analytics</a>
            <a href="/straddle" target="_blank" style="font-size:12px;padding:4px 10px;border-radius:6px;border:1px solid #30363d;color:#8b949e;text-decoration:none;cursor:pointer;" title="Strangle Chart">&#x1F4C8; Strangle</a>
            <button onclick="openAdminModal()" style="font-size:12px;padding:4px 10px;border-radius:6px;border:1px solid #30363d;background:none;color:#8b949e;cursor:pointer;font-family:inherit;" title="VPS Admin Commands">&#x2699;&#xfe0f; VPS Admin</button>
            <!-- Broker Toggle Switch -->
            <div class="broker-toggle" style="display:flex;align-items:center;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:2px;gap:2px;">
                <button id="broker-dhan" onclick="toggleBroker('DHAN')" style="background:#21262d;border:none;color:#c9d1d9;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600;transition:all 0.2s;">Dhan</button>
                <button id="broker-kotak" onclick="toggleBroker('KOTAK')" style="background:none;border:none;color:#8b949e;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600;transition:all 0.2s;">Kotak Neo</button>
            </div>
            <span id="token-status" style="font-size:12px;padding:4px 10px;border-radius:12px;cursor:pointer;border:1px solid #30363d;color:#8b949e;" onclick="refreshToken()" title="Click to refresh token">API: ...</span>
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

    <!-- Toast container -->
    <div class="toast-container" id="toast-container"></div>

    <!-- Main P&L Cards -->
    <div class="grid grid-main">
        <div class="card">
            <h3>Total P&L</h3>
            <div class="value" id="total-pnl">&#8377;0</div>
            <div class="sub" id="pnl-breakdown">R: &#8377;0 | U: &#8377;0</div>
        </div>
        <div class="card">
            <h3>Loss Buffer</h3>
            <div class="value positive" id="loss-remaining">{{ loss_limit_fmt }}</div>
            <div class="sub" id="loss-limit-info">of {{ loss_limit_fmt }} limit</div>
            <div class="progress-bar">
                <div class="progress-fill" id="loss-bar" style="width:0%;background:#3fb950;"></div>
            </div>
        </div>
        <div class="card">
            <h3>Profit Lock</h3>
            <div class="value neutral" id="profit-lock-value">{{ profit_lock_distance_fmt }}</div>
            <div class="sub" id="profit-lock-info">to {{ profit_lock_threshold_fmt }} lock threshold</div>
        </div>
        <div class="card">
            <h3>Win Rate</h3>
            <div class="value" id="win-rate">0%</div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
                <div class="sub" id="trade-stats" style="margin:0;">0 trades</div>
                <button id="main-extend-btn" onclick="extendTradeLimit()" class="btn-xs btn-neutral" style="padding:2px 6px;font-size:10px;font-weight:700;background:#21262d;border-color:#30363d;color:#e6edf3;border-radius:4px;cursor:pointer;line-height:1.2;">+10 Trades</button>
            </div>
        </div>
    </div>

    <!-- Risk Meters -->
    <div class="grid grid-detail">
        <div class="card" id="dd-card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <h3 style="margin:0;">Trailing Drawdown</h3>
                <span id="dd-badge" style="font-size:10px;padding:2px 8px;border-radius:10px;background:#21262d;color:#484f58;">&#9679; Inactive</span>
            </div>
            <!-- Inactive state -->
            <div id="dd-inactive">
                <div style="color:#8b949e;font-size:12px;">Activates when realized &ge; &#8377;10,000</div>
                <div style="margin-top:6px;">
                    <span style="font-size:11px;color:#484f58;">Need </span>
                    <span id="dd-need" style="font-size:13px;font-weight:700;color:#8b949e;">&#8377;--</span>
                    <span style="font-size:11px;color:#484f58;"> more realized</span>
                </div>
            </div>
            <!-- Active state -->
            <div id="dd-active" style="display:none;">
                <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
                    <div>
                        <div style="font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;">Gap to Lockout</div>
                        <div id="dd-gap" style="font-size:26px;font-weight:700;color:#3fb950;">&#8377;--</div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:10px;color:#8b949e;">Floor</div>
                        <div id="dd-floor" style="font-size:14px;font-weight:700;color:#f85149;">&#8377;--</div>
                        <div style="font-size:10px;color:#484f58;">HWM <span id="dd-hwm">&#8377;--</span></div>
                    </div>
                </div>
                <div class="progress-bar" style="margin-bottom:4px;">
                    <div class="progress-fill" id="drawdown-bar" style="width:0%;background:#3fb950;"></div>
                </div>
                <div style="display:flex;justify-content:space-between;font-size:9px;color:#484f58;">
                    <span id="dd-floor-label">&#9650; Floor</span>
                    <span id="dd-hwm-label">HWM &#9650;</span>
                </div>
            </div>
        </div>

        <div class="card" id="equity-curve-card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <h3 style="margin:0;">Equity Curve</h3>
                <div style="display:flex;gap:10px;align-items:center;">
                    <span style="font-size:9px;color:#484f58;">scroll=zoom · drag=pan · dblclick=reset</span>
                    <span id="eq-summary" style="font-size:11px;color:#8b949e;"></span>
                </div>
            </div>
            <div style="position:relative;height:180px;width:100%;">
                <canvas id="equity-chart"></canvas>
                <div id="eq-empty" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#484f58;font-size:12px;">No closed trades today</div>
            </div>
        </div>
    </div>

    <!-- Option Chain + Depth of Market (side by side) -->
    <div id="trading-desktop-container" class="desktop-only" style="padding:0 24px;margin-top:16px;">
        <!-- Dedicated status bar for chart-trading page -->
        <div id="chart-trading-status-bar" style="display:none;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px 16px;margin-bottom:12px;align-items:center;justify-content:space-between;gap:16px;">
            <div style="display:flex;align-items:center;gap:16px;">
                <span style="font-size:12px;color:#8b949e;">Net P&L:</span>
                <strong id="ct-net-pnl" style="font-size:15px;font-weight:700;">&#8377;0</strong>
                <span id="ct-pnl-breakdown" style="font-size:11px;color:#8b949e;">R: &#8377;0 | U: &#8377;0 | Charges: &#8377;0</span>
            </div>
            <div style="display:flex;align-items:center;gap:16px;font-size:12px;color:#8b949e;">
                <div>Loss Buffer: <strong id="ct-loss-remaining" style="color:#e6edf3;">&#8377;0</strong></div>
                <div style="width:1px;height:12px;background:#30363d;"></div>
                <div>Profit Lock: <strong id="ct-profit-lock" style="color:#e6edf3;">&#8377;0</strong></div>
            </div>
            <div style="display:flex;align-items:center;gap:12px;">
                <span id="ct-trades-counter" style="font-size:12px;font-weight:600;color:#c9d1d9;">0 / 35 trades</span>
                <button id="ct-extend-btn" onclick="extendTradeLimit()" class="btn-xs btn-neutral" style="padding:4px 8px;font-size:11px;font-weight:700;background:#21262d;border-color:#30363d;color:#e6edf3;border-radius:4px;cursor:pointer;line-height:1.2;">+10 Trades</button>
            </div>
        </div>
        <div id="trading-workspace" style="display:flex;gap:16px;transition:all 0.2s;">
            <!-- Left: Option Chain -->
            <div id="oc-workspace-col" style="flex:0 0 280px;width:280px;min-width:0;transition:all 0.2s;">
                <div class="card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                        <h3 style="margin:0;">Option Chain</h3>
                        <div style="display:flex;gap:10px;align-items:center;">
                            <select id="oc-underlying" class="form-input" style="width:120px;padding:4px 8px;font-size:12px;">
                                <option value="13">NIFTY</option>
                                <option value="1">SENSEX</option>
                            </select>
                            <select id="oc-expiry" class="form-input" style="width:120px;padding:4px 8px;font-size:12px;" onchange="loadOptionChain()">
                                <option value="">Loading...</option>
                            </select>
                            <span id="oc-auto-status" style="font-size:10px;color:#3fb950;margin-left:6px;">Auto 2s</span>
                        </div>
                    </div>
                    <div id="oc-spot" style="font-size:13px;color:#8b949e;margin-bottom:8px;">Spot: --</div>
                    <div id="oc-scroll-container">
                        <table id="oc-table" style="font-size:12px;">
                            <thead style="position:sticky;top:0;background:#0d1117;z-index:1;">
                                <tr>
                                    <th style="text-align:right;color:#3fb950;">CE LTP <span style="font-size:9px;color:#484f58;">S/B</span></th>
                                    <th style="text-align:center;font-weight:700;">Strike</th>
                                    <th style="text-align:left;color:#f85149;"><span style="font-size:9px;color:#484f58;">S/B</span> PE LTP</th>
                                </tr>
                            </thead>
                            <tbody id="oc-body">
                                <tr><td colspan="3" style="text-align:center;color:#484f58;padding:20px;">Loading option chain...</td></tr>
                            </tbody>
                        </table>
                    </div>

                    <!-- Spread Quick Bar (shown when legs are selected) -->
                    <div id="sqb-panel" style="display:none;border-top:1px solid #21262d;margin-top:8px;padding-top:8px;">
                        <!-- Row 1: Leg price inputs -->
                        <div style="display:flex;gap:8px;margin-bottom:6px;font-size:11px;align-items:center;flex-wrap:wrap;">
                            <!-- Sell leg -->
                            <div style="border-left:3px solid #f85149;padding-left:6px;display:flex;align-items:center;gap:4px;">
                                <span style="color:#f85149;font-weight:700;font-size:10px;">SELL</span>
                                <span id="sqb-sell-label" style="color:#8b949e;">--</span>
                                <input id="sqb-sell-price" type="number" step="0.05" placeholder="price"
                                       style="width:68px;font-size:12px;font-weight:700;padding:3px 6px;background:#21262d;border:1px solid #30363d;border-radius:4px;color:#f85149;"
                                       oninput="sqbPriceDirty('sell');sqbAutoCalc()" title="Sell price">
                                <span style="color:#484f58;font-size:10px;">SL*</span>
                                <input id="sqb-sell-sl" type="number" step="0.05" placeholder="required"
                                       style="width:72px;font-size:12px;padding:3px 6px;background:#21262d;border:1px solid #d29922;border-radius:4px;color:#d29922;"
                                       oninput="sqbAutoCalc()" title="Stop loss price (required)">
                            </div>
                            <span style="color:#484f58;">→</span>
                            <!-- Buy leg -->
                            <div style="border-left:3px solid #3fb950;padding-left:6px;display:flex;align-items:center;gap:4px;">
                                <span style="color:#3fb950;font-weight:700;font-size:10px;">BUY</span>
                                <span id="sqb-buy-label" style="color:#8b949e;">--</span>
                                <input id="sqb-buy-price" type="number" step="0.05" placeholder="price"
                                       style="width:68px;font-size:12px;font-weight:700;padding:3px 6px;background:#21262d;border:1px solid #30363d;border-radius:4px;color:#3fb950;"
                                       oninput="sqbPriceDirty('buy');sqbAutoCalc()" title="Buy price">
                            </div>
                            <span id="sqb-net-credit" style="font-weight:700;font-size:11px;"></span>
                            <button onclick="clearSpreadQuickBar()" style="margin-left:auto;background:none;border:none;color:#484f58;cursor:pointer;font-size:11px;padding:0;" title="Clear">✕</button>
                        </div>
                        <!-- Row 2: Qty -->
                        <div style="display:flex;gap:6px;align-items:center;">
                            <input id="sqb-risk" class="form-input" type="number" placeholder="Max Loss ₹" style="width:90px;font-size:11px;padding:4px 6px;" oninput="sqbAutoCalc()">
                            <span style="font-size:11px;color:#8b949e;">Qty:</span>
                            <strong id="sqb-qty" style="font-size:12px;color:#e6edf3;">-</strong>
                            <span id="sqb-lots" style="font-size:10px;color:#484f58;"></span>
                            <input id="sqb-qty-override" class="form-input" type="number" placeholder="Override" style="width:72px;font-size:11px;padding:4px 6px;" title="Override calculated qty" oninput="sqbAutoCalc()">
                        </div>
                        <!-- Row 3: All buttons -->
                        <div style="display:flex;gap:6px;align-items:center;">
                            <button id="sqb-execute-btn" onclick="executeSpreadNow('LIMIT')" disabled class="btn-sell" style="padding:5px 14px;font-size:12px;font-weight:700;border:none;border-radius:6px;cursor:pointer;opacity:0.5;">&#9889; EXECUTE LMT</button>
                            <button id="sqb-execute-mkt-btn" onclick="executeSpreadNow('MARKET')" disabled class="btn-sell" style="padding:5px 10px;font-size:12px;font-weight:700;border:none;border-radius:6px;cursor:pointer;opacity:0.5;background:#7a1a1a;" title="Both legs at MARKET price">&#9889; MKT</button>
                            <button id="sqb-single-sell-mkt-btn" onclick="executeSingleLeg('sell','MARKET')" style="display:none;padding:5px 10px;font-size:11px;font-weight:700;background:#5a1a1a;color:#f85149;border:1px solid #f85149;border-radius:6px;cursor:pointer;" title="Emergency: sell leg only at MARKET">SELL MKT</button>
                            <button id="sqb-single-buy-mkt-btn" onclick="executeSingleLeg('buy','MARKET')" style="display:none;padding:5px 10px;font-size:11px;font-weight:700;background:#0a2e1a;color:#3fb950;border:1px solid #3fb950;border-radius:6px;cursor:pointer;" title="Emergency: buy leg only at MARKET">BUY MKT</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Right: Depth of Market -->
            <div style="flex:1;min-width:0;">
                <div class="card" id="dom-panel" style="padding:12px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <div style="display:flex;align-items:center;gap:10px;">
                            <h3 id="dom-title-text" style="margin:0;">Depth of Market</h3>
                            <h3 id="chart-title-text" style="margin:0;display:none;">Option Chart</h3>
                            <div style="display:flex;gap:2px;align-items:center;">
                                <button id="dom-tab-depth" onclick="switchDomTab('depth')" style="background:none;border:none;color:#3fb950;cursor:pointer;font-size:11px;font-weight:700;padding:2px 8px;border-bottom:2px solid #3fb950;">Depth</button>
                                <button id="dom-tab-chart" onclick="switchDomTab('chart')" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:11px;font-weight:400;padding:2px 8px;border-bottom:2px solid transparent;">Chart</button>
                                <div id="chart-timeframe-group" style="display:none;gap:4px;align-items:center;margin-left:6px;">
                                    <button id="tf-btn-60" onclick="setChartTimeframe(60)" class="btn-xs btn-neutral" style="padding:2px 6px;font-size:10px;font-weight:700;background:#238636;border-color:#238636;color:#ffffff;line-height:1.2;">1m</button>
                                    <button id="tf-btn-15" onclick="setChartTimeframe(15)" class="btn-xs btn-neutral" style="padding:2px 6px;font-size:10px;font-weight:700;background:#21262d;border-color:#30363d;color:#8b949e;line-height:1.2;">15s</button>
                                    <button id="tf-btn-5" onclick="setChartTimeframe(5)" class="btn-xs btn-neutral" style="padding:2px 6px;font-size:10px;font-weight:700;background:#21262d;border-color:#30363d;color:#8b949e;line-height:1.2;">5s</button>
                                </div>
                                <input type="hidden" id="chart-timeframe" value="60">
                            </div>
                        </div>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <span id="dom-instrument" style="font-size:12px;color:#8b949e;">Select an option from the chain</span>
                            <span id="dom-status" style="display:none;font-size:10px;padding:2px 6px;border-radius:8px;background:#0d4429;color:#3fb950;font-weight:600;">LIVE</span>
                            <button id="dom-maximize-btn" onclick="toggleMaximizeChart()" style="background:none;border:1px solid #30363d;color:#8b949e;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;margin-right:4px;" title="Maximize/Stretched View">&#x1f5d6; Maximize</button>
                            <button id="dom-close-btn" onclick="domClose()" style="display:none;background:none;border:1px solid #30363d;color:#8b949e;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;" title="Close DOM panel">✕</button>
                        </div>
                    </div>

                    <!-- Analysis Summary -->
                    <div id="dom-analysis" style="display:none;margin-bottom:12px;">
                        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px;">
                            <div id="dom-sentiment" style="padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;letter-spacing:0.5px;"></div>
                            <div style="font-size:12px;color:#8b949e;">
                                Spread: <span id="dom-spread" style="color:#e6edf3;font-weight:600;">--</span>
                                <span id="dom-spread-pct" style="color:#8b949e;font-size:11px;"></span>
                            </div>
                            <div style="font-size:12px;color:#8b949e;">
                                Imbalance: <span id="dom-imbalance" style="font-weight:600;">--</span>
                            </div>
                        </div>
                        <div style="display:flex;gap:8px;flex-wrap:wrap;font-size:10px;">
                            <div>
                                <span style="color:#3fb950;">Buy Wall:</span>
                                <span id="dom-buy-wall" style="color:#e6edf3;font-weight:600;">--</span>
                            </div>
                            <div>
                                <span style="color:#f85149;">Sell Wall:</span>
                                <span id="dom-sell-wall" style="color:#e6edf3;font-weight:600;">--</span>
                            </div>
                            <div>
                                <span style="color:#8b949e;">Total Bid:</span>
                                <span id="dom-total-bid" style="color:#3fb950;">--</span>
                            </div>
                            <div>
                                <span style="color:#8b949e;">Total Ask:</span>
                                <span id="dom-total-ask" style="color:#f85149;">--</span>
                            </div>
                            <div>
                                <span style="color:#58a6ff;">Support:</span>
                                <span id="dom-support" style="color:#3fb950;font-weight:600;">--</span>
                            </div>
                            <div>
                                <span style="color:#58a6ff;">Resistance:</span>
                                <span id="dom-resistance" style="color:#f85149;font-weight:600;">--</span>
                            </div>
                        </div>
                        <!-- Imbalance bar -->
                        <div style="margin-top:8px;height:6px;background:#21262d;border-radius:3px;overflow:hidden;display:flex;">
                            <div id="dom-imbalance-bar-bid" style="height:100%;background:#3fb950;transition:width 0.3s;"></div>
                            <div id="dom-imbalance-bar-ask" style="height:100%;background:#f85149;transition:width 0.3s;"></div>
                        </div>
                    </div>

                    <!-- Chart Canvas (hidden by default, shown on Chart tab) -->
                    <!-- Chart Canvas (hidden by default, shown on Chart tab) -->
                    <div id="dom-chart-canvas-container" style="display:none;position:relative;">
                        <div id="dom-chart-canvas" style="height:420px;width:100%;"></div>
                        
                        <!-- Floating TV-style Order Placer Tags -->
                        <div id="chart-tag-breakout" style="display:none;position:absolute;right:80px;background:rgba(255,153,0,0.9);border:1px solid #ff9900;border-radius:4px;padding:3px 8px;z-index:50;color:#0d1117;font-family:monospace;font-size:10px;font-weight:bold;box-shadow:0 2px 5px rgba(0,0,0,0.5);display:flex;align-items:center;gap:6px;">
                            <span>BREAKOUT LIMIT: <input id="chart-tag-breakout-input" type="number" step="0.05" style="width:65px;font-size:10px;background:#0d1117;color:#ff9900;border:1px solid #ff9900;border-radius:3px;padding:1px;text-align:center;font-weight:bold;margin:0 4px;" onchange="updatePriceFromTag('breakout')"></span>
                            <span>Qty: <input id="chart-tag-breakout-qty-input" type="number" style="width:50px;font-size:10px;background:#0d1117;color:#ff9900;border:1px solid #ff9900;border-radius:3px;padding:1px;text-align:center;font-weight:bold;margin:0 4px;" onchange="updateQtyFromTag()"></span>
                            <button onclick="submitChartTriggerOrders()" style="background:#0d1117;color:#ff9900;border:none;border-radius:3px;padding:1px 5px;cursor:pointer;font-size:9px;font-weight:bold;">⚡ TRANSMIT</button>
                        </div>
                        
                        <div id="chart-tag-sl" style="display:none;position:absolute;right:80px;background:rgba(248,81,73,0.9);border:1px solid #f85149;border-radius:4px;padding:3px 8px;z-index:50;color:#ffffff;font-family:monospace;font-size:10px;font-weight:bold;box-shadow:0 2px 5px rgba(0,0,0,0.5);display:flex;align-items:center;gap:6px;">
                            <span><span id="chart-tag-sl-label">STOP LOSS LIMIT</span>: <input id="chart-tag-sl-input" type="number" step="0.05" style="width:65px;font-size:10px;background:#0d1117;color:#f85149;border:1px solid #f85149;border-radius:3px;padding:1px;text-align:center;font-weight:bold;margin:0 4px;" onchange="updatePriceFromTag('sl')"></span>
                            <button onclick="submitStopLoss1Click()" style="background:#ffffff;color:#f85149;border:none;border-radius:3px;padding:1px 5px;cursor:pointer;font-size:9px;font-weight:bold;">⚡ PLACE SL</button>
                        </div>
                        <!-- Chart Price Controls -->
                        <div style="display:flex;align-items:center;justify-content:space-between;padding:8px;background:#161b22;border:1px solid #30363d;border-radius:6px;margin-top:6px;gap:6px;flex-wrap:wrap;">
                            <div style="display:flex;align-items:center;gap:6px;">
                                <span style="font-size:11px;color:#8b949e;font-weight:600;">Breakout:</span>
                                <input id="chart-breakout-val" type="number" step="0.05" style="width:75px;font-size:11px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px;height:24px;" onchange="updatePriceLineFromInput('breakout')">
                                <button onclick="chartClickPlacement('breakout')" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;font-size:10px;padding:3px 6px;border-radius:4px;cursor:pointer;height:24px;" title="Click on chart to set Breakout trigger">&#x1f570; Place</button>
                            </div>
                            <div style="display:flex;align-items:center;gap:6px;">
                                <span style="font-size:11px;color:#8b949e;font-weight:600;">Stop Loss:</span>
                                <input id="chart-sl-val" type="number" step="0.05" style="width:75px;font-size:11px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px;height:24px;" onchange="updatePriceLineFromInput('sl')">
                                <button onclick="chartClickPlacement('sl')" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;font-size:10px;padding:3px 6px;border-radius:4px;cursor:pointer;height:24px;" title="Click on chart to set Stop Loss">&#x1f570; Place</button>
                            </div>
                            <div style="display:flex;align-items:center;gap:6px;">
                                <span style="font-size:11px;color:#8b949e;font-weight:600;">Take Profit:</span>
                                <input id="chart-tp-val" type="number" step="0.05" style="width:75px;font-size:11px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px;height:24px;" onchange="updatePriceLineFromInput('tp')">
                                <button onclick="chartClickPlacement('tp')" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;font-size:10px;padding:3px 6px;border-radius:4px;cursor:pointer;height:24px;" title="Click on chart to set Take Profit">&#x1f570; Place</button>
                            </div>
                            <button id="chart-submit-orders" onclick="submitChartTriggerOrders()" style="background:#238636;border:none;color:#ffffff;font-size:11px;font-weight:700;padding:4px 10px;border-radius:4px;cursor:pointer;height:24px;" title="Send trigger order to broker">&#9889; Transmit</button>
                        </div>
                    </div>

                    <!-- Depth Chart -->
                    <div id="dom-chart" style="overflow-y:auto;">
                        <div style="color:#484f58;padding:30px;text-align:center;font-size:13px;">
                            Click any CE or PE price in the option chain to view 20-level market depth
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Order Placement Panel (hidden — superseded by option chain quick bar) -->
    <div style="padding:0 24px;display:none;">
        <div class="card">
            <!-- DISABLED: Naked Order + Spread Entry tabs hidden — superseded by Option Chain quick bar -->
            <!-- To re-enable: remove the display:none from the tab headers div and tab-naked/tab-spread divs -->
            <div style="display:flex;gap:0;border-bottom:1px solid #21262d;margin-bottom:16px;display:none;">
                <div class="order-tab active" data-tab="naked" onclick="switchOrderTab('naked')" style="padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid #58a6ff;color:#58a6ff;">Naked Order</div>
                <div class="order-tab" data-tab="spread" onclick="switchOrderTab('spread')" style="padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;">Spread Entry</div>
            </div>

            <!-- ═══ NAKED ORDER TAB ═══ (hidden — use option chain quick bar instead) -->
            <div id="tab-naked" style="display:none;">
                <!-- Instrument Search -->
                <div class="search-wrapper" style="margin-bottom:12px;">
                    <div class="form-label">Instrument</div>
                    <input id="instrument-search" class="form-input" placeholder="Search... e.g. NIFTY 24000 CE" autocomplete="off">
                    <div class="search-results" id="search-results"></div>
                    <input type="hidden" id="order-security-id">
                    <input type="hidden" id="order-exchange-segment">
                    <input type="hidden" id="order-lot-size" value="1">
                    <input type="hidden" id="order-tick-size" value="0.05">
                    <div id="selected-instrument" style="margin-top:4px;font-size:12px;color:#58a6ff;display:none;"></div>
                </div>

                <!-- Integrated Order Form: fields + auto-sizing -->
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr 1fr 1fr 1fr;gap:10px;margin-bottom:12px;">
                    <div>
                        <div class="form-label">Side</div>
                        <select id="order-txn-type" class="form-select">
                            <option value="BUY">BUY</option>
                            <option value="SELL">SELL</option>
                        </select>
                    </div>
                    <div>
                        <div class="form-label">Order Type</div>
                        <select id="order-type" class="form-select">
                            <option value="STOP_LOSS">STOP LIMIT</option>
                            <option value="STOP_LOSS_MARKET">STOP MARKET</option>
                            <option value="LIMIT">LIMIT</option>
                            <option value="MARKET">MARKET</option>
                        </select>
                    </div>
                    <div>
                        <div class="form-label">Product</div>
                        <select id="order-product-type" class="form-select">
                            <option value="MARGIN">MARGIN</option>
                            <option value="INTRADAY">INTRADAY</option>
                            <option value="CNC">CNC</option>
                        </select>
                    </div>
                    <div>
                        <div class="form-label">Price</div>
                        <input id="order-price" class="form-input calc-trigger" type="number" step="0.05" placeholder="0.00">
                    </div>
                    <div>
                        <div class="form-label">Trigger</div>
                        <input id="order-trigger-price" class="form-input" type="number" step="0.05" placeholder="0.00">
                    </div>
                    <div>
                        <div class="form-label">SL Price</div>
                        <input id="calc-sl" class="form-input calc-trigger" type="number" step="0.05" placeholder="0.00">
                    </div>
                    <div>
                        <div class="form-label">TP Price</div>
                        <input id="calc-tp" class="form-input" type="number" step="0.05" placeholder="0.00">
                    </div>
                    <div>
                        <div class="form-label">Max Loss (&#8377;)</div>
                        <input id="calc-risk" class="form-input calc-trigger" type="number" value="{{ default_risk }}">
                    </div>
                </div>

                <!-- Auto-calculated sizing display + submit -->
                <div style="display:flex;gap:16px;align-items:center;justify-content:space-between;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 16px;">
                    <div style="display:flex;gap:24px;align-items:center;font-size:13px;">
                        <div><span class="form-label">Qty:</span> <strong id="calc-qty" style="font-size:16px;">-</strong> <span id="calc-lots" style="color:#8b949e;font-size:11px;"></span></div>
                        <div><span class="form-label">Risk/Unit:</span> <strong id="calc-risk-unit">-</strong></div>
                        <div><span class="form-label">Actual Risk:</span> <strong id="calc-actual-risk" class="negative">-</strong></div>
                        <div id="calc-feasibility" style="font-size:12px;"></div>
                    </div>
                    <div style="display:flex;gap:8px;">
                        <input id="order-quantity" class="form-input" type="number" placeholder="Qty" style="width:80px;text-align:center;">
                        <button onclick="placeOrder('BUY')" class="btn-buy" style="padding:8px 24px;font-size:14px;font-weight:700;border:none;border-radius:6px;cursor:pointer;">BUY</button>
                        <button onclick="placeOrder('SELL')" class="btn-sell" style="padding:8px 24px;font-size:14px;font-weight:700;border:none;border-radius:6px;cursor:pointer;">SELL</button>
                    </div>
                </div>
            </div>

            <!-- ═══ SPREAD ENTRY TAB ═══ (hidden — use option chain quick bar instead) -->
            <div id="tab-spread" style="display:none;">
                <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 16px;margin-bottom:12px;font-size:12px;color:#8b949e;">
                    Spread entry: when sell trigger hits, the hedge leg is bought at market first, then the sell executes at your price. This ensures margin availability.
                </div>

                <!-- Sell Leg (main) -->
                <div style="border-left:3px solid #f85149;padding-left:12px;margin-bottom:16px;">
                    <div class="form-label" style="color:#f85149;font-weight:600;margin-bottom:8px;">SELL LEG (main)</div>
                    <div class="search-wrapper" style="margin-bottom:8px;">
                        <input id="spread-sell-search" class="form-input" placeholder="Search sell instrument..." autocomplete="off">
                        <div class="search-results" id="spread-sell-results"></div>
                        <input type="hidden" id="spread-sell-id">
                        <input type="hidden" id="spread-sell-exseg">
                        <input type="hidden" id="spread-sell-lot" value="1">
                        <div id="spread-sell-info" style="margin-top:4px;font-size:12px;color:#f85149;display:none;"></div>
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;">
                        <div>
                            <div class="form-label">Sell Price</div>
                            <input id="spread-sell-price" class="form-input spread-calc" type="number" step="0.05" placeholder="0.00">
                        </div>
                        <div>
                            <div class="form-label">Trigger Price</div>
                            <input id="spread-sell-trigger" class="form-input" type="number" step="0.05" placeholder="0.00">
                        </div>
                        <div>
                            <div class="form-label">SL Price</div>
                            <input id="spread-sell-sl" class="form-input spread-calc" type="number" step="0.05" placeholder="0.00">
                        </div>
                        <div>
                            <div class="form-label">Max Loss (&#8377;)</div>
                            <input id="spread-risk" class="form-input spread-calc" type="number" value="{{ default_risk }}">
                        </div>
                    </div>
                </div>

                <!-- Buy Leg (hedge) -->
                <div style="border-left:3px solid #3fb950;padding-left:12px;margin-bottom:16px;">
                    <div class="form-label" style="color:#3fb950;font-weight:600;margin-bottom:8px;">BUY LEG (hedge - bought at market)</div>
                    <div class="search-wrapper" style="margin-bottom:8px;">
                        <input id="spread-buy-search" class="form-input" placeholder="Search hedge instrument..." autocomplete="off">
                        <div class="search-results" id="spread-buy-results"></div>
                        <input type="hidden" id="spread-buy-id">
                        <input type="hidden" id="spread-buy-exseg">
                        <input type="hidden" id="spread-buy-lot" value="1">
                        <div id="spread-buy-info" style="margin-top:4px;font-size:12px;color:#3fb950;display:none;"></div>
                    </div>
                </div>

                <!-- Spread sizing + submit -->
                <div style="display:flex;gap:16px;align-items:center;justify-content:space-between;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 16px;">
                    <div style="display:flex;gap:24px;align-items:center;font-size:13px;">
                        <div><span class="form-label">Qty:</span> <strong id="spread-qty">-</strong> <span id="spread-lots" style="color:#8b949e;font-size:11px;"></span></div>
                        <div><span class="form-label">Risk/Unit:</span> <strong id="spread-risk-unit">-</strong></div>
                        <div><span class="form-label">Actual Risk:</span> <strong id="spread-actual-risk" class="negative">-</strong></div>
                    </div>
                    <div style="display:flex;gap:8px;align-items:center;">
                        <input id="spread-quantity" class="form-input" type="number" placeholder="Qty" style="width:80px;text-align:center;">
                        <button onclick="placeSpreadOrder()" class="btn-sell" style="padding:8px 24px;font-size:14px;font-weight:700;border:none;border-radius:6px;cursor:pointer;">PLACE SPREAD</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Positions Table -->
    <div style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <h3 style="margin:0;">Open Positions</h3>
                <button onclick="exitAllPositions()" class="btn-danger btn-sm">EXIT ALL</button>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Instrument</th>
                        <th>Qty</th>
                        <th>Avg Price</th>
                        <th>LTP</th>
                        <th>P&L</th>
                        <th>SL / TP</th>
                        <th>Quick Set</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="positions-body">
                    <tr><td colspan="8" style="text-align:center;color:#484f58;">No open positions</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Pending Orders -->
    <div style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <h3 style="margin:0;">Pending Orders</h3>
                <span id="pending-orders-count" style="color:#8b949e;font-size:12px;"></span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Instrument</th>
                        <th>Side</th>
                        <th>Qty</th>
                        <th>Type</th>
                        <th>Price</th>
                        <th>Trigger</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="pending-orders-body">
                    <tr><td colspan="8" style="text-align:center;color:#484f58;">No pending orders</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Today's Trades -->
    <div class="desktop-only" style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <h3 style="margin:0;">Today's Trades</h3>
                <a href="/analytics" target="_blank" style="font-size:11px;color:#58a6ff;text-decoration:none;">Full Analytics &#x2197;</a>
            </div>
            <table>
                <thead><tr><th>Time</th><th>Instrument</th><th>Type</th><th>Qty</th><th>P&L</th></tr></thead>
                <tbody id="journal-today-body">
                    <tr><td colspan="5" style="text-align:center;color:#484f58;">No trades today</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Token Update -->
    <div style="padding:0 24px;margin-top:16px;">
        <details style="background:#161b22;border:1px solid #21262d;border-radius:8px;padding:12px 16px;">
            <summary style="cursor:pointer;color:#8b949e;font-size:13px;font-weight:600;">Update Dhan Access Token</summary>
            <div style="margin-top:12px;display:flex;gap:10px;align-items:center;">
                <input id="new-token-input" type="password" class="form-input" placeholder="Paste new Dhan access token" style="flex:1;">
                <button onclick="updateToken()" class="btn-neutral" style="padding:8px 20px;white-space:nowrap;">Update Token</button>
            </div>
            <div style="margin-top:8px;font-size:11px;color:#484f58;">
                Get your token from <a href="https://web.dhan.co" target="_blank" style="color:#58a6ff;">web.dhan.co</a> &rarr; API section. Token expires daily.
            </div>
        </details>
    </div>

    <div class="footer">
        Trade Management Platform | Risk data refreshes every {{ interval }}s | State is encrypted and tamper-proof
    </div>

    <script>
        // ── CDN Script Loader (non-blocking with timeout) ────────────
        // Loads external scripts without blocking the page. If a CDN
        // fails or takes too long, the dashboard still works fully.
        function loadScript(url, timeout, onReady) {
            var done = false;
            var s = document.createElement('script');
            s.src = url;
            s.async = true;
            s.onload = function() { if (!done) { done = true; onReady(true); } };
            s.onerror = function() { if (!done) { done = true; console.warn('CDN load failed:', url); onReady(false); } };
            setTimeout(function() { if (!done) { done = true; console.warn('CDN load timeout:', url); onReady(false); } }, timeout);
            document.head.appendChild(s);
        }

        // Socket.IO - loaded async, fallback to polling if unavailable
        var socket = null;
        loadScript('https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js', 8000, function(ok) {
            if (ok && typeof io !== 'undefined') {
                try {
                    socket = io();
                    if (typeof setupSocketListeners === 'function') setupSocketListeners();
                    console.log('Socket.IO connected');
                } catch(e) {
                    console.warn('Socket.IO connection failed:', e);
                }
            } else {
                console.warn('Socket.IO not available - using polling only');
            }
        });

        // Chart.js + zoom plugin — loaded async
        loadScript('https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js', 8000, function(ok) {
            if (ok && typeof Chart !== 'undefined') {
                // Load zoom plugin (requires hammerjs for pinch on mobile)
                loadScript('https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js', 5000, function() {
                    loadScript('https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js', 5000, function() {
                        try {
                            initEquityCharts();
                            console.log('Chart.js + zoom initialized');
                            refreshEquityCurve();
                            setInterval(refreshEquityCurve, 60000);
                        } catch(e) { console.warn('Chart init error:', e); }
                    });
                });
            } else {
                console.warn('Chart.js not available - chart disabled');
            }
        });

        function refreshEquityCurve() {
            fetch('/api/equity_curve')
                .then(function(r) { return r.json(); })
                .then(function(d) { updateEquityCharts(d); })
                .catch(function(e) { console.warn('Equity curve fetch error:', e); });
        }

        const QUICK_SL_OFFSETS = {{ quick_sl_offsets }};
        const QUICK_TP_OFFSETS = {{ quick_tp_offsets }};

        // Auto hard-refresh every hour (clears stale WS state, memory leaks, etc.)
        setTimeout(function(){ location.reload(true); }, 3600000);

        // ── Sound Alert System (with volume/mute controls) ─────────
        var AudioCtx = window.AudioContext || window.webkitAudioContext;
        var audioCtx = null;
        var _audioVolume = parseFloat(localStorage.getItem('audioVolume') || '0.3');
        var _audioMuted = localStorage.getItem('audioMuted') === 'true';

        function initAudio() {
            if (!audioCtx) {
                try { audioCtx = new AudioCtx(); } catch(e) {}
            }
        }
        document.addEventListener('click', initAudio, { once: true });

        function getVolume() { return _audioMuted ? 0 : _audioVolume; }

        function toggleMute() {
            _audioMuted = !_audioMuted;
            localStorage.setItem('audioMuted', _audioMuted);
            var btn = document.getElementById('mute-btn');
            if (btn) {
                btn.innerHTML = _audioMuted ? '&#x1f507;' : '&#x1f50a;';
                btn.style.borderColor = _audioMuted ? '#f85149' : '#30363d';
                btn.style.color = _audioMuted ? '#f85149' : '#8b949e';
            }
        }

        // Init audio controls from localStorage
        (function() {
            var slider = document.getElementById('volume-slider');
            if (slider) {
                slider.value = _audioVolume * 100;
                slider.addEventListener('input', function() {
                    _audioVolume = this.value / 100;
                    localStorage.setItem('audioVolume', _audioVolume);
                });
            }
            if (_audioMuted) {
                var btn = document.getElementById('mute-btn');
                if (btn) { btn.innerHTML = '&#x1f507;'; btn.style.borderColor = '#f85149'; btn.style.color = '#f85149'; }
            }
        })();

        function playBeep(freq, dur, type) {
            if (!audioCtx || getVolume() === 0) return;
            try {
                var osc = audioCtx.createOscillator();
                var gain = audioCtx.createGain();
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.frequency.value = freq;
                osc.type = type || 'sine';
                gain.gain.value = getVolume();
                osc.start();
                gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + dur);
                osc.stop(audioCtx.currentTime + dur);
            } catch(e) {}
        }

        function playAlert(type) {
            switch(type) {
                case 'sl_hit':
                    playBeep(800, 0.15); setTimeout(function(){playBeep(400, 0.3);}, 200);
                    break;
                case 'tp_hit':
                    playBeep(400, 0.15); setTimeout(function(){playBeep(800, 0.3);}, 200);
                    break;
                case 'loss_warning':
                    playBeep(600, 0.1); setTimeout(function(){playBeep(600, 0.1);}, 150);
                    setTimeout(function(){playBeep(600, 0.1);}, 300);
                    break;
                case 'lockout':
                    playBeep(200, 1.0, 'square');
                    break;
                case 'order':
                    playBeep(1000, 0.08);
                    break;
                case 'error':
                    playBeep(300, 0.3, 'sawtooth');
                    break;
                case 'cooldown_end':
                    playBeep(500, 0.15); setTimeout(function(){playBeep(700, 0.15);}, 180);
                    setTimeout(function(){playBeep(900, 0.3);}, 360);
                    break;
                case 'profit_lock':
                    playBeep(1200, 0.12); setTimeout(function(){playBeep(1500, 0.2);}, 150);
                    break;
                case 'approaching_loss_limit':
                    playBeep(500, 0.08, 'triangle'); setTimeout(function(){playBeep(500, 0.08, 'triangle');}, 120);
                    setTimeout(function(){playBeep(500, 0.08, 'triangle');}, 240);
                    setTimeout(function(){playBeep(700, 0.15, 'triangle');}, 400);
                    break;
            }
        }

        // Request browser notification permission
        if ('Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
        function showBrowserNotif(title, body) {
            if ('Notification' in window && Notification.permission === 'granted') {
                new Notification(title, { body: body });
            }
        }

        // ── Toast Notifications ────────────────────────────────────
        function showToast(msg, type) {
            type = type || 'info';
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = 'toast toast-' + type;
            toast.textContent = msg;
            container.appendChild(toast);
            setTimeout(function(){ toast.remove(); }, 4000);
        }

        // ── Helpers ────────────────────────────────────────────────
        function fmt(n) {
            if (n === null || n === undefined) return '\\u20B90';
            var sign = n < 0 ? '-' : '';
            return sign + '\\u20B9' + Math.abs(n).toLocaleString('en-IN', {maximumFractionDigits: 0});
        }
        function fmtDec(n) {
            if (n === null || n === undefined) return '\\u20B90';
            var sign = n < 0 ? '-' : '';
            return sign + '\\u20B9' + Math.abs(n).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        }
        function fmtPct(n) {
            return (n || 0).toFixed(1) + '%';
        }

        // ── Alert State Tracking ───────────────────────────────────
        var _alertState = {
            lockout: false, cooldown: false,
            lossWarn80: false, lossWarn90: false,
            profitLock: false, cooldownEndNotified: false
        };
        var _lastSlTpData = {};

        function checkAlerts(data) {
            var pct = data.limits.loss_used_pct;

            // Approaching loss limit (80%)
            if (pct >= 80 && !_alertState.lossWarn80) {
                playAlert('approaching_loss_limit');
                showToast('80% of daily loss limit used!', 'warning');
                showBrowserNotif('Loss Warning', '80% of daily loss limit used!');
                _alertState.lossWarn80 = true;
            }
            // 90% loss limit
            if (pct >= 90 && !_alertState.lossWarn90) {
                playAlert('loss_warning');
                showToast('90% of daily loss limit used!', 'error');
                showBrowserNotif('Loss Warning', '90% of daily loss limit used!');
                _alertState.lossWarn90 = true;
            }
            // Lockout
            if (data.lockout.active && !_alertState.lockout) {
                playAlert('lockout');
                showToast('ACCOUNT LOCKED: ' + data.lockout.reason, 'error');
                showBrowserNotif('ACCOUNT LOCKED', data.lockout.reason);
                _alertState.lockout = true;
            }
            // Cooldown start
            if (data.cooldown.active && !_alertState.cooldown) {
                playAlert('loss_warning');
                showToast('Cooldown active: ' + data.cooldown.reason, 'warning');
                _alertState.cooldown = true;
                _alertState.cooldownEndNotified = false;
            }
            // Cooldown end
            if (!data.cooldown.active && _alertState.cooldown && !_alertState.cooldownEndNotified) {
                playAlert('cooldown_end');
                showToast('Cooldown expired - you can trade again', 'success');
                showBrowserNotif('Cooldown Over', 'You can trade again');
                _alertState.cooldownEndNotified = true;
            }
            if (!data.cooldown.active) _alertState.cooldown = false;

            // Profit lock activation
            if (data.profit_lock.active && !_alertState.profitLock) {
                playAlert('profit_lock');
                showToast('Profit Lock activated! Floor: ' + fmt(data.profit_lock.floor), 'info');
                showBrowserNotif('Profit Lock Active', 'Floor set at ' + fmt(data.profit_lock.floor));
                _alertState.profitLock = true;
            }
        }

        // ── SL/TP trigger from server ──────────────────────────────
        // Called from CDN loader when socket connects asynchronously
        function setupSocketListeners() {
            if (!socket) return;
            socket.on('sl_tp_triggered', function(data) {
                if (data.action === 'SPREAD_FILLED') {
                    playAlert('order');
                    showToast('Spread filled: ' + data.security_id + ' SELL @ \\u20B9' + (data.trigger_price || 0).toFixed(2), 'success');
                    return;
                }
                if (data.action === 'SPREAD_FAILED') {
                    playAlert('error');
                    showToast('Spread FAILED: ' + (data.error || 'unknown error'), 'error');
                    return;
                }
                var isTP = data.action === 'TAKE_PROFIT';
                playAlert(isTP ? 'tp_hit' : 'sl_hit');
                var msg = data.action + ' triggered for ' + data.security_id +
                          ' @ \\u20B9' + (data.trigger_price || 0).toFixed(2) +
                          ' (LTP: \\u20B9' + (data.ltp || 0).toFixed(2) + ')';
                showToast(msg, isTP ? 'success' : 'error');
                showBrowserNotif(data.action, msg);
                // Capture exit screenshot for journal
                if (data.security_id) {
                    closeJournalEntry(String(data.security_id),
                        data.exit_price || data.ltp || 0, 0,
                        data.pnl || null);
                }
            });
            if (typeof safeUpdate === 'function') {
                socket.on('status_update', safeUpdate);
            }
            socket.on('oc_ltp', function(data) {
                // Closed Market / Holiday Filter (IST checks)
                var now = new Date();
                var utc = now.getTime() + (now.getTimezoneOffset() * 60000);
                var istTime = new Date(utc + (3600000 * 5.5));
                var day = istTime.getDay();
                var hour = istTime.getHours();
                var min = istTime.getMinutes();
                var timeVal = hour * 100 + min;
                var isHoliday = (day === 0 || day === 6);
                var isMarketOpen = (!isHoliday && timeVal >= 915 && timeVal <= 1530);
                if (!isMarketOpen) return;

                // Only cache non-zero prices — zero bid means no active market
                var ltp = parseFloat(data.ltp);
                if (!ltp || ltp <= 0) return;
                _ltpCache[String(data.sid)] = ltp;
                // Real-time LTP update for option chain cells
                var el = document.getElementById('oc-ltp-' + data.sid);
                if (el) el.textContent = parseFloat(data.ltp).toFixed(2);
                // Update quick bar if this is the selected sell/buy leg
                if (_spreadSellLeg && String(_spreadSellLeg.securityId) === String(data.sid)) {
                    _spreadSellLeg.ltp = data.ltp;
                    if (!_sqbSellPriceDirty) {
                        var sp = document.getElementById('sqb-sell-price');
                        if (sp) sp.value = data.ltp.toFixed(2);
                    }
                    // Update live chart bar
                    if (_lwSeries && _lwCurrentSecurity === _spreadSellLeg.securityId) {
                        // Closed Market / Holiday Filter (IST checks)
                        var now = new Date();
                        var utc = now.getTime() + (now.getTimezoneOffset() * 60000);
                        var istTime = new Date(utc + (3600000 * 5.5));
                        var day = istTime.getDay();
                        var hour = istTime.getHours();
                        var min = istTime.getMinutes();
                        var timeVal = hour * 100 + min;
                        var isHoliday = (day === 0 || day === 6);
                        var isMarketOpen = (!isHoliday && timeVal >= 915 && timeVal <= 1530);
                        if (!isMarketOpen) return;

                        var nowSec = Math.floor(Date.now() / 1000);
                        var tfEl = document.getElementById('chart-timeframe');
                        var interval = tfEl ? parseInt(tfEl.value) : 60;
                        var barSec = nowSec - (nowSec % interval);
                        var ltp = data.ltp;
                        if (barSec !== _liveBarTime) {
                            _liveBarTime = barSec; _liveBarOpen = ltp;
                            _liveBarHigh = ltp;    _liveBarLow  = ltp;
                        } else {
                            if (ltp > _liveBarHigh) _liveBarHigh = ltp;
                            if (ltp < _liveBarLow)  _liveBarLow  = ltp;
                        }
                        try { _lwSeries.update({time: barSec, open: _liveBarOpen, high: _liveBarHigh, low: _liveBarLow, close: ltp}); } catch(e) {}
                    }
                }
                if (_spreadBuyLeg && String(_spreadBuyLeg.securityId) === String(data.sid)) {
                    _spreadBuyLeg.ltp = data.ltp;
                    if (!_sqbBuyPriceDirty) {
                        var bp = document.getElementById('sqb-buy-price');
                        if (bp) bp.value = data.ltp.toFixed(2);
                    }
                }
            });
            socket.on('order_update', function(data) {
                if (data.orderStatus === 'TRADED') {
                    playAlert('order');
                    showToast('\\u2713 Filled: ' + data.symbol + ' qty ' + data.tradedQty, 'success');
                }
                // REJECTED is handled by SPREAD_FAILED event
            });
            socket.on('SPREAD_FAILED', function(data) {
                playAlert('error');
                showToast('Order REJECTED: ' + (data.reason || 'unknown'), 'error');
            });
        }
        if (socket) setupSocketListeners();

        // ── Dashboard Update ───────────────────────────────────────
        function updateDashboard(data) {
            // Ensure nested objects exist to prevent TypeError
            data.lockout = data.lockout || {active: false, reason: '', time: null};
            data.cooldown = data.cooldown || {active: false, remaining_seconds: 0, reason: ''};
            data.pnl = data.pnl || {realized: 0, unrealized: 0, total: 0, peak: 0};
            data.limits = data.limits || {daily_max_loss: 0, loss_remaining: 0, loss_used_pct: 0};
            data.profit_lock = data.profit_lock || {active: false, threshold: 0, distance: 0};
            data.trailing_drawdown = data.trailing_drawdown || {enabled: false};
            data.trades = data.trades || {total: 0, winners: 0, losers: 0, win_rate: 0, history: []};

            // Status badge
            var badge = document.getElementById('status-badge');
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
            var cdBanner = document.getElementById('cooldown-banner');
            if (data.cooldown.active) {
                cdBanner.classList.add('active');
                var secs = data.cooldown.remaining_seconds;
                var m = Math.floor(secs / 60);
                var s = secs % 60;
                document.getElementById('cooldown-timer').textContent =
                    String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
                document.getElementById('cooldown-reason').textContent = data.cooldown.reason;
            } else {
                cdBanner.classList.remove('active');
            }

            // Check for lot size warnings
            var activeWarn = null;
            for (var k in data) {
                if (k.startsWith('lot_warn_') && data[k] !== null && data[k] !== undefined) {
                    activeWarn = data[k];
                    break;
                }
            }
            var warnModal = document.getElementById('lot-warn-modal');
            if (activeWarn && warnModal) {
                document.getElementById('lot-warn-symbol').textContent = activeWarn.symbol;
                document.getElementById('lot-warn-qty').textContent = activeWarn.qty;
                document.getElementById('lot-warn-limit').textContent = activeWarn.limit;
                document.getElementById('lot-warn-timer').textContent = activeWarn.seconds_left + 's';
                warnModal.style.display = 'flex';
            } else if (warnModal) {
                warnModal.style.display = 'none';
            }

            // P&L cards
            var totalPnl = data.pnl.net_total !== undefined ? data.pnl.net_total : data.pnl.total;
            var totalEl = document.getElementById('total-pnl');
            totalEl.textContent = fmt(totalPnl);
            totalEl.className = 'value ' + (totalPnl >= 0 ? 'positive' : 'negative');
            
            var charges = data.pnl.brokerage || 0;
            document.getElementById('pnl-breakdown').textContent =
                'R: ' + fmt(data.pnl.realized) + ' | U: ' + fmt(data.pnl.unrealized) + ' | Charges: ' + fmt(charges);

            // Loss buffer
            var lossRemaining = data.limits.loss_remaining;
            var lossEl = document.getElementById('loss-remaining');
            lossEl.textContent = fmt(lossRemaining);
            lossEl.className = 'value ' + (lossRemaining > data.limits.daily_max_loss * 0.3 ? 'positive' : lossRemaining > 0 ? 'warning' : 'negative');
            document.getElementById('loss-limit-info').textContent = 'of ' + fmt(data.limits.daily_max_loss) + ' limit';
            var lossPct = Math.min(100, Math.max(0, data.limits.loss_used_pct));
            var lossBar = document.getElementById('loss-bar');
            lossBar.style.width = lossPct + '%';
            lossBar.style.background = lossPct > 70 ? '#f85149' : lossPct > 40 ? '#d29922' : '#3fb950';

            // Profit lock
            var plEl = document.getElementById('profit-lock-value');
            var plInfo = document.getElementById('profit-lock-info');
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
            var maxTrades = data.limits.max_trades_limit || 35;
            document.getElementById('trade-stats').textContent =
                data.trades.total + ' / ' + maxTrades + ' trades (W:' + data.trades.winners + ' L:' + data.trades.losers + ')';

            // Update chart trading status bar
            var ctNetPnl = document.getElementById('ct-net-pnl');
            if (ctNetPnl) {
                ctNetPnl.textContent = fmt(totalPnl);
                ctNetPnl.className = 'value ' + (totalPnl >= 0 ? 'positive' : 'negative');
                document.getElementById('ct-pnl-breakdown').textContent =
                    'R: ' + fmt(data.pnl.realized) + ' | U: ' + fmt(data.pnl.unrealized) + ' | Charges: ' + fmt(charges);
                
                var lossRemaining = data.limits.loss_remaining;
                document.getElementById('ct-loss-remaining').textContent = fmt(lossRemaining);
                
                var plInfo = document.getElementById('ct-profit-lock');
                if (data.profit_lock.active) {
                    plInfo.textContent = fmt(data.profit_lock.floor);
                    plInfo.className = 'positive';
                } else {
                    plInfo.textContent = fmt(data.profit_lock.distance);
                    plInfo.className = 'neutral';
                }

                // Update Trade limits counter
                document.getElementById('ct-trades-counter').textContent = 
                    data.trades.total + ' / ' + maxTrades + ' trades';
                
                // Update extend trade limit buttons state
                var extendBtn = document.getElementById('ct-extend-btn');
                var mainExtendBtn = document.getElementById('main-extend-btn');
                [extendBtn, mainExtendBtn].forEach(function(btn) {
                    if (btn) {
                        if (data.limits.trade_limit_extended) {
                            btn.textContent = 'Extended';
                            btn.disabled = true;
                            btn.style.opacity = '0.5';
                            btn.style.cursor = 'not-allowed';
                        } else {
                            btn.textContent = '+10 Trades';
                            btn.disabled = false;
                            btn.style.opacity = '1';
                            btn.style.cursor = 'pointer';
                        }
                    }
                });
            }

            // Trailing drawdown — new style
            if (data.trailing_drawdown.enabled) {
                var dd = data.trailing_drawdown;
                var hwm = dd.high_water_mark || 0;
                var threshold = data.profit_lock.threshold || 3000;
                var realized = data.pnl ? (data.pnl.net_realized !== undefined ? data.pnl.net_realized : data.pnl.realized) : 0;
                var total = data.pnl ? (data.pnl.net_total !== undefined ? data.pnl.net_total : data.pnl.total) : 0;
                var floor = dd.drawdown_limit > 0 ? hwm - dd.drawdown_limit : 0;
                var gap = total - floor;
                var active = hwm >= threshold;

                var badge = document.getElementById('dd-badge');
                var card = document.getElementById('dd-card');
                document.getElementById('dd-inactive').style.display = active ? 'none' : 'block';
                document.getElementById('dd-active').style.display = active ? 'block' : 'none';

                if (!active) {
                    var need = Math.max(0, threshold - realized);
                    document.getElementById('dd-need').textContent = fmt(need);
                    badge.textContent = '⬤ Inactive';
                    badge.style.background = '#21262d'; badge.style.color = '#484f58';
                    card.style.borderColor = '';
                } else {
                    document.getElementById('dd-hwm').textContent = fmt(hwm);
                    document.getElementById('dd-floor').textContent = fmt(floor);
                    document.getElementById('dd-floor-label').textContent = '▲ Floor ' + fmt(floor);
                    document.getElementById('dd-hwm-label').textContent = 'HWM ' + fmt(hwm) + ' ▲';
                    document.getElementById('dd-gap').textContent = fmt(Math.max(0, gap));

                    var danger = gap < dd.drawdown_limit * 0.2;
                    var warn   = gap < dd.drawdown_limit * 0.5;
                    var gapColor = danger ? '#f85149' : warn ? '#f0883e' : '#3fb950';
                    document.getElementById('dd-gap').style.color = gapColor;

                    // Bar: how much of the safe zone remains (gap / drawdown_limit)
                    var barPct = dd.drawdown_limit > 0 ? Math.min(100, Math.max(0, gap / dd.drawdown_limit * 100)) : 0;
                    var ddBar = document.getElementById('drawdown-bar');
                    ddBar.style.width = barPct + '%';
                    ddBar.style.background = gapColor;

                    badge.textContent = danger ? '⚠ Active' : '⬤ Active';
                    badge.style.background = danger ? '#2d1117' : '#1f2d1f';
                    badge.style.color = danger ? '#f85149' : '#3fb950';
                    badge.style.border = '1px solid ' + gapColor;
                    card.style.borderColor = danger ? '#f85149' : '';
                }
            }

            // Positions table
            updatePositions(data.positions || [], data.sl_tp_orders || {});

            // Pending orders
            updatePendingOrders(data.pending_orders || []);


            // Equity curve (fetched separately every 60s)
            if (data.equity_curve) updateEquityCharts(data.equity_curve);

            // Journal today tab
            if (data.trades && data.trades.history) {
                updateJournalToday(data.trades.history);
            }

            // Time
            document.getElementById('market-time').textContent = new Date().toLocaleTimeString('en-IN');

            // Sound/notification alerts
            checkAlerts(data);
        }

        // ── Positions Table with SL/TP inline ──────────────────────
        // Store position data so buttons can reference by index
        var _openPositions = [];

        function updatePositions(positions, slTpOrders) {
            var tbody = document.getElementById('positions-body');
            var open = positions.filter(function(p){ return p.netQty !== 0; });
            _openPositions = open;
            if (open.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#484f58;">No open positions</td></tr>';
                return;
            }
            var html = '';
            for (var i = 0; i < open.length; i++) {
                var p = open[i];
                var pnl = (p.realizedProfit || 0) + (p.unrealizedProfit || 0);
                var pnlClass = pnl >= 0 ? 'positive' : 'negative';
                var sid = (p.securityId || '') + '';
                var exSeg = p.exchangeSegment || '';
                var dir = p.netQty > 0 ? 1 : -1;
                var ltp = p.lastTradedPrice || 0;
                var qty = Math.abs(p.netQty || 0);

                // SL/TP info
                var sltp = slTpOrders[sid];
                var slText = '-';
                var tpText = '-';
                if (sltp && sltp.active) {
                    if (sltp.current_sl || sltp.stop_loss) {
                        slText = '<span class="negative">SL: ' + fmtDec(sltp.current_sl || sltp.stop_loss) + '</span>';
                        if (sltp.trailing) slText += ' <span style="color:#58a6ff;font-size:10px;">TSL</span>';
                    }
                    if (sltp.take_profit) {
                        tpText = '<span class="positive">TP: ' + fmtDec(sltp.take_profit) + '</span>';
                    }
                }

                html += '<tr data-pos-idx="' + i + '">';
                html += '<td>' + (p.tradingSymbol || sid) + '</td>';
                html += '<td style="color:' + (dir > 0 ? '#3fb950' : '#f85149') + ';">' + (p.netQty || 0) + '</td>';
                html += '<td>' + fmtDec(p.avgPrice || 0) + '</td>';
                html += '<td>' + fmtDec(ltp) + '</td>';
                html += '<td class="' + pnlClass + '">' + fmt(pnl) + '</td>';
                html += '<td style="font-size:12px;">' + slText + '<br>' + tpText + '</td>';

                // Quick SL/TP buttons - use data attributes, no inline handlers
                html += '<td><div class="quick-btns">';
                for (var si = 0; si < QUICK_SL_OFFSETS.length; si++) {
                    html += '<button class="btn-sl btn-xs" data-action="qsl" data-idx="' + i + '" data-offset="' + QUICK_SL_OFFSETS[si] + '">SL-' + QUICK_SL_OFFSETS[si] + '</button>';
                }
                for (var ti = 0; ti < QUICK_TP_OFFSETS.length; ti++) {
                    html += '<button class="btn-tp btn-xs" data-action="qtp" data-idx="' + i + '" data-offset="' + QUICK_TP_OFFSETS[ti] + '">TP+' + QUICK_TP_OFFSETS[ti] + '</button>';
                }
                html += '<button class="btn-xs" style="background:#1f6feb;color:white;" data-action="tsl" data-idx="' + i + '">TSL</button>';
                html += '</div></td>';

                // Actions
                html += '<td style="white-space:nowrap;">';
                html += '<button class="btn-sl btn-sm" data-action="set-sl" data-idx="' + i + '">SL</button> ';
                html += '<button class="btn-tp btn-sm" data-action="set-tp" data-idx="' + i + '">TP</button> ';
                html += '<button class="btn-neutral btn-sm" data-action="tsl" data-idx="' + i + '">TSL</button> ';
                html += '<button class="btn-danger btn-sm" data-action="exit-mkt" data-idx="' + i + '">EXIT MKT</button> ';
                html += '<button class="btn-sm" style="background:#1a3a5c;color:#58a6ff;border:1px solid #1f6feb;" data-action="show-exit-form" data-idx="' + i + '">EXIT...</button>';
                html += '</td>';
                html += '</tr>';

                // Inline exit form (partial / target)
                html += '<tr id="exit-row-' + sid + '" style="display:none;"><td colspan="8" style="padding:4px 8px;background:#0d1117;border-bottom:1px solid #21262d;">';
                html += '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;">';
                html += '<span style="color:#8b949e;">Qty:</span>';
                html += '<button class="btn-xs btn-neutral" data-action="pct-qty" data-idx="' + i + '" data-pct="25">25%</button>';
                html += '<button class="btn-xs btn-neutral" data-action="pct-qty" data-idx="' + i + '" data-pct="50">50%</button>';
                html += '<button class="btn-xs btn-neutral" data-action="pct-qty" data-idx="' + i + '" data-pct="75">75%</button>';
                html += '<button class="btn-xs btn-neutral" data-action="pct-qty" data-idx="' + i + '" data-pct="100">100%</button>';
                html += '<input id="exit-qty-' + sid + '" type="number" value="' + qty + '" min="1" style="width:60px;font-size:11px;" class="form-input">';
                html += '<span style="color:#8b949e;margin-left:4px;">@ </span>';
                html += '<input id="exit-price-' + sid + '" type="number" step="0.05" value="' + ltp.toFixed(2) + '" style="width:75px;font-size:11px;" class="form-input" placeholder="Price">';
                html += '<button class="btn-sm btn-sl" data-action="exit-lmt" data-idx="' + i + '">EXIT LMT</button>';
                html += '<button class="btn-sm btn-danger" data-action="exit-partial-mkt" data-idx="' + i + '">EXIT MKT</button>';
                html += '<button class="btn-xs btn-neutral" data-action="hide-exit-form" data-idx="' + i + '" style="margin-left:4px;">✕</button>';
                html += '</div>';
                html += '</td></tr>';

                // Trailing SL inline form row (hidden by default)
                html += '<tr id="tsl-row-' + sid + '" style="display:none;"><td colspan="8">';
                html += '<div class="inline-sltp">';
                html += '<span style="font-size:12px;color:#8b949e;">Trailing SL:</span>';
                html += '<input id="tsl-price-' + sid + '" placeholder="SL Price" type="number" step="0.05" value="' + ltp.toFixed(2) + '">';
                html += '<input id="tsl-trail-' + sid + '" placeholder="Trail pts" type="number" step="0.05" value="10">';
                html += '<input id="tsl-trigger-' + sid + '" placeholder="Start after" type="number" step="0.05" value="20">';
                html += '<button class="btn-sl btn-sm" data-action="set-tsl" data-idx="' + i + '">Set TSL</button>';
                html += '<button class="btn-neutral btn-sm" data-action="hide-tsl" data-idx="' + i + '">Cancel</button>';
                html += '</div>';
                html += '</td></tr>';
            }
            tbody.innerHTML = html;
        }

        // Event delegation for all position table buttons
        document.getElementById('positions-body').addEventListener('click', function(e) {
            var btn = e.target.closest('button[data-action]');
            if (!btn) return;
            var action = btn.getAttribute('data-action');
            var idx = parseInt(btn.getAttribute('data-idx'));
            var p = _openPositions[idx];
            if (!p) return;

            var sid = (p.securityId || '') + '';
            var exSeg = p.exchangeSegment || '';
            var prodType = p.productType || '';
            var qty = Math.abs(p.netQty || 0);
            var dir = p.netQty > 0 ? 1 : -1;

            if (action === 'qsl') {
                quickSL(sid, exSeg, dir, parseInt(btn.getAttribute('data-offset')));
            } else if (action === 'qtp') {
                quickTP(sid, exSeg, dir, parseInt(btn.getAttribute('data-offset')));
            } else if (action === 'tsl') {
                showTSLForm(sid);
            } else if (action === 'set-sl') {
                promptSL(sid);
            } else if (action === 'set-tp') {
                promptTP(sid);
            } else if (action === 'exit-mkt') {
                exitPositionMkt(sid, exSeg, prodType, qty, dir);
            } else if (action === 'show-exit-form') {
                showExitForm(sid);
            } else if (action === 'hide-exit-form') {
                hideExitForm(sid);
            } else if (action === 'pct-qty') {
                var pct = parseInt(btn.getAttribute('data-pct'));
                var lotSize = _ocLotSize || 25;
                var raw = qty * pct / 100;
                var lots = Math.max(1, Math.round(raw / lotSize));
                document.getElementById('exit-qty-' + sid).value = lots * lotSize;
            } else if (action === 'exit-lmt') {
                exitPositionLmt(sid, exSeg, prodType, dir);
            } else if (action === 'exit-partial-mkt') {
                exitPositionPartialMkt(sid, exSeg, prodType, dir);
            } else if (action === 'set-tsl') {
                setTrailingSL(sid);
            } else if (action === 'hide-tsl') {
                hideTSLForm(sid);
            }
        });

        // ── Equity Curve ─────────────────────────────────────────────
        var _eqChart = null;
        var _eqData = [];  // [{time, pnl, cumulative, win}]

        function _buildEqChartConfig(canvasId, height) {
            return {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        {
                            label: 'Realized P&L',
                            data: [],
                            borderColor: '#58a6ff',
                            borderWidth: 2,
                            fill: false,
                            tension: 0.3,
                            pointRadius: 5,
                            pointBackgroundColor: [],
                            pointBorderColor: [],
                        },
                        {
                            label: 'Lockout Floor',
                            data: [],
                            borderColor: '#f85149',
                            borderWidth: 1,
                            borderDash: [6, 3],
                            fill: false,
                            pointRadius: 0,
                            tension: 0,
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: {
                            ticks: { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 12, maxRotation: 0 },
                            grid: { color: '#161b22' },
                            title: { display: false }
                        },
                        y: {
                            ticks: { color: '#8b949e', font: { size: 10 }, callback: function(v){ return '\\u20B9' + v.toLocaleString('en-IN'); } },
                            grid: { color: '#21262d' }
                        }
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function(ctx) {
                                    if (ctx.datasetIndex === 0) {
                                        var pt = _eqData[ctx.dataIndex];
                                        if (!pt) return '';
                                        var sign = pt.pnl >= 0 ? '+' : '';
                                        return 'Cumul: \\u20B9' + ctx.parsed.y.toLocaleString('en-IN') + '  (trade: ' + sign + pt.pnl.toFixed(0) + ')';
                                    }
                                    return 'Floor: \\u20B9' + ctx.parsed.y.toLocaleString('en-IN');
                                }
                            }
                        },
                        zoom: {
                            zoom: {
                                wheel: { enabled: true, speed: 0.1 },
                                pinch: { enabled: true },
                                mode: 'x',
                            },
                            pan: {
                                enabled: true,
                                mode: 'x',
                            },
                            limits: {
                                x: { minRange: 2 }
                            }
                        }
                    },
                    animation: { duration: 0 }
                }
            };
        }

        function initEquityCharts() {
            if (typeof Chart === 'undefined') return;
            var c1 = document.getElementById('equity-chart');
            if (c1 && !_eqChart) {
                _eqChart = new Chart(c1, _buildEqChartConfig('equity-chart', 180));
                c1.addEventListener('dblclick', function(){ _eqChart.resetZoom(); });
            }
        }

        function _applyEqData(chart, emptyId, summaryId, trades, floor) {
            var empty = document.getElementById(emptyId);
            var sumEl = document.getElementById(summaryId);
            if (!chart) return;
            if (!trades || trades.length === 0) {
                if (empty) empty.style.display = 'flex';
                chart.data.labels = [];
                chart.data.datasets[0].data = [];
                chart.data.datasets[1].data = [];
                chart.update('none');
                return;
            }
            if (empty) empty.style.display = 'none';

            var labels = [], cumValues = [], colors = [], borders = [], floorLine = [];
            var cumul = 0;
            var wins = 0, losses = 0;
            trades.forEach(function(t) {
                var pnl = t.pnl || 0;
                cumul += pnl;
                var timeStr = (t.time || t.exit_time || '').substring(11, 16);
                labels.push(timeStr);
                cumValues.push(Math.round(cumul));
                var win = pnl >= 0;
                if (win) wins++; else losses++;
                colors.push(win ? '#3fb950' : '#f85149');
                borders.push(win ? '#3fb950' : '#f85149');
                floorLine.push(floor > 0 ? Math.round(floor) : null);
            });

            chart.data.labels = labels;
            chart.data.datasets[0].data = cumValues;
            chart.data.datasets[0].pointBackgroundColor = colors;
            chart.data.datasets[0].pointBorderColor = borders;
            chart.data.datasets[1].data = floor > 0 ? floorLine : [];
            chart.update('none');

            if (sumEl) {
                var total = wins + losses;
                sumEl.textContent = wins + 'W / ' + losses + 'L  (' + (total > 0 ? Math.round(wins/total*100) : 0) + '%)';
                sumEl.style.color = cumul >= 0 ? '#3fb950' : '#f85149';
            }
        }

        function updateEquityCharts(eqData) {
            if (!eqData) return;
            var trades = eqData.trades || [];
            var floor = eqData.floor || 0;
            _eqData = trades;
            _applyEqData(_eqChart, 'eq-empty', 'eq-summary', trades, floor);
        }

        // legacy stub — no-op (pnl-chart canvas removed)
        function initPnlChart() {}
        function updatePnlChart() {}

        // ── Pending Orders ──────────────────────────────────────────
        function updatePendingOrders(orders) {
            var tbody = document.getElementById('pending-orders-body');
            var countEl = document.getElementById('pending-orders-count');
            if (!orders || orders.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#484f58;">No pending orders</td></tr>';
                if (countEl) countEl.textContent = '';
                return;
            }
            if (countEl) countEl.textContent = orders.length + ' pending';
            var html = '';
            for (var i = 0; i < orders.length; i++) {
                var o = orders[i];
                var sideColor = o.transactionType === 'BUY' ? '#3fb950' : '#f85149';
                html += '<tr>';
                html += '<td>' + (o.tradingSymbol || o.securityId) + '</td>';
                html += '<td style="color:' + sideColor + ';font-weight:600;">' + o.transactionType + '</td>';
                html += '<td>' + o.quantity;
                if (o.tradedQuantity > 0) html += ' <span style="color:#8b949e;font-size:11px;">(' + o.tradedQuantity + ' filled)</span>';
                html += '</td>';
                html += '<td>' + o.orderType + '</td>';
                html += '<td>' + (o.price > 0 ? fmtDec(o.price) : '-') + '</td>';
                html += '<td>' + (o.triggerPrice > 0 ? fmtDec(o.triggerPrice) : '-') + '</td>';
                html += '<td><span style="color:#d29922;">' + o.orderStatus + '</span></td>';
                html += '<td>';
                html += '<button class="btn-neutral btn-sm" onclick="showModifyOrder(\\'' + o.orderId + '\\',' + i + ')">Modify</button> ';
                html += '<button class="btn-danger btn-sm" onclick="cancelPendingOrder(\\'' + o.orderId + '\\')">Cancel</button>';
                html += '</td>';
                html += '</tr>';
                // Inline modify row (hidden)
                html += '<tr id="modify-row-' + o.orderId + '" style="display:none;"><td colspan="8">';
                html += '<div style="display:flex;gap:10px;align-items:center;padding:8px 0;">';
                html += '<span style="font-size:12px;color:#8b949e;">Modify:</span>';
                html += '<input id="mod-price-' + o.orderId + '" type="number" step="0.05" placeholder="Price" value="' + (o.price || '') + '" class="form-input" style="width:100px;">';
                html += '<input id="mod-trigger-' + o.orderId + '" type="number" step="0.05" placeholder="Trigger" value="' + (o.triggerPrice || '') + '" class="form-input" style="width:100px;">';
                html += '<button class="btn-sl btn-sm" onclick="submitModifyOrder(\\'' + o.orderId + '\\',\\'' + o.orderType + '\\',' + o.quantity + ')">Save</button>';
                html += '<button class="btn-neutral btn-sm" onclick="hideModifyOrder(\\'' + o.orderId + '\\')">Cancel</button>';
                html += '</div></td></tr>';
            }
            tbody.innerHTML = html;
        }

        function showModifyOrder(orderId) {
            var row = document.getElementById('modify-row-' + orderId);
            if (row) row.style.display = '';
        }
        function hideModifyOrder(orderId) {
            var row = document.getElementById('modify-row-' + orderId);
            if (row) row.style.display = 'none';
        }
        function submitModifyOrder(orderId, orderType, qty) {
            var price = parseFloat(document.getElementById('mod-price-' + orderId).value) || 0;
            var trigger = parseFloat(document.getElementById('mod-trigger-' + orderId).value) || 0;
            fetch('/api/order/modify', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ order_id: orderId, order_type: orderType, quantity: qty, price: price, trigger_price: trigger })
            })
            .then(function(r){ return r.json(); })
            .then(function(res) {
                if (res.status === 'ok') {
                    playAlert('order');
                    showToast('Order modified', 'success');
                    hideModifyOrder(orderId);
                } else {
                    showToast('Modify failed: ' + (res.message || ''), 'error');
                }
            })
            .catch(function(e) { showToast('Modify error: ' + e, 'error'); });
        }
        function cancelPendingOrder(orderId) {
            if (!confirm('Cancel this order?')) return;
            fetch('/api/order/cancel_pending', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ order_id: orderId })
            })
            .then(function(r){ return r.json(); })
            .then(function(res) {
                if (res.status === 'ok') {
                    playAlert('order');
                    showToast('Order cancelled', 'success');
                } else {
                    showToast('Cancel failed: ' + (res.message || ''), 'error');
                }
            })
            .catch(function(e) { showToast('Cancel error: ' + e, 'error'); });
        }

        // ── Recent Orders (Rejected / Executed / Cancelled) ─────────
        function updateRecentOrders(orders) {
            var tbody = document.getElementById('recent-orders-body');
            var countEl = document.getElementById('recent-orders-count');
            if (!tbody) return;
            if (!orders || orders.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#484f58;">No recent orders</td></tr>';
                if (countEl) countEl.textContent = '';
                return;
            }
            var rejected = 0;
            for (var j = 0; j < orders.length; j++) { if (orders[j].orderStatus === 'REJECTED') rejected++; }
            if (countEl) {
                countEl.textContent = orders.length + ' order' + (orders.length > 1 ? 's' : '') +
                    (rejected > 0 ? ' (' + rejected + ' rejected)' : '');
            }
            var html = '';
            for (var i = 0; i < orders.length; i++) {
                var o = orders[i];
                var sideColor = o.transactionType === 'BUY' ? '#3fb950' : '#f85149';
                var statusColor = '#8b949e';
                if (o.orderStatus === 'REJECTED') statusColor = '#f85149';
                else if (o.orderStatus === 'TRADED') statusColor = '#3fb950';
                else if (o.orderStatus === 'CANCELLED') statusColor = '#d29922';
                html += '<tr>';
                html += '<td>' + (o.tradingSymbol || o.securityId) + '</td>';
                html += '<td style="color:' + sideColor + ';font-weight:600;">' + o.transactionType + '</td>';
                html += '<td>' + o.quantity;
                if (o.tradedQuantity > 0) html += ' <span style="color:#8b949e;font-size:11px;">(' + o.tradedQuantity + ' filled)</span>';
                html += '</td>';
                html += '<td>' + o.orderType + '</td>';
                html += '<td>' + (o.price > 0 ? fmtDec(o.price) : 'MKT') + '</td>';
                html += '<td><span style="color:' + statusColor + ';font-weight:600;">' + o.orderStatus + '</span></td>';
                html += '<td style="font-size:12px;color:#8b949e;max-width:200px;overflow:hidden;text-overflow:ellipsis;">';
                if (o.orderStatus === 'REJECTED' && o.rejectedReason) {
                    html += '<span style="color:#f85149;">' + o.rejectedReason + '</span>';
                } else if (o.updateTime) {
                    html += o.updateTime;
                } else {
                    html += '-';
                }
                html += '</td>';
                html += '</tr>';
            }
            tbody.innerHTML = html;
        }

        // ── Trade Journal ───────────────────────────────────────────
        // switchJournalTab is defined in <head> so tabs work even if this script errors

        function updateJournalToday(trades) {
            var tbody = document.getElementById('journal-today-body');
            if (!tbody) return;
            if (!trades || trades.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#484f58;">No trades today</td></tr>';
                return;
            }
            var html = '';
            for (var i = 0; i < trades.length; i++) {
                var t = trades[i];
                var pnlClass = t.pnl >= 0 ? 'positive' : 'negative';
                html += '<tr>';
                html += '<td>' + (t.time_of_day || new Date(t.time * 1000).toLocaleTimeString('en-IN')) + '</td>';
                html += '<td>' + (t.security_id || '-') + '</td>';
                html += '<td>' + (t.type || t.trade_type || '-') + '</td>';
                html += '<td>' + (t.quantity || '-') + '</td>';
                html += '<td class="' + pnlClass + '">' + fmt(t.pnl) + '</td>';
                html += '</tr>';
            }
            tbody.innerHTML = html;
        }

        // switchOrderTab is defined in <head> so tabs work even if this script errors

        // ── Instrument Search ──────────────────────────────────────
        var searchTimeout = null;
        var _searchResults = [];  // Store results so we can reference by index

        document.getElementById('instrument-search').addEventListener('input', function() {
            clearTimeout(searchTimeout);
            var q = this.value.trim();
            if (q.length < 2) {
                document.getElementById('search-results').style.display = 'none';
                return;
            }
            searchTimeout = setTimeout(function() {
                var url = '/api/instruments/search?q=' + encodeURIComponent(q) + '&limit=15';
                fetch(url)
                    .then(function(r){
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        return r.json();
                    })
                    .then(function(results) {
                        var container = document.getElementById('search-results');
                        if (!results || results.length === 0) {
                            container.innerHTML = '<div class="search-item"><div class="meta">No results found</div></div>';
                            container.style.display = 'block';
                            _searchResults = [];
                            return;
                        }
                        _searchResults = results;
                        var html = '';
                        for (var i = 0; i < results.length; i++) {
                            var r = results[i];
                            html += '<div class="search-item" data-idx="' + i + '">';
                            html += '<div><span class="sym">' + r.custom_symbol + '</span></div>';
                            html += '<div class="meta">' + r.exchange + ' | Lot: ' + r.lot_size + ' | ' + r.instrument_type + '</div>';
                            html += '</div>';
                        }
                        container.innerHTML = html;
                        container.style.display = 'block';
                    })
                    .catch(function(err) {
                        console.error('Search failed:', err);
                        var container = document.getElementById('search-results');
                        container.innerHTML = '<div class="search-item"><div class="meta" style="color:#f85149;">Search error: ' + err.message + '</div></div>';
                        container.style.display = 'block';
                    });
            }, 300);
        });

        // Handle search result clicks via event delegation
        document.getElementById('search-results').addEventListener('click', function(e) {
            var item = e.target.closest('.search-item');
            if (!item) return;
            var idx = parseInt(item.getAttribute('data-idx'));
            if (idx >= 0 && idx < _searchResults.length) {
                selectInstrument(_searchResults[idx]);
            }
        });

        // Close all search results when clicking elsewhere
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.search-wrapper')) {
                document.querySelectorAll('.search-results').forEach(function(el) {
                    el.style.display = 'none';
                });
            }
        });

        function selectInstrument(inst) {
            document.getElementById('instrument-search').value = inst.custom_symbol;
            document.getElementById('order-security-id').value = inst.security_id;
            document.getElementById('order-exchange-segment').value = inst.exchange_segment;
            document.getElementById('order-lot-size').value = inst.lot_size;
            document.getElementById('order-tick-size').value = inst.tick_size;
            document.getElementById('search-results').style.display = 'none';
            document.getElementById('selected-instrument').style.display = 'block';
            document.getElementById('selected-instrument').textContent =
                inst.exchange + ' | ' + inst.instrument_type + ' | Lot size: ' + inst.lot_size + ' | ID: ' + inst.security_id;

            // Fetch LTP and trigger auto-calc
            fetch('/api/ltp/' + inst.security_id + '?exchange_segment=' + inst.exchange_segment)
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.ltp) {
                        document.getElementById('order-price').value = d.ltp;
                        triggerAutoCalc();
                    }
                });
        }

        // ── Auto Position Size Calculator ────────────────────────────
        var _calcTimeout = null;

        function triggerAutoCalc() {
            clearTimeout(_calcTimeout);
            _calcTimeout = setTimeout(doAutoCalc, 400);
        }

        function doAutoCalc() {
            var secId = document.getElementById('order-security-id').value;
            if (!secId) return;

            var price = parseFloat(document.getElementById('order-price').value) || 0;
            var sl = parseFloat(document.getElementById('calc-sl').value) || 0;
            var risk = parseFloat(document.getElementById('calc-risk').value) || 0;

            if (price <= 0 || sl <= 0 || risk <= 0 || price === sl) return;

            fetch('/api/order/calculate_size', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    risk_amount: risk,
                    entry_price: price,
                    sl_price: sl,
                    security_id: secId,
                    transaction_type: document.getElementById('order-txn-type').value,
                    product_type: document.getElementById('order-product-type').value
                })
            })
            .then(function(r){ return r.json(); })
            .then(function(data) {
                if (data.error) return;
                document.getElementById('calc-qty').textContent = data.quantity;
                document.getElementById('calc-lots').textContent = data.num_lots != null ? data.num_lots + ' lot(s) x ' + data.lot_size : '';
                document.getElementById('calc-risk-unit').textContent = fmtDec(data.risk_per_unit);
                document.getElementById('calc-actual-risk').textContent = fmt(data.actual_risk);
                var feas = document.getElementById('calc-feasibility');
                if (!data.feasible) {
                    feas.innerHTML = '<span class="negative">Blocked: ' + data.feasibility_reason + '</span>';
                } else {
                    feas.innerHTML = '<span class="positive">Risk OK</span>';
                }
                document.getElementById('order-quantity').value = data.quantity;
            })
            .catch(function(err) { console.error('Auto-calc error:', err); });
        }

        // Attach auto-calc listeners to all .calc-trigger inputs
        document.querySelectorAll('.calc-trigger').forEach(function(el) {
            el.addEventListener('input', triggerAutoCalc);
        });
        document.getElementById('order-txn-type').addEventListener('change', triggerAutoCalc);
        document.getElementById('order-product-type').addEventListener('change', triggerAutoCalc);

        // ── Place Order ────────────────────────────────────────────
        function placeOrder(side) {
            var secId = document.getElementById('order-security-id').value;
            var qty = parseInt(document.getElementById('order-quantity').value);
            if (!secId) { showToast('Select an instrument first', 'warning'); return; }
            if (!qty || qty <= 0) { showToast('Enter or calculate quantity first', 'warning'); return; }

            var orderType = document.getElementById('order-type').value;
            var price = parseFloat(document.getElementById('order-price').value) || 0;

            var triggerPrice = parseFloat(document.getElementById('order-trigger-price').value) || 0;

            if (orderType === 'LIMIT' && price <= 0) { showToast('Enter a price for limit order', 'warning'); return; }
            if ((orderType === 'STOP_LOSS' || orderType === 'STOP_LOSS_MARKET') && triggerPrice <= 0) { showToast('Enter a trigger price for stop-loss order', 'warning'); return; }
            if (orderType === 'STOP_LOSS' && price <= 0) { showToast('Enter a limit price for stop-limit order', 'warning'); return; }

            var payload = {
                security_id: secId,
                exchange_segment: document.getElementById('order-exchange-segment').value,
                transaction_type: side,
                order_type: orderType,
                product_type: document.getElementById('order-product-type').value,
                quantity: qty,
                price: price,
                trigger_price: parseFloat(document.getElementById('order-trigger-price').value) || 0,
                sl_price: parseFloat(document.getElementById('calc-sl').value) || 0,
                tp_price: parseFloat(document.getElementById('calc-tp').value) || 0
            };

            var label = side + ' ' + qty + ' @ ' + orderType;
            if (!confirm('Place order: ' + label + '?')) return;

            fetch('/api/order/place', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
            .then(function(r){ return r.json(); })
            .then(function(result) {
                if (result.status === 'BLOCKED') {
                    playAlert('error');
                    showToast('Order BLOCKED: ' + result.reason, 'error');
                } else if (result.status === 'REJECTED') {
                    playAlert('error');
                    showToast('Order REJECTED by Dhan: ' + (result.reason || 'Unknown reason'), 'error');
                } else if (result.status === 'error') {
                    playAlert('error');
                    showToast('Order failed: ' + result.message, 'error');
                } else {
                    var statusInfo = result.orderStatus ? ' (' + result.orderStatus + ')' : '';
                    playAlert('order');
                    showToast('Order placed' + statusInfo + ': ' + label, 'success');
                    // Clear form
                    document.getElementById('order-quantity').value = '';
                }
            })
            .catch(function(err) {
                showToast('Network error: ' + err, 'error');
            });
        }

        // ── SL/TP Functions ────────────────────────────────────────
        function promptSL(sid) {
            var price = prompt('Enter Stop Loss price:');
            if (price && !isNaN(price)) {
                fetch('/api/sl', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({security_id: sid, price: parseFloat(price)})
                }).then(function(r){ return r.json(); }).then(function(d) {
                    showToast('SL set at \\u20B9' + parseFloat(price).toFixed(2), 'success');
                });
            }
        }

        function promptTP(sid) {
            var price = prompt('Enter Take Profit price:');
            if (price && !isNaN(price)) {
                fetch('/api/tp', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({security_id: sid, price: parseFloat(price)})
                }).then(function(r){ return r.json(); }).then(function(d) {
                    showToast('TP set at \\u20B9' + parseFloat(price).toFixed(2), 'success');
                });
            }
        }

        function quickSL(sid, exSeg, direction, offset) {
            fetch('/api/sl/quick', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({security_id: sid, exchange_segment: exSeg, direction: direction, offset_points: offset, mode: 'sl'})
            }).then(function(r){ return r.json(); }).then(function(d) {
                if (d.error) { showToast(d.error, 'error'); return; }
                showToast('SL set at \\u20B9' + d.price.toFixed(2) + ' (LTP: \\u20B9' + d.ltp.toFixed(2) + ')', 'success');
            });
        }

        function quickTP(sid, exSeg, direction, offset) {
            fetch('/api/sl/quick', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({security_id: sid, exchange_segment: exSeg, direction: direction, offset_points: offset, mode: 'tp'})
            }).then(function(r){ return r.json(); }).then(function(d) {
                if (d.error) { showToast(d.error, 'error'); return; }
                showToast('TP set at \\u20B9' + d.price.toFixed(2) + ' (LTP: \\u20B9' + d.ltp.toFixed(2) + ')', 'success');
            });
        }

        function showTSLForm(sid, ltp) {
            var row = document.getElementById('tsl-row-' + sid);
            if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
        }
        function hideTSLForm(sid) {
            var row = document.getElementById('tsl-row-' + sid);
            if (row) row.style.display = 'none';
        }

        function setTrailingSL(sid) {
            var price = parseFloat(document.getElementById('tsl-price-' + sid).value) || 0;
            var trail = parseFloat(document.getElementById('tsl-trail-' + sid).value) || 0;
            var trigger = parseFloat(document.getElementById('tsl-trigger-' + sid).value) || 0;
            if (price <= 0) { showToast('Enter SL price', 'warning'); return; }
            fetch('/api/sl', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({security_id: sid, price: price, trailing: true, trail_points: trail, trail_trigger: trigger})
            }).then(function(r){ return r.json(); }).then(function(d) {
                showToast('Trailing SL set at \\u20B9' + price.toFixed(2) + ' (trail: ' + trail + 'pts)', 'success');
                hideTSLForm(sid);
            });
        }

        function exitPosition(sid, exSeg, prodType, qty, direction) {
            exitPositionMkt(sid, exSeg, prodType, qty, direction);
        }

        function showExitForm(sid) {
            document.getElementById('exit-row-' + sid).style.display = 'table-row';
        }
        function hideExitForm(sid) {
            document.getElementById('exit-row-' + sid).style.display = 'none';
        }

        function _placeExitOrder(sid, exSeg, prodType, qty, direction, orderType, price, fullQty) {
            var txn = direction > 0 ? 'SELL' : 'BUY';
            var isFullExit = !fullQty || qty >= fullQty;
            // Only cancel the exchange SL when closing the full position.
            // A partial exit still needs the SL protecting the remaining size.
            if (isFullExit) {
                fetch('/api/order/cancel_sl/' + encodeURIComponent(sid), { method: 'POST' })
                    .catch(function(){})
                    .finally(function() { _doPlaceExitOrder(sid, exSeg, prodType, qty, txn, orderType, price, fullQty); });
            } else {
                _doPlaceExitOrder(sid, exSeg, prodType, qty, txn, orderType, price, fullQty);
            }
        }
        function _doPlaceExitOrder(sid, exSeg, prodType, qty, txn, orderType, price, fullQty) {
            fetch('/api/order/place', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    security_id: sid,
                    exchange_segment: exSeg,
                    transaction_type: txn,
                    quantity: qty,
                    order_type: orderType,
                    product_type: prodType,
                    price: orderType === 'LIMIT' ? price : 0
                })
            }).then(function(r){ return r.json(); }).then(function(d) {
                if (d.status === 'ok' || d.orderId) {
                    playAlert('order');
                    showToast(txn + ' ' + qty + ' @ ' + (orderType === 'LIMIT' ? '\\u20B9' + price.toFixed(2) + ' LMT' : 'MKT') + ' sent', 'success');
                    hideExitForm(sid);
                    // Partial exit: replace exchange SL with correct remaining quantity
                    if (fullQty && qty < fullQty) {
                        var remaining = fullQty - qty;
                        fetch('/api/order/replace_sl/' + encodeURIComponent(sid), {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ remaining_qty: remaining })
                        }).catch(function(){});
                    }
                    // Capture exit screenshot for journal if this is a tracked sell leg
                    var exitPx = orderType === 'LIMIT' ? price : 0;
                    closeJournalEntry(String(sid), exitPx, 0, null);
                } else {
                    showToast('Exit failed: ' + (d.message || d.remarks || ''), 'error');
                }
            }).catch(function(e) { showToast('Network error: ' + e, 'error'); });
        }

        function exitPositionMkt(sid, exSeg, prodType, qty, direction) {
            _placeExitOrder(sid, exSeg, prodType, qty, direction, 'MARKET', 0, qty);
        }

        function exitPositionLmt(sid, exSeg, prodType, direction) {
            var pos    = _openPositions.find(function(p){ return String(p.securityId) === String(sid); });
            var fullQty = pos ? Math.abs(pos.netQty || 0) : 0;
            var qty   = parseInt(document.getElementById('exit-qty-' + sid).value) || 0;
            var price = parseFloat(document.getElementById('exit-price-' + sid).value) || 0;
            if (!qty || qty <= 0) { showToast('Enter quantity', 'warning'); return; }
            if (!price || price <= 0) { showToast('Enter price', 'warning'); return; }
            _placeExitOrder(sid, exSeg, prodType, qty, direction, 'LIMIT', price, fullQty);
        }

        function exitPositionPartialMkt(sid, exSeg, prodType, direction) {
            var pos    = _openPositions.find(function(p){ return String(p.securityId) === String(sid); });
            var fullQty = pos ? Math.abs(pos.netQty || 0) : 0;
            var qty = parseInt(document.getElementById('exit-qty-' + sid).value) || 0;
            if (!qty || qty <= 0) { showToast('Enter quantity', 'warning'); return; }
            _placeExitOrder(sid, exSeg, prodType, qty, direction, 'MARKET', 0, fullQty);
        }

        function exitAllPositions() {
            if (confirm('EXIT ALL POSITIONS? This will close everything at market.')) {
                fetch('/api/exit_all', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                }).then(function(r){ return r.json(); }).then(function(d) {
                    if (d.status === 'ok') {
                        playAlert('order');
                        showToast('Closed ' + d.closed_positions + ' positions, cancelled ' + d.cancelled_orders + ' orders', 'success');
                    } else {
                        showToast('Exit all failed: ' + (d.message || ''), 'error');
                    }
                });
            }
        }

        // ── Token Update ─────────────────────────────────────────────
        function updateToken() {
            var token = document.getElementById('new-token-input').value.trim();
            if (!token) { showToast('Please paste a token first', 'warning'); return; }
            if (!confirm('Update the Dhan access token?')) return;
            fetch('/api/update_token', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ access_token: token })
            })
            .then(function(r){ return r.json(); })
            .then(function(res) {
                if (res.status === 'ok') {
                    playAlert('order');
                    showToast('Token updated successfully!', 'success');
                    document.getElementById('new-token-input').value = '';
                } else {
                    showToast('Token update failed: ' + (res.message || ''), 'error');
                }
            })
            .catch(function(e) { showToast('Error: ' + e, 'error'); });
        }

        // ── Spread Instrument Search ─────────────────────────────────
        var _spreadSellResults = [];
        var _spreadBuyResults = [];

        function setupSpreadSearch(inputId, resultsId, resultsArr, setterFn) {
            var input = document.getElementById(inputId);
            var container = document.getElementById(resultsId);
            if (!input || !container) return;

            var timeout = null;
            input.addEventListener('input', function() {
                clearTimeout(timeout);
                var q = this.value.trim();
                if (q.length < 2) { container.style.display = 'none'; return; }
                timeout = setTimeout(function() {
                    fetch('/api/instruments/search?q=' + encodeURIComponent(q) + '&limit=15')
                        .then(function(r){ return r.json(); })
                        .then(function(results) {
                            if (!results || results.length === 0) {
                                container.innerHTML = '<div class="search-item"><div class="meta">No results</div></div>';
                                container.style.display = 'block';
                                return;
                            }
                            resultsArr.length = 0;
                            for (var i = 0; i < results.length; i++) resultsArr.push(results[i]);
                            var html = '';
                            for (var i = 0; i < results.length; i++) {
                                var r = results[i];
                                html += '<div class="search-item" data-idx="' + i + '">';
                                html += '<div><span class="sym">' + r.custom_symbol + '</span></div>';
                                html += '<div class="meta">' + r.exchange + ' | Lot: ' + r.lot_size + ' | ' + r.instrument_type + '</div>';
                                html += '</div>';
                            }
                            container.innerHTML = html;
                            container.style.display = 'block';
                        })
                        .catch(function(err) { console.error('Search error:', err); });
                }, 300);
            });

            container.addEventListener('click', function(e) {
                var item = e.target.closest('.search-item');
                if (!item) return;
                var idx = parseInt(item.getAttribute('data-idx'));
                if (idx >= 0 && idx < resultsArr.length) {
                    setterFn(resultsArr[idx]);
                    container.style.display = 'none';
                }
            });
        }

        function selectSellInstrument(inst) {
            document.getElementById('spread-sell-search').value = inst.custom_symbol;
            document.getElementById('spread-sell-id').value = inst.security_id;
            document.getElementById('spread-sell-exseg').value = inst.exchange_segment;
            document.getElementById('spread-sell-lot').value = inst.lot_size;
            var info = document.getElementById('spread-sell-info');
            info.style.display = 'block';
            info.textContent = inst.exchange + ' | ' + inst.instrument_type + ' | Lot: ' + inst.lot_size + ' | ID: ' + inst.security_id;
            fetch('/api/ltp/' + inst.security_id + '?exchange_segment=' + inst.exchange_segment)
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.ltp) {
                        document.getElementById('spread-sell-price').value = d.ltp;
                        triggerSpreadCalc();
                    }
                });
        }

        function selectBuyInstrument(inst) {
            document.getElementById('spread-buy-search').value = inst.custom_symbol;
            document.getElementById('spread-buy-id').value = inst.security_id;
            document.getElementById('spread-buy-exseg').value = inst.exchange_segment;
            document.getElementById('spread-buy-lot').value = inst.lot_size;
            var info = document.getElementById('spread-buy-info');
            info.style.display = 'block';
            info.textContent = inst.exchange + ' | ' + inst.instrument_type + ' | Lot: ' + inst.lot_size + ' | ID: ' + inst.security_id;
        }

        setupSpreadSearch('spread-sell-search', 'spread-sell-results', _spreadSellResults, selectSellInstrument);
        setupSpreadSearch('spread-buy-search', 'spread-buy-results', _spreadBuyResults, selectBuyInstrument);

        // ── Spread Auto-Calc ─────────────────────────────────────────
        var _spreadCalcTimeout = null;

        function triggerSpreadCalc() {
            clearTimeout(_spreadCalcTimeout);
            _spreadCalcTimeout = setTimeout(doSpreadCalc, 400);
        }

        function doSpreadCalc() {
            var secId = document.getElementById('spread-sell-id').value;
            if (!secId) return;

            var price = parseFloat(document.getElementById('spread-sell-price').value) || 0;
            var sl = parseFloat(document.getElementById('spread-sell-sl').value) || 0;
            var risk = parseFloat(document.getElementById('spread-risk').value) || 0;

            if (price <= 0 || sl <= 0 || risk <= 0 || price === sl) return;

            fetch('/api/order/calculate_size', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    risk_amount: risk,
                    entry_price: price,
                    sl_price: sl,
                    security_id: secId,
                    transaction_type: 'SELL',
                    product_type: 'MARGIN'
                })
            })
            .then(function(r){ return r.json(); })
            .then(function(data) {
                if (data.error) return;
                document.getElementById('spread-qty').textContent = data.quantity;
                document.getElementById('spread-lots').textContent = data.num_lots != null ? data.num_lots + ' lot(s) x ' + data.lot_size : '';
                document.getElementById('spread-risk-unit').textContent = fmtDec(data.risk_per_unit);
                document.getElementById('spread-actual-risk').textContent = fmt(data.actual_risk);
                document.getElementById('spread-quantity').value = data.quantity;
            })
            .catch(function(err) { console.error('Spread calc error:', err); });
        }

        document.querySelectorAll('.spread-calc').forEach(function(el) {
            el.addEventListener('input', triggerSpreadCalc);
        });

        // ── Place Spread Order ───────────────────────────────────────
        function placeSpreadOrder() {
            var sellId = document.getElementById('spread-sell-id').value;
            var buyId = document.getElementById('spread-buy-id').value;
            var qty = parseInt(document.getElementById('spread-quantity').value);

            if (!sellId) { showToast('Select sell instrument', 'warning'); return; }
            if (!buyId) { showToast('Select hedge (buy) instrument', 'warning'); return; }
            if (!qty || qty <= 0) { showToast('Enter quantity or fill pricing for auto-calc', 'warning'); return; }

            var payload = {
                sell_security_id: sellId,
                sell_exchange_segment: document.getElementById('spread-sell-exseg').value,
                sell_price: parseFloat(document.getElementById('spread-sell-price').value) || 0,
                sell_trigger_price: parseFloat(document.getElementById('spread-sell-trigger').value) || 0,
                sell_sl: parseFloat(document.getElementById('spread-sell-sl').value) || 0,
                buy_security_id: buyId,
                buy_exchange_segment: document.getElementById('spread-buy-exseg').value,
                quantity: qty
            };

            if (payload.sell_price <= 0) { showToast('Enter sell price', 'warning'); return; }
            if (payload.sell_trigger_price <= 0) { showToast('Enter trigger price for sell', 'warning'); return; }

            var sellName = document.getElementById('spread-sell-search').value;
            var buyName = document.getElementById('spread-buy-search').value;
            var label = 'SELL ' + qty + ' ' + sellName + ' @ ' + payload.sell_price +
                        ' (hedge: BUY ' + buyName + ' @ MKT)';

            if (!confirm('Place spread order?\\n' + label)) return;

            fetch('/api/order/place_spread', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
            .then(function(r){ return r.json(); })
            .then(function(result) {
                if (result.status === 'error') {
                    playAlert('error');
                    showToast('Spread order failed: ' + result.message, 'error');
                } else {
                    playAlert('order');
                    showToast('Spread order queued - monitoring trigger @ \\u20B9' + payload.sell_trigger_price, 'success');
                }
            })
            .catch(function(err) { showToast('Network error: ' + err, 'error'); });
        }

        // Safe dashboard update wrapper
        function safeUpdate(data) {
            try {
                if (data && typeof data === 'object' && !data.error) {
                    updateDashboard(data);
                } else if (data && data.error) {
                    console.warn('Status API error:', data.error);
                }
            } catch(e) {
                console.error('Dashboard update error:', e, data);
            }
        }

        function extendTradeLimit() {
            if (!confirm('Are you sure you want to extend your daily trade limit by 10? This can only be done once per day.')) {
                return;
            }
            fetch('/api/admin/extend_trade_limit', {
                method: 'POST'
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.status === 'success') {
                    showToast(d.message, 'success');
                    fetch('/api/status')
                        .then(function(r){ return r.json(); })
                        .then(safeUpdate)
                        .catch(function(e){});
                } else {
                    showToast(d.message || 'Failed to extend trade limit', 'error');
                }
            })
            .catch(function(e) {
                showToast('Error: ' + e.message, 'error');
            });
        }

        // Socket.IO real-time updates (bound via setupSocketListeners when socket connects async)

        // Initial fetch
        fetch('/api/status')
            .then(function(r){
                if (!r.ok) console.warn('Status HTTP error:', r.status);
                return r.json();
            })
            .then(safeUpdate)
            .catch(function(e){ console.error('Status fetch error:', e); });

        // Fallback polling
        setInterval(function() {
            fetch('/api/status')
                .then(function(r){ return r.json(); })
                .then(safeUpdate)
                .catch(function(e){});
        }, {{ interval * 1000 }});

        // ── Token Status ──────────────────────────────────
        function checkTokenStatus() {
            var el = document.getElementById('token-status');
            fetch('/api/token/status')
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.valid) {
                        el.style.background = '#0d4429';
                        el.style.color = '#3fb950';
                        el.style.borderColor = '#238636';
                        el.textContent = 'API: OK';
                        el.title = 'Token valid (bal: ' + (d.balance != null ? d.balance : '?') + '). Click to refresh.';
                    } else {
                        el.style.background = '#4a1d1d';
                        el.style.color = '#f85149';
                        el.style.borderColor = '#da3633';
                        el.textContent = 'API: INVALID';
                        el.title = (d.error || 'Token invalid') + ' — Click to refresh token';
                    }
                })
                .catch(function() {
                    el.style.background = '#3d2e00';
                    el.style.color = '#d29922';
                    el.style.borderColor = '#9e6a03';
                    el.textContent = 'API: ?';
                    el.title = 'Could not check token status. Click to refresh.';
                });
        }
        function refreshToken() {
            var el = document.getElementById('token-status');
            el.textContent = 'Refreshing...';
            el.style.color = '#d29922';
            el.style.borderColor = '#9e6a03';
            el.style.background = '#3d2e00';
            fetch('/api/token/refresh', {method:'POST', headers:{'Content-Type':'application/json'}})
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.status === 'ok') {
                        showToast('Token refreshed successfully!', 'success');
                        checkTokenStatus();
                    } else {
                        showToast('Token refresh failed: ' + (d.message || 'Unknown error'), 'error');
                        checkTokenStatus();
                    }
                })
                .catch(function(e) {
                    showToast('Token refresh request failed: ' + e, 'error');
                    checkTokenStatus();
                });
        }
        // Check on load, then every 5 minutes
        checkTokenStatus();
        setInterval(checkTokenStatus, 300000);

        function updateBrokerUI(activeBroker) {
            var dhanBtn = document.getElementById('broker-dhan');
            var kotakBtn = document.getElementById('broker-kotak');
            if (!dhanBtn || !kotakBtn) return;
            
            if (activeBroker === 'DHAN') {
                dhanBtn.style.background = '#21262d';
                dhanBtn.style.color = '#c9d1d9';
                kotakBtn.style.background = 'none';
                kotakBtn.style.color = '#8b949e';
            } else {
                kotakBtn.style.background = '#21262d';
                kotakBtn.style.color = '#c9d1d9';
                dhanBtn.style.background = 'none';
                dhanBtn.style.color = '#8b949e';
            }
        }

        function fetchActiveBroker() {
            fetch('/api/broker/active')
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (d.status === 'ok') {
                        updateBrokerUI(d.active_broker);
                    }
                })
                .catch(function(e) { console.error('Error fetching active broker:', e); });
        }

        window.toggleBroker = function(broker) {
            showToast('Switching broker to ' + broker + '...', 'info');
            fetch('/api/broker/toggle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ broker: broker })
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.status === 'ok') {
                    showToast('Broker switched to ' + broker + '!', 'success');
                    updateBrokerUI(broker);
                    setTimeout(function() { window.location.reload(); }, 800);
                } else {
                    showToast('Broker switch failed: ' + (d.message || 'Error'), 'error');
                }
            })
            .catch(function(e) {
                showToast('Broker switch request failed: ' + e, 'error');
            });
        };

        // Fetch active broker on load
        fetchActiveBroker();

        // ── Option Chain ─────────────────────────────────────────────
        var _ocLotSize = 75;

        function initOptionChain() {
            // Clear any existing auto-refresh timer
            if (_ocAutoTimer) { clearInterval(_ocAutoTimer); _ocAutoTimer = null; }
            var sel = document.getElementById('oc-underlying');
            var underlying = sel.options[sel.selectedIndex].text;
            fetch('/api/option_chain/expiries?underlying=' + underlying)
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.error) { showToast('Expiry fetch failed: ' + d.error, 'error'); return; }
                    var expiries = d.expiries || [];
                    var expSel = document.getElementById('oc-expiry');
                    expSel.innerHTML = '';
                    expiries.forEach(function(exp) {
                        var opt = document.createElement('option');
                        opt.value = exp;
                        opt.textContent = exp;
                        expSel.appendChild(opt);
                    });
                    if (expiries.length > 0) loadOptionChain();
                    else {
                        document.getElementById('oc-body').innerHTML =
                            '<tr><td colspan="3" style="text-align:center;color:#484f58;padding:20px;">No expiries found</td></tr>';
                    }
                })
                .catch(function(e) {
                    console.error('Expiry list error:', e);
                    document.getElementById('oc-body').innerHTML =
                        '<tr><td colspan="3" style="text-align:center;color:#f85149;padding:20px;">Failed to load expiries</td></tr>';
                });
        }

        var _ocAutoTimer = null;
        var _ocRefreshing = false;  // Prevent overlapping requests

        var _ocRenderedKey = '';  // tracks last rendered chain structure

        function renderOcChain(d, expiry) {
            var body = document.getElementById('oc-body');
            var spot = d.spot || 0;
            _ocLotSize = d.lot_size || 75;
            
            // Adjust input step and min values based on active lot size
            var qtyOverride = document.getElementById('sqb-qty-override');
            if (qtyOverride) {
                qtyOverride.step = _ocLotSize;
                qtyOverride.min = _ocLotSize;
                if (!qtyOverride.value || qtyOverride.value === "50" || qtyOverride.value === "75") {
                    qtyOverride.value = _ocLotSize;
                }
            }
            var tagQtyInput = document.getElementById('chart-tag-breakout-qty-input');
            if (tagQtyInput) {
                tagQtyInput.step = _ocLotSize;
                tagQtyInput.min = _ocLotSize;
                if (!tagQtyInput.value || tagQtyInput.value === "50" || tagQtyInput.value === "75") {
                    tagQtyInput.value = _ocLotSize;
                }
            }
            var now = new Date();
            var timeStr = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0') + ':' + now.getSeconds().toString().padStart(2,'0');
            document.getElementById('oc-spot').textContent = 'Spot: \\u20B9' + (spot > 0 ? spot.toFixed(2) : '--') + '  |  Expiry: ' + expiry + '  |  Lot: ' + _ocLotSize + '  |  ' + timeStr;

            var chain = d.chain || [];
            if (chain.length === 0) {
                body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#484f58;padding:20px;">No strikes found</td></tr>';
                _ocRenderedKey = '';
                return;
            }

            // Build a key representing the chain structure (strikes + expiry)
            var structKey = expiry + '|' + chain.map(function(r){return r.strike;}).join(',');

            if (structKey === _ocRenderedKey) {
                // Structure unchanged — update LTP values in-place only (no flicker)
                chain.forEach(function(row) {
                    var ceSid = String(row.ce_security_id || '');
                    var peSid = String(row.pe_security_id || '');
                    if (ceSid && !_ltpCache[ceSid]) {
                        var el = document.getElementById('oc-ltp-' + ceSid);
                        if (el) el.textContent = row.ce_ltp ? row.ce_ltp.toFixed(2) : '-';
                    }
                    if (peSid && !_ltpCache[peSid]) {
                        var el = document.getElementById('oc-ltp-' + peSid);
                        if (el) el.textContent = row.pe_ltp ? row.pe_ltp.toFixed(2) : '-';
                    }
                });
                // Always apply WebSocket cache on top (values guaranteed > 0)
                Object.keys(_ltpCache).forEach(function(sid) {
                    var el = document.getElementById('oc-ltp-' + sid);
                    if (el) el.textContent = _ltpCache[sid].toFixed(2);
                });
                return;
            }

            // Structure changed — full rebuild
            console.log('[OC] Rebuild triggered. old:', JSON.stringify(_ocRenderedKey), 'new:', JSON.stringify(structKey));
            _ocRenderedKey = structKey;
            var atmIdx = Math.floor(chain.length / 2);
            var html = '';
            chain.forEach(function(row, i) {
                var isATM = (i === atmIdx);
                var rowStyle = isATM ? 'background:#1a2332;' : '';
                var ceLtp    = row.ce_ltp ? row.ce_ltp.toFixed(2) : '-';
                var peLtp    = row.pe_ltp ? row.pe_ltp.toFixed(2) : '-';
                var ceSid    = row.ce_security_id || '';
                var peSid    = row.pe_security_id || '';
                var ceLtpVal = row.ce_ltp || 0;
                var peLtpVal = row.pe_ltp || 0;
                var stk      = row.strike;

                html += '<tr id="oc-row-' + stk.toFixed(0) + '" style="' + rowStyle + '">';

                // CE cell: [S][B] LTP
                html += '<td style="text-align:right;padding:5px 8px;white-space:nowrap;">';
                if (ceSid) {
                    html += '<button class="oc-s-btn" id="qsb-CE-' + stk.toFixed(0) + '-S" ';
                    html += 'onclick="spreadSelectLeg(\\'sell\\',\\'' + ceSid + '\\',' + stk + ',' + ceLtpVal + ',\\'CE\\');event.stopPropagation();" title="Spread SELL">S</button>';
                    html += '<button class="oc-b-btn" id="qsb-CE-' + stk.toFixed(0) + '-B" ';
                    html += 'onclick="spreadSelectLeg(\\'buy\\',\\'' + ceSid + '\\',' + stk + ',' + ceLtpVal + ',\\'CE\\');event.stopPropagation();" title="Spread BUY">B</button>';
                }
                html += '<span id="oc-ltp-' + ceSid + '" style="color:#3fb950;font-weight:600;cursor:pointer;" ';
                html += 'onclick="ocSelect(\\'' + ceSid + '\\',\\'CE\\',\\'' + expiry + '\\',' + stk + ',' + ceLtpVal + ')">' + ceLtp + '</span>';
                html += '</td>';

                // Strike cell
                html += '<td style="text-align:center;font-weight:700;color:#e6edf3;background:#161b22;border-left:2px solid #30363d;border-right:2px solid #30363d;padding:5px 12px;">' + stk.toFixed(0) + '</td>';

                // PE cell: LTP [S][B]
                html += '<td style="text-align:left;padding:5px 8px;white-space:nowrap;">';
                html += '<span id="oc-ltp-' + peSid + '" style="color:#f85149;font-weight:600;cursor:pointer;" ';
                html += 'onclick="ocSelect(\\'' + peSid + '\\',\\'PE\\',\\'' + expiry + '\\',' + stk + ',' + peLtpVal + ')">' + peLtp + '</span>';
                if (peSid) {
                    html += '<button class="oc-s-btn" id="qsb-PE-' + stk.toFixed(0) + '-S" ';
                    html += 'style="margin-left:3px;margin-right:2px;" ';
                    html += 'onclick="spreadSelectLeg(\\'sell\\',\\'' + peSid + '\\',' + stk + ',' + peLtpVal + ',\\'PE\\');event.stopPropagation();" title="Spread SELL">S</button>';
                    html += '<button class="oc-b-btn" id="qsb-PE-' + stk.toFixed(0) + '-B" ';
                    html += 'style="margin-left:0;" ';
                    html += 'onclick="spreadSelectLeg(\\'buy\\',\\'' + peSid + '\\',' + stk + ',' + peLtpVal + ',\\'PE\\');event.stopPropagation();" title="Spread BUY">B</button>';
                }
                html += '</td>';

                html += '</tr>';
            });
            body.innerHTML = html;
            // Apply WebSocket cache on top of initial REST values
            Object.keys(_ltpCache).forEach(function(sid) {
                var el = document.getElementById('oc-ltp-' + sid);
                if (el) el.textContent = _ltpCache[sid].toFixed(2);
            });
            restoreSpreadPillHighlights();
        }

        var _ocLtpSubscribed = false;
        var _ocLastUnderlying = '';  // detect underlying switches to force feed restart

        function subscribeOcLtp(chain) {
            // Subscribe ATM±10 strikes (CE + PE) for real-time LTP updates
            var sel = document.getElementById('oc-underlying');
            var underlying = sel ? sel.options[sel.selectedIndex].text : 'NIFTY';
            var isBse = (underlying === 'SENSEX' || underlying === 'BANKEX');
            var exchange_segment = isBse ? 'BSE_FNO' : 'NSE_FNO';
            var atmIdx = Math.floor(chain.length / 2);
            var lo = Math.max(0, atmIdx - 10);
            var hi = Math.min(chain.length - 1, atmIdx + 10);
            var instruments = [];
            for (var i = lo; i <= hi; i++) {
                var row = chain[i];
                if (row.ce_security_id) instruments.push({sid: String(row.ce_security_id), type: 'CE', exchange_segment: exchange_segment});
                if (row.pe_security_id) instruments.push({sid: String(row.pe_security_id), type: 'PE', exchange_segment: exchange_segment});
            }
            if (!instruments.length) return;
            var force = (underlying !== _ocLastUnderlying);
            _ocLastUnderlying = underlying;
            fetch('/api/option_chain/subscribe_ltp', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({instruments: instruments, underlying: underlying, force: force})
            }).catch(function(){});
            _ocLtpSubscribed = true;
        }

        function loadOptionChain(silent) {
            var sel = document.getElementById('oc-underlying');
            var underlying = sel.options[sel.selectedIndex].text;
            var expiry = document.getElementById('oc-expiry').value;
            if (!expiry) { initOptionChain(); return; }

            // Skip if a previous silent refresh is still in-flight
            if (silent && _ocRefreshing) return;

            var body = document.getElementById('oc-body');
            if (!silent) {
                body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#484f58;padding:20px;">Loading...</td></tr>';
            }

            _ocRefreshing = true;
            fetch('/api/option_chain/data?underlying=' + underlying + '&expiry=' + expiry)
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    _ocRefreshing = false;
                    if (d.error) {
                        if (!silent) body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#f85149;padding:20px;">' + d.error + '</td></tr>';
                        return;
                    }
                    renderOcChain(d, expiry);
                    subscribeOcLtp(d.chain || []);
                    // Update live chart candle with latest sell-leg LTP
                    if (_spreadSellLeg && _lwSeries && _lwCurrentSecurity === _spreadSellLeg.securityId) {
                        var chain = d.chain || [];
                        for (var ci = 0; ci < chain.length; ci++) {
                            var row = chain[ci];
                            var ltp = 0;
                            if (String(row.ce_security_id) === String(_spreadSellLeg.securityId)) ltp = parseFloat(row.ce_ltp) || 0;
                            else if (String(row.pe_security_id) === String(_spreadSellLeg.securityId)) ltp = row.pe_ltp;
                            if (ltp > 0) {
                                _spreadSellLeg.ltp = ltp;
                                updateSpreadQuickBar();
                                // Closed Market / Holiday Filter (IST checks)
                                var now = new Date();
                                var utc = now.getTime() + (now.getTimezoneOffset() * 60000);
                                var istTime = new Date(utc + (3600000 * 5.5));
                                var day = istTime.getDay();
                                var hour = istTime.getHours();
                                var min = istTime.getMinutes();
                                var timeVal = hour * 100 + min;
                                var isHoliday = (day === 0 || day === 6);
                                var isMarketOpen = (!isHoliday && timeVal >= 915 && timeVal <= 1530);
                                if (!isMarketOpen) return;

                                // Push live LTP into the current bar
                                var nowSec = Math.floor(Date.now() / 1000);
                                var tfEl = document.getElementById('chart-timeframe');
                                var interval = tfEl ? parseInt(tfEl.value) : 60;
                                var barSec = nowSec - (nowSec % interval);
                                if (barSec !== _liveBarTime) {
                                    _liveBarTime = barSec; _liveBarOpen = ltp;
                                    _liveBarHigh = ltp;    _liveBarLow  = ltp;
                                } else {
                                    if (ltp > _liveBarHigh) _liveBarHigh = ltp;
                                    if (ltp < _liveBarLow)  _liveBarLow  = ltp;
                                }
                                try { _lwSeries.update({time: barSec, open: _liveBarOpen, high: _liveBarHigh, low: _liveBarLow, close: ltp}); } catch(e) {}
                                break;
                            }
                        }
                    }
                })
                .catch(function(e) {
                    _ocRefreshing = false;
                    if (!silent) body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#f85149;padding:20px;">Error: ' + e + '</td></tr>';
                });

            // Start auto-refresh timer (5s) if not already running
            startOcAutoRefresh();
        }

        function startOcAutoRefresh() {
            if (_ocAutoTimer) return;  // Already running
            _ocAutoTimer = setInterval(function() {
                loadOptionChain(true);
            }, 1000);
            console.log('[OC] Auto-refresh started (1s interval)');
            var statusEl = document.getElementById('oc-auto-status');
            if (statusEl) statusEl.style.color = '#3fb950';
        }

        function stopOcAutoRefresh() {
            if (_ocAutoTimer) {
                clearInterval(_ocAutoTimer);
                _ocAutoTimer = null;
                console.log('[OC] Auto-refresh stopped');
            }
            var statusEl = document.getElementById('oc-auto-status');
            if (statusEl) statusEl.style.color = '#484f58';
        }

        function ocSelect(securityId, optType, expiry, strike, ltp) {
            if (!securityId) return;
            var sel = document.getElementById('oc-underlying');
            var underlying = sel.options[sel.selectedIndex].text;
            var symbol = underlying + ' ' + strike.toFixed(0) + ' ' + optType + ' ' + expiry;

            document.getElementById('instrument-search').value = symbol;
            document.getElementById('order-security-id').value = securityId;
            document.getElementById('order-exchange-segment').value = 'NSE_FNO';
            document.getElementById('order-lot-size').value = _ocLotSize;
            document.getElementById('order-tick-size').value = '0.05';
            document.getElementById('selected-instrument').style.display = 'block';
            document.getElementById('selected-instrument').textContent =
                'NSE | OPTIDX | Lot size: ' + _ocLotSize + ' | ID: ' + securityId;

            if (ltp && ltp > 0) {
                document.getElementById('order-price').value = ltp;
                triggerAutoCalc();
            }

            // Subscribe to depth for this instrument
            subscribeDepth(securityId, 'NSE_FNO', symbol);

            showToast('Selected: ' + symbol + ' @ \\u20B9' + (ltp ? ltp.toFixed(2) : '--'), 'success');
        }

        document.getElementById('oc-underlying').addEventListener('change', function() {
            initOptionChain();
        });

        initOptionChain();

        var _ltpCache = {};  // sid → ltp — persists across re-renders

        // ── Spread Quick Entry ───────────────────────────────────────────
        var _spreadSellLeg = null;  // {securityId, strike, ltp, optType}
        var _spreadBuyLeg  = null;
        var _sqbCalcTimeout = null;

        function spreadSelectLeg(side, secId, strike, ltp, optType) {
            if (!secId) return;
            var expiry = document.getElementById('oc-expiry').value;
            var sel    = document.getElementById('oc-underlying');
            var underlying = sel.options[sel.selectedIndex].text;
            var leg = {securityId: secId, strike: strike, ltp: ltp, optType: optType,
                       expiry: expiry, underlying: underlying};
            var symbol = underlying + ' ' + strike.toFixed(0) + ' ' + optType + ' ' + expiry;

            var bseUnderlyings = ['SENSEX', 'BANKEX'];
            var exSeg = bseUnderlyings.indexOf(underlying) >= 0 ? 'BSE_FNO' : 'NSE_FNO';

            if (side === 'sell') {
                _spreadSellLeg = leg;
                _spreadSellLeg.exchangeSegment = exSeg;
                _sqbSellPriceDirty = false;  // reset so new LTP pre-fills
                // Subscribe DOM + load chart for sell leg
                subscribeDepth(secId, exSeg, symbol);
                loadChart(secId, exSeg);
                switchDomTab('chart');
                // Cross-populate existing spread form sell leg
                document.getElementById('spread-sell-id').value = secId;
                document.getElementById('spread-sell-exseg').value = exSeg;
                document.getElementById('spread-sell-price').value = ltp.toFixed(2);
                document.getElementById('spread-sell-search').value = symbol;
                document.getElementById('spread-sell-info').style.display = 'block';
                document.getElementById('spread-sell-info').textContent = 'NSE_FNO | ID: ' + secId;
                showToast('SELL: ' + strike.toFixed(0) + ' ' + optType + ' @ \\u20B9' + ltp.toFixed(2), 'warning');
            } else {
                _spreadBuyLeg = leg;
                _spreadBuyLeg.exchangeSegment = exSeg;
                _sqbBuyPriceDirty = false;  // reset so new LTP pre-fills
                // Cross-populate existing spread form buy leg
                document.getElementById('spread-buy-id').value = secId;
                document.getElementById('spread-buy-exseg').value = exSeg;
                document.getElementById('spread-buy-search').value = symbol;
                document.getElementById('spread-buy-info').style.display = 'block';
                document.getElementById('spread-buy-info').textContent = 'NSE_FNO | ID: ' + secId;
                showToast('BUY: ' + strike.toFixed(0) + ' ' + optType + ' @ \\u20B9' + ltp.toFixed(2), 'success');
            }
            updateSpreadQuickBar();
            if (_spreadSellLeg && _spreadBuyLeg) { spreadQuickCalc(); }
        }

        function restoreSpreadPillHighlights() {
            if (_spreadSellLeg) {
                var sBtn = document.getElementById('qsb-' + _spreadSellLeg.optType + '-' + _spreadSellLeg.strike.toFixed(0) + '-S');
                if (sBtn) sBtn.classList.add('oc-s-active');
            }
            if (_spreadBuyLeg) {
                var bBtn = document.getElementById('qsb-' + _spreadBuyLeg.optType + '-' + _spreadBuyLeg.strike.toFixed(0) + '-B');
                if (bBtn) bBtn.classList.add('oc-b-active');
            }
        }

        var _sqbSellPriceDirty = false;
        var _sqbBuyPriceDirty  = false;

        function sqbPriceDirty(side) {
            if (side === 'sell') _sqbSellPriceDirty = true;
            else _sqbBuyPriceDirty = true;
        }

        function sqbGetSellPrice() {
            return parseFloat(document.getElementById('sqb-sell-price').value) || 0;
        }
        function sqbGetBuyPrice() {
            return parseFloat(document.getElementById('sqb-buy-price').value) || 0;
        }
        function sqbGetSL() {
            return parseFloat(document.getElementById('sqb-sell-sl').value) || 0;
        }

        function updateSpreadQuickBar() {
            var panel = document.getElementById('sqb-panel');
            if (!_spreadSellLeg && !_spreadBuyLeg) { panel.style.display = 'none'; return; }
            panel.style.display = 'block';

            // Update labels (strike + optType only)
            document.getElementById('sqb-sell-label').textContent = _spreadSellLeg
                ? (_spreadSellLeg.strike.toFixed(0) + ' ' + _spreadSellLeg.optType) : '-- pick S';
            document.getElementById('sqb-buy-label').textContent = _spreadBuyLeg
                ? (_spreadBuyLeg.strike.toFixed(0) + ' ' + _spreadBuyLeg.optType) : '-- pick B';

            // Pre-fill price inputs from LTP only if user hasn't manually edited
            if (_spreadSellLeg && !_sqbSellPriceDirty) {
                var sp = document.getElementById('sqb-sell-price');
                if (_spreadSellLeg.ltp > 0) sp.value = _spreadSellLeg.ltp.toFixed(2);
                else sp.value = '';
            }
            if (_spreadBuyLeg && !_sqbBuyPriceDirty) {
                var bp = document.getElementById('sqb-buy-price');
                if (_spreadBuyLeg.ltp > 0) bp.value = _spreadBuyLeg.ltp.toFixed(2);
                else bp.value = '';
            }

            sqbAutoCalc();
        }

        function sqbAutoCalc() {
            var sellPrice = sqbGetSellPrice();
            var buyPrice  = sqbGetBuyPrice();
            var sl        = sqbGetSL();
            var maxLoss   = parseFloat(document.getElementById('sqb-risk').value) || 0;
            var lotSize   = _ocLotSize || 25;

            // Net credit display
            var nc = document.getElementById('sqb-net-credit');
            if (sellPrice > 0 && buyPrice > 0) {
                var net = sellPrice - buyPrice;
                nc.textContent = (net >= 0 ? '+' : '') + '\\u20B9' + net.toFixed(2) + (net >= 0 ? ' cr' : ' db');
                nc.style.color = net >= 0 ? '#3fb950' : '#f85149';
            } else { nc.textContent = ''; }

            // Auto-qty from SL risk: lots = floor(maxLoss / (risk_points * lotSize))
            var qtyEl  = document.getElementById('sqb-qty');
            var lotsEl = document.getElementById('sqb-lots');
            var autoQty = 0;

            // Check if we are focusing on Chart trading prices
            var chartBreakout = parseFloat(document.getElementById('chart-breakout-val').value) || 0;
            var chartSL = parseFloat(document.getElementById('chart-sl-val').value) || 0;

            if (chartBreakout > 0 && chartSL > 0 && maxLoss > 0) {
                var diff = Math.abs(chartBreakout - chartSL);
                if (diff > 0) {
                    var riskPerLot = diff * lotSize;
                    var lots = Math.floor(maxLoss / riskPerLot);
                    autoQty = lots * lotSize;
                    qtyEl.textContent  = autoQty > 0 ? autoQty : '-';
                    lotsEl.textContent = lots > 0 ? '(' + lots + ' lots)' : '';
                }
            } else if (sl > sellPrice && sellPrice > 0 && maxLoss > 0) {
                var riskPerLot = (sl - sellPrice) * lotSize;
                var lots = Math.floor(maxLoss / riskPerLot);
                autoQty = lots * lotSize;
                qtyEl.textContent  = autoQty > 0 ? autoQty : '-';
                lotsEl.textContent = lots > 0 ? '(' + lots + ' lots)' : '';
            } else {
                qtyEl.textContent  = '-';
                lotsEl.textContent = sl > 0 && sl <= sellPrice ? '\\u26A0 SL must be > sell price' : '';
            }

            // Enable execute: LIMIT needs sell price + SL > sellPrice + qty; MKT only needs both legs + SL + qty
            var qty = parseInt(document.getElementById('sqb-qty-override').value) || autoQty;
            var bothLegs = !!(_spreadSellLeg && _spreadBuyLeg);
            var readyLmt = bothLegs && sellPrice > 0 && sl > sellPrice && qty > 0;
            var readyMkt = bothLegs && sl > 0 && qty > 0;
            var execBtn    = document.getElementById('sqb-execute-btn');
            var execMktBtn = document.getElementById('sqb-execute-mkt-btn');
            execBtn.disabled    = !readyLmt; execBtn.style.opacity    = readyLmt ? '1' : '0.5';
            execMktBtn.disabled = !readyMkt; execMktBtn.style.opacity = readyMkt ? '1' : '0.5';

            // Show single-leg emergency buttons when a leg is selected
            document.getElementById('sqb-single-sell-mkt-btn').style.display = _spreadSellLeg ? 'inline-block' : 'none';
            document.getElementById('sqb-single-buy-mkt-btn').style.display  = _spreadBuyLeg  ? 'inline-block' : 'none';
        }

        // Keep spreadQuickCalc as alias for backward compat
        function spreadQuickCalc() { sqbAutoCalc(); }

        function executeSingleLeg(side, orderType) {
            var leg = side === 'sell' ? _spreadSellLeg : _spreadBuyLeg;
            if (!leg) { showToast('No ' + side + ' leg selected', 'warning'); return; }
            var price = side === 'sell' ? sqbGetSellPrice() : sqbGetBuyPrice();
            var isMarket = (orderType === 'MARKET');
            if (!isMarket && (!price || price <= 0)) { showToast('Enter price first', 'warning'); return; }
            var qty = parseInt(document.getElementById('sqb-qty-override').value)
                   || parseInt(document.getElementById('sqb-qty').textContent) || 0;
            if (!qty || qty <= 0) { showToast('Enter quantity first', 'warning'); return; }
            var txn = side === 'sell' ? 'SELL' : 'BUY';
            var priceLabel = isMarket ? 'MARKET' : ('\\u20B9' + price.toFixed(2) + ' LIMIT');
            if (!confirm(txn + ' ' + qty + ' x ' + leg.strike.toFixed(0) + ' ' + leg.optType + ' @ ' + priceLabel + '\\n(single leg — no hedge)')) return;
            fetch('/api/order/place', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    security_id:      leg.securityId,
                    exchange_segment: leg.exchangeSegment || 'NSE_FNO',
                    transaction_type: txn,
                    quantity:         qty,
                    order_type:       isMarket ? 'MARKET' : 'LIMIT',
                    product_type:     'MARGIN',
                    price:            isMarket ? 0 : price
                })
            })
            .then(function(r){ return r.json(); })
            .then(function(result) {
                if (result.status === 'error' || result.status === 'BLOCKED') {
                    playAlert('error');
                    showToast(txn + ' rejected: ' + (result.message || result.reason || ''), 'error');
                } else {
                    playAlert('order');
                    showToast(txn + ' order sent (' + (isMarket ? 'MARKET' : 'LIMIT \\u20B9' + price.toFixed(2)) + ')', 'success');
                }
            })
            .catch(function(e) { showToast('Network error: ' + e, 'error'); });
        }

        function executeSpreadNow(orderType) {
            if (!_spreadSellLeg || !_spreadBuyLeg) { showToast('Select both SELL and BUY legs', 'warning'); return; }
            var qty = parseInt(document.getElementById('sqb-qty-override').value)
                   || parseInt(document.getElementById('sqb-qty').textContent);
            if (!qty || qty <= 0) { showToast('Enter quantity or wait for auto-calc', 'warning'); return; }
            var isMarket = (orderType === 'MARKET');
            var sellPrice = sqbGetSellPrice();
            var sl        = sqbGetSL();
            if (!isMarket && (!sellPrice || sellPrice <= 0)) { showToast('Enter SELL price', 'warning'); return; }
            var minSl = isMarket ? (_spreadSellLeg.ltp || 0) : sellPrice;
            if (!sl || sl <= minSl) { showToast('SL must be above ' + (isMarket ? 'current price ₹' + minSl.toFixed(2) : 'sell price'), 'warning'); return; }
            var sellLabel = isMarket ? 'MARKET' : ('\\u20B9' + sellPrice.toFixed(2) + ' LIMIT');
            var label = 'EXECUTE SPREAD NOW (' + (isMarket ? 'MARKET' : 'LIMIT') + '):\\n'
                + 'BUY  ' + qty + ' x ' + _spreadBuyLeg.strike.toFixed(0)  + ' ' + _spreadBuyLeg.optType  + ' @ MARKET (hedge first)\\n'
                + 'SELL ' + qty + ' x ' + _spreadSellLeg.strike.toFixed(0) + ' ' + _spreadSellLeg.optType + ' @ ' + sellLabel + '\\n'
                + 'SL: \\u20B9' + sl.toFixed(2);
            if (!confirm(label)) return;
            var payload = {
                sell_security_id:      _spreadSellLeg.securityId,
                sell_exchange_segment: _spreadSellLeg.exchangeSegment || 'NSE_FNO',
                sell_price:            isMarket ? 0 : sellPrice,
                sell_trigger_price:    isMarket ? 0 : sellPrice,
                sell_order_type:       isMarket ? 'MARKET' : 'LIMIT',
                sell_sl:               sl,
                buy_security_id:       _spreadBuyLeg.securityId,
                buy_exchange_segment:  _spreadBuyLeg.exchangeSegment || 'NSE_FNO',
                quantity:              qty,
                instant:               true
            };
            fetch('/api/order/place_spread', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
            .then(function(r){ return r.json(); })
            .then(function(result) {
                if (result.status === 'error' || result.status === 'BLOCKED') {
                    playAlert('error');
                    showToast('Spread rejected: ' + (result.message || result.reason || ''), 'error');
                } else {
                    showToast('Spread order sent — awaiting fill confirmation...', 'info');
                    // Capture entry screenshot and create journal entry
                    var sellLegSnap = _spreadSellLeg;
                    var buyLegSnap  = _spreadBuyLeg;
                    var qtySnap     = qty;
                    var sellPriceSnap = isMarket ? (sellLegSnap.ltp || 0) : sellPrice;
                    captureNiftyScreenshot(sellPriceSnap, null, function(entryImg) {
                        var lotSz = _ocLotSize || 25;
                        var entryData = {
                            trade_type:        'spread',
                            instrument:        (sellLegSnap.underlying || '') + ' ' + sellLegSnap.strike.toFixed(0) + ' ' + sellLegSnap.optType + ' ' + (sellLegSnap.expiry || ''),
                            hedge_instrument:  (buyLegSnap.underlying  || '') + ' ' + buyLegSnap.strike.toFixed(0)  + ' ' + buyLegSnap.optType  + ' ' + (buyLegSnap.expiry  || ''),
                            sell_security_id:  String(sellLegSnap.securityId),
                            sell_entry_price:  sellPriceSnap,
                            buy_entry_price:   buyLegSnap.ltp || 0,
                            lots:              Math.round(qtySnap / lotSz) || 1,
                            lot_size:          lotSz,
                            entry_screenshot:  entryImg
                        };
                        createJournalEntry(entryData);
                    });
                    clearSpreadQuickBar();
                }
            })
            .catch(function(e) { showToast('Network error: ' + e, 'error'); });
        }


        // ── Journal screenshot + entry helpers ─────────────────────────
        var _journalOpenEntries = {};  // sell_security_id → entry_id

        function captureNiftyScreenshot(entryPrice, exitPrice, callback) {
            fetch('/api/chart/nifty')
            .then(function(r){ return r.json(); })
            .then(function(d) {
                var candles = (d.candles || []).slice(-150);
                if (!candles.length) { callback(null); return; }
                var canvas = document.createElement('canvas');
                canvas.width = 900; canvas.height = 200;
                drawChartOnCanvas(canvas, candles, entryPrice, exitPrice);
                var dataUrl = canvas.toDataURL('image/png');
                fetch('/api/journal/screenshot', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({data_url: dataUrl})
                })
                .then(function(r){ return r.json(); })
                .then(function(res){ callback(res.filename || null); })
                .catch(function(){ callback(null); });
            })
            .catch(function(){ callback(null); });
        }

        function drawChartOnCanvas(canvas, candles, entryPrice, exitPrice) {
            var ctx = canvas.getContext('2d');
            var W = canvas.width, H = canvas.height, PAD = 10;
            ctx.fillStyle = '#0d1117'; ctx.fillRect(0,0,W,H);

            // Grid
            ctx.strokeStyle = '#161b22'; ctx.lineWidth = 1;
            for (var g = 1; g < 4; g++) {
                ctx.beginPath(); ctx.moveTo(0,H*g/4); ctx.lineTo(W,H*g/4); ctx.stroke();
            }

            var minP = Infinity, maxP = -Infinity;
            candles.forEach(function(c){ if(c.low<minP)minP=c.low; if(c.high>maxP)maxP=c.high; });
            if (entryPrice) { if(entryPrice<minP)minP=entryPrice; if(entryPrice>maxP)maxP=entryPrice; }
            if (exitPrice)  { if(exitPrice<minP) minP=exitPrice;  if(exitPrice>maxP) maxP=exitPrice; }
            var pRange = maxP - minP || 1;
            var cw = (W - PAD*2) / candles.length;

            function toY(p){ return H - PAD - (p - minP) / pRange * (H - PAD*2 - 12); }

            candles.forEach(function(c, i) {
                var x = PAD + i * cw + cw * 0.5;
                var up = c.close >= c.open;
                ctx.strokeStyle = up ? '#3fb950' : '#f85149';
                ctx.fillStyle   = up ? '#3fb950' : '#f85149';
                ctx.beginPath(); ctx.moveTo(x, toY(c.high)); ctx.lineTo(x, toY(c.low)); ctx.stroke();
                var bTop = Math.min(toY(c.open), toY(c.close));
                var bH   = Math.max(1, Math.abs(toY(c.close) - toY(c.open)));
                ctx.fillRect(x - cw*0.35, bTop, cw*0.7, bH);
            });

            // Entry marker
            if (entryPrice) {
                var ey = toY(entryPrice);
                ctx.strokeStyle = '#d29922'; ctx.lineWidth = 1;
                ctx.setLineDash([3,3]);
                ctx.beginPath(); ctx.moveTo(0,ey); ctx.lineTo(W,ey); ctx.stroke();
                ctx.setLineDash([]);
                // Arrow at last candle x
                var ex = PAD + (candles.length-1)*cw + cw*0.5;
                ctx.fillStyle = '#d29922';
                ctx.beginPath(); ctx.moveTo(ex+10,ey); ctx.lineTo(ex+4,ey-5); ctx.lineTo(ex+4,ey+5); ctx.closePath(); ctx.fill();
                ctx.font = '10px monospace'; ctx.fillStyle = '#d29922';
                ctx.fillText('ENTRY ' + entryPrice.toFixed(1), 4, ey - 3);
            }
            // Exit marker
            if (exitPrice) {
                var xy = toY(exitPrice);
                ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1;
                ctx.setLineDash([3,3]);
                ctx.beginPath(); ctx.moveTo(0,xy); ctx.lineTo(W,xy); ctx.stroke();
                ctx.setLineDash([]);
                ctx.font = '10px monospace'; ctx.fillStyle = '#58a6ff';
                ctx.fillText('EXIT ' + exitPrice.toFixed(1), 4, xy - 3);
            }
            // Timestamp
            if (candles.length) {
                var last = candles[candles.length-1];
                var d = new Date(last.time * 1000);
                var ts = d.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'});
                ctx.font = '9px monospace'; ctx.fillStyle = '#484f58';
                ctx.fillText('NIFTY · 1m · ' + ts, 4, H - 2);
            }
        }

        function createJournalEntry(entryData) {
            fetch('/api/journal/entry', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(entryData)
            })
            .then(function(r){ return r.json(); })
            .then(function(res) {
                if (res.entry_id && entryData.sell_security_id) {
                    _journalOpenEntries[entryData.sell_security_id] = res.entry_id;
                    console.log('Journal entry created:', res.entry_id, entryData.instrument);
                } else {
                    console.error('Journal entry failed:', res);
                    showToast('Journal entry failed: ' + JSON.stringify(res), 'warning');
                }
            })
            .catch(function(e){
                console.error('Journal entry network error:', e);
                showToast('Journal entry network error', 'warning');
            });
        }

        function closeJournalEntry(securityId, sellExitPrice, buyExitPrice, pnl) {
            var entryId = _journalOpenEntries[securityId];
            if (!entryId) {
                // Not in local map (e.g. page was refreshed mid-trade) — ask server
                fetch('/api/journal/open_entry/' + encodeURIComponent(securityId))
                    .then(function(r){ return r.json(); })
                    .then(function(res) {
                        if (res.entry_id) {
                            _journalOpenEntries[securityId] = res.entry_id;
                            closeJournalEntry(securityId, sellExitPrice, buyExitPrice, pnl);
                        }
                    })
                    .catch(function(){});
                return;
            }
            captureNiftyScreenshot(null, sellExitPrice, function(exitImg) {
                fetch('/api/journal/entry/' + entryId, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        action: 'exit',
                        sell_exit_price: sellExitPrice,
                        buy_exit_price:  buyExitPrice,
                        exit_screenshot: exitImg,
                        pnl: pnl
                    })
                }).then(function(){
                    delete _journalOpenEntries[securityId];
                }).catch(function(){});
            });
        }
        // ── End journal helpers ────────────────────────────────────────

        function clearSpreadQuickBar() {
            _spreadSellLeg = null;
            _spreadBuyLeg  = null;
            _sqbSellPriceDirty = false;
            _sqbBuyPriceDirty  = false;
            document.getElementById('sqb-panel').style.display = 'none';
            document.getElementById('sqb-qty').textContent = '-';
            document.getElementById('sqb-lots').textContent = '';
            document.getElementById('sqb-net-credit').textContent = '';
            document.getElementById('sqb-qty-override').value = '';
            document.getElementById('sqb-sell-sl').value = '';
        }

        // ── Snapshot Chart (TradingView Lightweight Charts) ──────────────
        var _lwChart  = null;
        var _lwSeries = null;
        var _lwCurrentSecurity = null;
        var _liveBarTime = 0;
        var _liveBarOpen = 0;
        var _liveBarHigh = 0;
        var _liveBarLow  = Infinity;

        function initLightweightChart() {
            var container = document.getElementById('dom-chart-canvas');
            if (!container) return;
            if (typeof LightweightCharts === 'undefined') {
                container.innerHTML = '<div style="color:#484f58;font-size:11px;padding:20px;text-align:center;">Chart library not loaded<br><span style="font-size:10px;">Check VPS internet access to cdn.jsdelivr.net</span></div>';
                return;
            }
            container.innerHTML = '';
             var initialHeight = 420;
             if (document.documentElement.classList.contains('chart-trading-mode')) {
                 initialHeight = window.innerHeight - 230;
                 if (initialHeight < 300) initialHeight = 300;
             }
             container.style.height = initialHeight + 'px';
             _lwChart = LightweightCharts.createChart(container, {
                width:  container.clientWidth || 600,
                height: initialHeight,
                layout: { background: {color:'#0d1117'}, textColor:'#8b949e' },
                grid:   { vertLines:{color:'#21262d'}, horzLines:{color:'#21262d'} },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                rightPriceScale: { borderColor:'#30363d' },
                timeScale: { 
                    borderColor:'#30363d', 
                    timeVisible:true, 
                    secondsVisible:false,
                    tickMarkFormatter: function(time, tickMarkType, locale) {
                        var date = new Date(time * 1000);
                        return date.toLocaleTimeString('en-US', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false });
                    }
                },
                localization: {
                    timeFormatter: function(timestamp) {
                        var date = new Date(timestamp * 1000);
                        return date.toLocaleTimeString('en-US', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                    }
                }
            });
            _lwSeries = _lwChart.addCandlestickSeries({
                upColor:'#3fb950', downColor:'#f85149',
                borderUpColor:'#3fb950', borderDownColor:'#f85149',
                wickUpColor:'#3fb950', wickDownColor:'#f85149',
            });
            
            _lwChart.subscribeClick(function(param) {
                if (!param || !param.point) return;
                var clickedPrice = _lwSeries.coordinateToPrice(param.point.y);
                if (!clickedPrice) return;
                
                if (window._placementActiveMode) {
                    var price = Math.round(clickedPrice * 20) / 20;
                    window.setPriceLineValue(window._placementActiveMode, price);
                    window._placementActiveMode = null;
                    document.getElementById('dom-chart-canvas').style.cursor = 'default';
                    return;
                }
                
                // Click near existing line to edit it
                var breakoutPrice = parseFloat(document.getElementById('chart-breakout-val').value) || 0;
                var slPrice = parseFloat(document.getElementById('chart-sl-val').value) || 0;
                var tpPrice = parseFloat(document.getElementById('chart-tp-val').value) || 0;
                var tolerance = Math.max(1.5, clickedPrice * 0.025); // Forgiving 2.5% or 1.5 points minimum tolerance
                
                if (breakoutPrice > 0 && Math.abs(clickedPrice - breakoutPrice) < tolerance) {
                    window.chartClickPlacement('breakout');
                } else if (slPrice > 0 && Math.abs(clickedPrice - slPrice) < tolerance) {
                    window.chartClickPlacement('sl');
                } else if (tpPrice > 0 && Math.abs(clickedPrice - tpPrice) < tolerance) {
                    window.chartClickPlacement('tp');
                }
            });

            _lwChart.subscribeCrosshairMove(function(param) {
                window.repositionChartTags();
            });
        }

        var _lwRefreshTimer = null;
        var _lwExchangeSegment = 'NSE_FNO';
        window._lwFirstLoad = true;

        function loadChart(securityId, exchangeSegment) {
            _lwCurrentSecurity = securityId;
            _lwExchangeSegment = exchangeSegment || 'NSE_FNO';
            _liveBarTime = 0; _liveBarOpen = 0; _liveBarHigh = 0; _liveBarLow = Infinity;
            window._lwFirstLoad = true;
            if (!_lwChart) initLightweightChart();
            if (!_lwChart) return;
            _refreshChart();
            // Auto-refresh every 60s to update latest candle
            if (_lwRefreshTimer) clearInterval(_lwRefreshTimer);
            _lwRefreshTimer = setInterval(_refreshChart, 60000);
        }

        function _refreshChart() {
            if (!_lwCurrentSecurity || !_lwSeries) return;
            var container = document.getElementById('dom-chart-canvas');
            fetch('/api/chart/' + _lwCurrentSecurity + '?exchange_segment=' + _lwExchangeSegment)
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (!container || !_lwSeries) return;
                    if (d.candles && d.candles.length > 0) {
                        _lwSeries.setData(d.candles);
                        if (window._lwFirstLoad) {
                            _lwChart.timeScale().scrollToRealTime();
                            window._lwFirstLoad = false;
                        }
                        setTimeout(window.repositionChartTags, 100);
                    }
                })
                .catch(function(e) { console.error('Chart refresh error:', e); });
        }

        function switchDomTab(tab) {
            var depthDiv = document.getElementById('dom-chart');
            var chartDiv = document.getElementById('dom-chart-canvas-container');
            var canvasDiv = document.getElementById('dom-chart-canvas');
            var depthBtn = document.getElementById('dom-tab-depth');
            var chartBtn = document.getElementById('dom-tab-chart');
            var tfSelect = document.getElementById('chart-timeframe-group');
            if (tab === 'chart') {
                depthDiv.style.display = 'none';
                chartDiv.style.display = 'block';
                if (tfSelect) tfSelect.style.display = 'flex';
                depthBtn.style.fontWeight = '400'; depthBtn.style.borderBottomColor = 'transparent'; depthBtn.style.color = '#8b949e';
                chartBtn.style.fontWeight = '700'; chartBtn.style.borderBottomColor = '#3fb950';    chartBtn.style.color = '#3fb950';
                
                var h = window._chartMaximized ? 550 : 420;
                canvasDiv.style.height = h + 'px';
                if (_lwChart) _lwChart.applyOptions({width: canvasDiv.clientWidth || 600, height: h});
                setTimeout(window.repositionChartTags, 150);
            } else {
                depthDiv.style.display = 'block';
                chartDiv.style.display = 'none';
                if (tfSelect) tfSelect.style.display = 'none';
                depthBtn.style.fontWeight = '700'; depthBtn.style.borderBottomColor = '#3fb950';    depthBtn.style.color = '#3fb950';
                chartBtn.style.fontWeight = '400'; chartBtn.style.borderBottomColor = 'transparent'; chartBtn.style.color = '#8b949e';
            }
        }

        window.changeChartTimeframe = function() {
            _liveBarTime = 0; _liveBarOpen = 0; _liveBarHigh = 0; _liveBarLow = Infinity;
            _refreshChart();
            showToast('Timeframe changed. Live ticks will aggregate to ' + document.getElementById('chart-timeframe').value + 's bars.', 'info');
        };

        window.setChartTimeframe = function(seconds) {
            var el = document.getElementById('chart-timeframe');
            if (el) el.value = seconds;
            var tfs = [60, 15, 5];
            tfs.forEach(function(t) {
                var btn = document.getElementById('tf-btn-' + t);
                if (btn) {
                    if (t === seconds) {
                        btn.style.background = '#238636';
                        btn.style.borderColor = '#238636';
                        btn.style.color = '#ffffff';
                    } else {
                        btn.style.background = '#21262d';
                        btn.style.borderColor = '#30363d';
                        btn.style.color = '#8b949e';
                    }
                }
            });
            window.changeChartTimeframe();
        };

        // ── Maximization Toggle ──
        window._chartMaximized = false;
        window.toggleMaximizeChart = function() {
            var ocCol = document.getElementById('oc-workspace-col');
            var maxBtn = document.getElementById('dom-maximize-btn');
            var canvasDiv = document.getElementById('dom-chart-canvas');
            
            window._chartMaximized = !window._chartMaximized;
            
            if (window._chartMaximized) {
                if (ocCol) ocCol.style.display = 'none';
                maxBtn.textContent = '🗗 Minimize';
                maxBtn.style.background = '#21262d';
                canvasDiv.style.height = '550px';
                if (_lwChart) {
                    _lwChart.applyOptions({
                        width: document.getElementById('dom-chart-canvas-container').clientWidth || 1000,
                        height: 550
                    });
                }
            } else {
                if (ocCol) ocCol.style.display = 'block';
                maxBtn.textContent = '🗖 Maximize';
                maxBtn.style.background = 'none';
                canvasDiv.style.height = '420px';
                if (_lwChart) {
                    _lwChart.applyOptions({
                        width: document.getElementById('dom-chart-canvas-container').clientWidth || 600,
                        height: 420
                    });
                }
            }
            setTimeout(window.repositionChartTags, 150);
        };

        // ── Floating Order Tags Repositioning ──
        window.repositionChartTags = function() {
            if (!_lwSeries || !_lwChart) return;
            
            var breakoutPrice = parseFloat(document.getElementById('chart-breakout-val').value) || 0;
            var slPrice = parseFloat(document.getElementById('chart-sl-val').value) || 0;
            
            var tagBreakout = document.getElementById('chart-tag-breakout');
            var tagSL = document.getElementById('chart-tag-sl');
            var canvasDiv = document.getElementById('dom-chart-canvas');
            var maxHeight = canvasDiv ? (canvasDiv.clientHeight || 420) : 420;
            
            if (breakoutPrice > 0 && tagBreakout) {
                var y = _lwSeries.priceToCoordinate(breakoutPrice);
                if (y !== null && y >= 0 && y <= maxHeight) {
                    tagBreakout.style.display = 'flex';
                    tagBreakout.style.top = (y - 12) + 'px';
                    var inputEl = document.getElementById('chart-tag-breakout-input');
                    if (inputEl && document.activeElement !== inputEl) {
                        inputEl.value = breakoutPrice.toFixed(2);
                    }
                    var qtyEl = document.getElementById('chart-tag-breakout-qty-input');
                    var currentQty = parseFloat(document.getElementById('sqb-qty-override').value) || parseFloat(document.getElementById('sqb-qty').textContent) || 50;
                    if (qtyEl && document.activeElement !== qtyEl) {
                        qtyEl.value = currentQty;
                    }
                } else {
                    tagBreakout.style.display = 'none';
                }
            } else if (tagBreakout) {
                tagBreakout.style.display = 'none';
            }
            
            if (slPrice > 0 && tagSL) {
                var y = _lwSeries.priceToCoordinate(slPrice);
                if (y !== null && y >= 0 && y <= maxHeight) {
                    tagSL.style.display = 'flex';
                    tagSL.style.top = (y - 12) + 'px';
                    var inputEl = document.getElementById('chart-tag-sl-input');
                    if (inputEl && document.activeElement !== inputEl) {
                        inputEl.value = slPrice.toFixed(2);
                    }
                    var labelEl = document.getElementById('chart-tag-sl-label');
                    if (labelEl) {
                        if (breakoutPrice > 0) {
                            var diff = slPrice - breakoutPrice;
                            var qty = parseFloat(document.getElementById('sqb-qty-override').value) || parseFloat(document.getElementById('sqb-qty').textContent) || 50;
                            var totalLoss = Math.abs(diff) * qty;
                            labelEl.textContent = 'STOP LOSS LIMIT (' + (diff >= 0 ? '+' : '') + diff.toFixed(2) + ' pts | -₹' + totalLoss.toFixed(2) + ')';
                        } else {
                            labelEl.textContent = 'STOP LOSS LIMIT';
                        }
                    }
                } else {
                    tagSL.style.display = 'none';
                }
            } else if (tagSL) {
                tagSL.style.display = 'none';
            }
        };

        window.updateQtyFromTag = function() {
            var val = parseInt(document.getElementById('chart-tag-breakout-qty-input').value) || 0;
            if (val > 0) {
                var overrideInput = document.getElementById('sqb-qty-override');
                if (overrideInput) {
                    overrideInput.value = val;
                }
            }
        };

        window.updatePriceFromTag = function(mode) {
            var val = parseFloat(document.getElementById('chart-tag-' + mode + '-input').value) || 0;
            if (val > 0) {
                var price = Math.round(val * 20) / 20; // round to nearest tick
                window.setPriceLineValue(mode, price);
            }
        };

        // ── Chart Price Line Controls ──────────────────────────
        window._placementActiveMode = null;
        window._priceLine_breakout = null;
        window._priceLine_sl = null;
        window._priceLine_tp = null;

        window.setPriceLineValue = function(mode, price) {
            var inputId = 'chart-' + mode + '-val';
            var input = document.getElementById(inputId);
            if (input) input.value = price.toFixed(2);
            
            var color = '#ff9900';
            var title = 'Breakout';
            if (mode === 'sl') { color = '#f85149'; title = 'Stop Loss'; }
            if (mode === 'tp') { color = '#3fb950'; title = 'Take Profit'; }
            
            var lineVar = '_priceLine_' + mode;
            if (window[lineVar]) {
                try {
                    _lwSeries.removePriceLine(window[lineVar]);
                } catch (e) {}
                window[lineVar] = null;
            }
            
            if (_lwSeries) {
                window[lineVar] = _lwSeries.createPriceLine({
                    price: price,
                    color: color,
                    lineWidth: 2,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: title,
                });
                if (typeof sqbAutoCalc === 'function') sqbAutoCalc();
                window.repositionChartTags();
            }
        };

        window.updatePriceLineFromInput = function(mode) {
            var input = document.getElementById('chart-' + mode + '-val');
            if (input && input.value) {
                var price = parseFloat(input.value);
                window.setPriceLineValue(mode, price);
            }
        };

        window.chartClickPlacement = function(mode) {
            window._placementActiveMode = mode;
            var canvas = document.getElementById('dom-chart-canvas');
            if (canvas) canvas.style.cursor = 'crosshair';
            showToast('Click on the chart to place ' + mode.toUpperCase() + ' line', 'info');
        };

        window.submitChartTriggerOrders = function() {
            var breakout = parseFloat(document.getElementById('chart-breakout-val').value) || 0;
            var sl = parseFloat(document.getElementById('chart-sl-val').value) || 0;
            var tp = parseFloat(document.getElementById('chart-tp-val').value) || 0;
            
            if (breakout <= 0) {
                showToast('Please set a valid Breakout trigger price first!', 'error');
                return;
            }
            
            showToast('Transmitting trigger orders...', 'info');
            
            var payload = {
                security_id: _lwCurrentSecurity,
                exchange_segment: _lwExchangeSegment,
                breakout_price: breakout,
                sl_price: sl,
                tp_price: tp,
                quantity: parseFloat(document.getElementById('sqb-qty-override').value) || parseFloat(document.getElementById('sqb-qty').textContent) || 0
            };
            
            if (payload.quantity <= 0) {
                showToast('Please specify order quantity (override or calculation)', 'error');
                return;
            }
            
            fetch('/api/order/place_trigger_chart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.status === 'success') {
                    showToast('Trigger orders placed successfully!', 'success');
                } else {
                    showToast('Failed: ' + (d.message || 'Error'), 'error');
                }
            })
            .catch(function(e) {
                showToast('Request failed: ' + e, 'error');
            });
        };

        window.submitStopLoss1Click = function() {
            var slPrice = parseFloat(document.getElementById('chart-sl-val').value) || 0;
            if (slPrice <= 0) {
                showToast('Please set a valid Stop Loss price line first!', 'error');
                return;
            }
            
            showToast('1-Click SL Triggered! Submitting native exchange SL order...', 'info');
            
            var payload = {
                security_id: _lwCurrentSecurity,
                exchange_segment: _lwExchangeSegment,
                sl_price: slPrice,
                quantity: parseFloat(document.getElementById('sqb-qty-override').value) || parseFloat(document.getElementById('sqb-qty').textContent) || 0
            };
            
            if (payload.quantity <= 0) {
                showToast('Please specify quantity first!', 'error');
                return;
            }
            
            fetch('/api/order/submit_sl_1click', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.status === 'success') {
                    showToast('Stop Loss order submitted successfully!', 'success');
                } else {
                    showToast('Failed to place SL order: ' + (d.message || 'Error'), 'error');
                }
            })
            .catch(function(e) {
                showToast('Request failed: ' + e, 'error');
            });
        };

        // Keyboard Shortcut Listener: Spacebar -> 1-Click SL
        document.addEventListener('keydown', function(e) {
            if (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA') {
                return;
            }
            if (e.code === 'Space') {
                e.preventDefault();
                window.submitStopLoss1Click();
            }
        });

        // ── Depth of Market ─────────────────────────────────────────────
        var _domCurrentSecurity = null;
        var _domLastUpdate = 0;
        var _domLastAnalysis = null;

        function subscribeDepth(securityId, exchangeSegment, symbol) {
            _domCurrentSecurity = securityId;
            _domLastUpdate = Date.now();
            _domLastAnalysis = null;
            console.log('[DOM] Subscribing:', securityId, exchangeSegment, symbol);
            document.getElementById('dom-instrument').textContent = symbol || ('Loading ' + securityId + '...');
            document.getElementById('dom-analysis').style.display = 'none';
            document.getElementById('dom-chart').innerHTML =
                '<div style="color:#484f58;padding:30px;text-align:center;font-size:13px;">' +
                'Connecting to depth feed...' +
                '<div style="margin-top:8px;font-size:11px;"><a href="/api/depth-diag" target="_blank" style="color:#58a6ff;">Run diagnostics</a></div>' +
                '</div>';
            document.getElementById('dom-status').style.display = 'inline';
            document.getElementById('dom-close-btn').style.display = 'inline';
            document.getElementById('dom-status').textContent = 'LIVE';
            document.getElementById('dom-status').style.background = '#0d4429';
            document.getElementById('dom-status').style.color = '#3fb950';
            if (socket) {
                socket.emit('subscribe_depth', {security_id: securityId, exchange_segment: exchangeSegment});
            } else {
                console.error('[DOM] No socket connection!');
                document.getElementById('dom-chart').innerHTML =
                    '<div style="color:#f85149;padding:20px;text-align:center;">Socket not connected. Refresh the page.</div>';
            }
        }

        function domClose() {
            _domCurrentSecurity = null;
            _domLastAnalysis = null;
            if (socket) socket.emit('unsubscribe_depth', {});
            document.getElementById('dom-instrument').textContent = 'Select an option from the chain';
            document.getElementById('dom-analysis').style.display = 'none';
            document.getElementById('dom-status').style.display = 'none';
            document.getElementById('dom-close-btn').style.display = 'none';
            document.getElementById('dom-chart').innerHTML =
                '<div style="color:#484f58;padding:30px;text-align:center;font-size:13px;">Click any CE or PE price in the option chain to view 20-level market depth</div>';
        }

        // Staleness detection — check every second
        setInterval(function() {
            if (!_domCurrentSecurity) return;
            var elapsed = Date.now() - _domLastUpdate;
            var statusEl = document.getElementById('dom-status');
            if (elapsed > 5000) {
                statusEl.textContent = 'STALE';
                statusEl.style.background = '#3d1f00';
                statusEl.style.color = '#d29922';
            } else if (elapsed > 3000) {
                statusEl.textContent = 'DELAYED';
                statusEl.style.background = '#3d2e00';
                statusEl.style.color = '#d29922';
            } else {
                statusEl.textContent = 'LIVE';
                statusEl.style.background = '#0d4429';
                statusEl.style.color = '#3fb950';
            }
        }, 1000);

        function setupDepthListener() {
            if (!socket) { console.error('[DOM] setupDepthListener: no socket'); return; }
            console.log('[DOM] Setting up depth listeners');
            socket.on('depth_update', function(data) {
                if (data.security_id && data.security_id !== _domCurrentSecurity) return;
                _domLastUpdate = Date.now();
                _domLastAnalysis = data.analysis || {};
                renderDepthChart(data.bids || [], data.asks || [], _domLastAnalysis);
                renderDepthAnalysis(_domLastAnalysis);
            });
            socket.on('depth_error', function(data) {
                console.error('[DOM] depth_error:', data);
                document.getElementById('dom-chart').innerHTML =
                    '<div style="color:#f85149;padding:20px;text-align:center;">' + (data.error || 'Depth error') + '</div>';
            });
            socket.on('depth_status', function(data) {
                console.warn('[DOM] depth_status:', data);
                if (data.status === 'no_data') {
                    document.getElementById('dom-chart').innerHTML =
                        '<div style="color:#d29922;padding:30px;text-align:center;font-size:13px;">' +
                        'No depth data received \u2014 market may be closed' +
                        '<div style="color:#484f58;font-size:11px;margin-top:6px;">Data will appear automatically when the market opens</div>' +
                        '</div>';
                } else if (data.status === 'connection_failed') {
                    var reason = data.reason || 'unknown';
                    document.getElementById('dom-chart').innerHTML =
                        '<div style="color:#f85149;padding:30px;text-align:center;font-size:13px;">' +
                        'Depth feed connection failed' +
                        '<div style="color:#d29922;font-size:11px;margin-top:6px;">' + reason + '</div>' +
                        '<div style="color:#484f58;font-size:10px;margin-top:4px;">Check Dhan depth API subscription and access token</div>' +
                        '<div style="margin-top:8px;"><a href="/api/depth-diag" target="_blank" style="color:#58a6ff;font-size:11px;">Run diagnostics</a></div>' +
                        '</div>';
                }
            });
        }

        function fmtQty(n) {
            if (n >= 100000) return (n / 100000).toFixed(1) + 'L';
            if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
            return n.toString();
        }

        function renderDepthChart(bids, asks, analysis) {
            if (!bids.length && !asks.length) return;
            analysis = analysis || {};

            // Determine wall prices for highlighting
            var buyWallPrice = (analysis.max_bid_wall && analysis.max_bid_wall.price) || null;
            var sellWallPrice = (analysis.max_ask_wall && analysis.max_ask_wall.price) || null;

            // Find max quantity for bar scaling
            var maxQty = 1;
            bids.forEach(function(b) { if (b.quantity > maxQty) maxQty = b.quantity; });
            asks.forEach(function(a) { if (a.quantity > maxQty) maxQty = a.quantity; });

            // Build combined ladder: asks (descending by price) then bids (descending by price)
            var askRows = asks.slice().reverse(); // Show highest ask at top
            var bidRows = bids.slice(); // Best bid first

            var html = '<table style="width:100%;border-collapse:collapse;font-size:10px;font-variant-numeric:tabular-nums;table-layout:fixed;">';
            html += '<thead style="position:sticky;top:0;background:#0d1117;z-index:1;">';
            html += '<tr>';
            html += '<th style="text-align:right;padding:2px 4px;color:#3fb950;font-size:9px;width:35%;">BID QTY</th>';
            html += '<th style="text-align:center;padding:2px 4px;color:#e6edf3;font-size:9px;width:30%;">PRICE</th>';
            html += '<th style="text-align:left;padding:2px 4px;color:#f85149;font-size:9px;width:35%;">ASK QTY</th>';
            html += '</tr></thead><tbody>';

            // Ask rows (sell side) - top of ladder
            askRows.forEach(function(a) {
                var pct = Math.round((a.quantity / maxQty) * 100);
                var isWall = sellWallPrice && a.price === sellWallPrice;
                var rowStyle = 'cursor:pointer;' + (isWall ? 'background:rgba(248,81,73,0.1);' : '');
                html += '<tr style="' + rowStyle + '" onclick="domSelectPrice(' + a.price + ')">';
                html += '<td style="padding:1px 4px;text-align:right;color:#484f58;">-</td>';
                html += '<td style="padding:1px 4px;text-align:center;color:#f85149;font-weight:' + (isWall ? '800' : '600') + ';cursor:pointer;font-size:9px;">' + a.price.toFixed(2) + (isWall ? ' \\u25C0' : '') + '</td>';
                html += '<td style="padding:1px 4px;text-align:left;">';
                html += '<div style="display:flex;align-items:center;gap:2px;">';
                html += '<div style="background:rgba(248,81,73,' + (isWall ? '0.45' : '0.25') + ');height:12px;width:' + pct + '%;min-width:2px;border-radius:1px;"></div>';
                html += '<span style="color:#f85149;font-size:9px;white-space:nowrap;' + (isWall ? 'font-weight:700;' : '') + '">' + fmtQty(a.quantity) + '</span>';
                html += '</div></td>';
                html += '</tr>';
            });

            // Spread row
            if (bids.length && asks.length) {
                var spread = (asks[0].price - bids[0].price).toFixed(2);
                html += '<tr style="border-top:1px solid #30363d;border-bottom:1px solid #30363d;background:#161b22;">';
                html += '<td colspan="3" style="padding:2px 4px;text-align:center;color:#8b949e;font-size:9px;">Spread: \\u20B9' + spread + '</td>';
                html += '</tr>';
            }

            // Bid rows (buy side) - bottom of ladder
            bidRows.forEach(function(b) {
                var pct = Math.round((b.quantity / maxQty) * 100);
                var isWall = buyWallPrice && b.price === buyWallPrice;
                var rowStyle = 'cursor:pointer;' + (isWall ? 'background:rgba(63,185,80,0.1);' : '');
                html += '<tr style="' + rowStyle + '" onclick="domSelectPrice(' + b.price + ')">';
                html += '<td style="padding:1px 4px;text-align:right;">';
                html += '<div style="display:flex;align-items:center;justify-content:flex-end;gap:2px;">';
                html += '<span style="color:#3fb950;font-size:9px;white-space:nowrap;' + (isWall ? 'font-weight:700;' : '') + '">' + fmtQty(b.quantity) + '</span>';
                html += '<div style="background:rgba(63,185,80,' + (isWall ? '0.45' : '0.25') + ');height:12px;width:' + pct + '%;min-width:2px;border-radius:1px;"></div>';
                html += '</div></td>';
                html += '<td style="padding:1px 4px;text-align:center;color:#3fb950;font-weight:' + (isWall ? '800' : '600') + ';cursor:pointer;font-size:9px;">' + (isWall ? '\\u25B6 ' : '') + b.price.toFixed(2) + '</td>';
                html += '<td style="padding:1px 4px;text-align:left;color:#484f58;">-</td>';
                html += '</tr>';
            });

            html += '</tbody></table>';
            document.getElementById('dom-chart').innerHTML = html;
        }

        function renderDepthAnalysis(a) {
            if (!a || !a.sentiment) return;
            var el = document.getElementById('dom-analysis');
            el.style.display = 'block';

            // Sentiment badge
            var sentEl = document.getElementById('dom-sentiment');
            sentEl.textContent = a.sentiment;
            if (a.sentiment === 'BULLISH') {
                sentEl.style.background = 'rgba(63,185,80,0.15)'; sentEl.style.color = '#3fb950';
                sentEl.style.border = '1px solid rgba(63,185,80,0.3)';
            } else if (a.sentiment === 'BEARISH') {
                sentEl.style.background = 'rgba(248,81,73,0.15)'; sentEl.style.color = '#f85149';
                sentEl.style.border = '1px solid rgba(248,81,73,0.3)';
            } else {
                sentEl.style.background = 'rgba(139,148,158,0.15)'; sentEl.style.color = '#8b949e';
                sentEl.style.border = '1px solid rgba(139,148,158,0.3)';
            }

            // Spread
            document.getElementById('dom-spread').textContent = '\\u20B9' + a.bid_ask_spread;
            document.getElementById('dom-spread-pct').textContent = ' (' + a.spread_pct + '%)';

            // Imbalance
            var imbEl = document.getElementById('dom-imbalance');
            imbEl.textContent = a.imbalance_ratio + 'x';
            imbEl.style.color = a.imbalance_ratio > 1.2 ? '#3fb950' : a.imbalance_ratio < 0.8 ? '#f85149' : '#8b949e';

            // Walls
            if (a.max_bid_wall) {
                document.getElementById('dom-buy-wall').textContent =
                    '\\u20B9' + a.max_bid_wall.price + ' (' + fmtQty(a.max_bid_wall.quantity) + ', ' + a.max_bid_wall.orders + ' orders)';
            }
            if (a.max_ask_wall) {
                document.getElementById('dom-sell-wall').textContent =
                    '\\u20B9' + a.max_ask_wall.price + ' (' + fmtQty(a.max_ask_wall.quantity) + ', ' + a.max_ask_wall.orders + ' orders)';
            }

            // Totals
            document.getElementById('dom-total-bid').textContent = fmtQty(a.total_bid_qty);
            document.getElementById('dom-total-ask').textContent = fmtQty(a.total_ask_qty);

            // Imbalance bar
            var total = a.total_bid_qty + a.total_ask_qty;
            if (total > 0) {
                var bidPct = Math.round((a.total_bid_qty / total) * 100);
                document.getElementById('dom-imbalance-bar-bid').style.width = bidPct + '%';
                document.getElementById('dom-imbalance-bar-ask').style.width = (100 - bidPct) + '%';
            }

            // Support levels (bid levels with qty > 1.5x average)
            var supportEl = document.getElementById('dom-support');
            if (a.support_levels && a.support_levels.length > 0) {
                var topSupport = a.support_levels[0];
                supportEl.textContent = '\\u20B9' + topSupport.price + ' (' + fmtQty(topSupport.quantity) + ')';
                if (a.support_levels.length > 1) {
                    supportEl.textContent += ' +' + (a.support_levels.length - 1);
                }
            } else {
                supportEl.textContent = '--';
            }

            // Resistance levels (ask levels with qty > 1.5x average)
            var resEl = document.getElementById('dom-resistance');
            if (a.resistance_levels && a.resistance_levels.length > 0) {
                var topRes = a.resistance_levels[0];
                resEl.textContent = '\\u20B9' + topRes.price + ' (' + fmtQty(topRes.quantity) + ')';
                if (a.resistance_levels.length > 1) {
                    resEl.textContent += ' +' + (a.resistance_levels.length - 1);
                }
            } else {
                resEl.textContent = '--';
            }
        }

        // Click DOM price -> fill order form
        function domSelectPrice(price) {
            document.getElementById('order-price').value = price;
            if (typeof triggerAutoCalc === 'function') triggerAutoCalc();
            showToast('Price set: \\u20B9' + price.toFixed(2), 'success');
        }

        // Wire up depth listener when socket connects
        var _origSetupSocketListeners = (typeof setupSocketListeners === 'function') ? setupSocketListeners : null;
        setupSocketListeners = function() {
            if (_origSetupSocketListeners) _origSetupSocketListeners();
            setupDepthListener();
        };
        // If socket is already connected, set up now
        if (socket) setupDepthListener();

        // VPS Admin Modal Helpers
        function openAdminModal() {
            document.getElementById('vps-admin-modal').style.display = 'flex';
        }
        function closeAdminModal() {
            document.getElementById('vps-admin-modal').style.display = 'none';
        }
        function copyText(text, btn) {
            navigator.clipboard.writeText(text).then(function() {
                var oldText = btn.textContent;
                btn.textContent = 'Copied!';
                btn.style.borderColor = '#238636';
                btn.style.color = '#3fb950';
                setTimeout(function() {
                    btn.textContent = oldText;
                    btn.style.borderColor = '';
                    btn.style.color = '';
                }, 1500);
            }).catch(function(err) {
                console.error('Could not copy text: ', err);
            });
        }
        function runReloadConfig(btn) {
            btn.disabled = true;
            btn.textContent = 'Running...';
            fetch('/api/admin/reload_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'}
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                btn.disabled = false;
                if (d.status === 'success' || d.status === 'ok') {
                    btn.textContent = 'Success!';
                    btn.style.backgroundColor = '#238636';
                    btn.style.borderColor = '#2ea043';
                    showToast('Configuration reloaded successfully!', 'success');
                    setTimeout(function() {
                        btn.textContent = 'Run';
                        btn.style.backgroundColor = '#1f6feb';
                        btn.style.borderColor = '#388bfd';
                    }, 2000);
                } else {
                    btn.textContent = 'Failed';
                    btn.style.backgroundColor = '#da3637';
                    btn.style.borderColor = '#f85149';
                    showToast('Reload failed: ' + (d.message || ''), 'error');
                    setTimeout(function() {
                        btn.textContent = 'Run';
                        btn.style.backgroundColor = '#1f6feb';
                        btn.style.borderColor = '#388bfd';
                    }, 2000);
                }
            })
            .catch(function(err) {
                btn.disabled = false;
                btn.textContent = 'Error';
                btn.style.backgroundColor = '#da3637';
                btn.style.borderColor = '#f85149';
                showToast('Network error: ' + err, 'error');
                setTimeout(function() {
                    btn.textContent = 'Run';
                    btn.style.backgroundColor = '#1f6feb';
                    btn.style.borderColor = '#388bfd';
                }, 2000);
            });
        }
    </script>

    <!-- VPS Admin Commands Modal -->
    <div id="vps-admin-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:99999;align-items:center;justify-content:center;backdrop-filter:blur(4px);">
        <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;width:90%;max-width:600px;padding:24px;box-shadow:0 8px 32px rgba(0,0,0,0.5);font-family:sans-serif;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;border-bottom:1px solid #30363d;padding-bottom:12px;">
                <h3 style="margin:0;color:#c9d1d9;font-size:16px;display:flex;align-items:center;gap:8px;">⚙️ VPS Admin Shortcuts</h3>
                <button onclick="closeAdminModal()" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:20px;font-weight:700;">&times;</button>
            </div>
            <p style="color:#8b949e;font-size:12px;margin-top:0;margin-bottom:16px;">Click on any command block to copy it to your clipboard. Run these inside your VPS terminal.</p>
            <div style="display:flex;flex-direction:column;gap:14px;max-height:400px;overflow-y:auto;padding-right:4px;">
                
                <!-- Command 1 -->
                <div>
                    <label style="display:block;color:#58a6ff;font-size:11px;font-weight:600;margin-bottom:4px;text-transform:uppercase;">🔓 Reset Lockout & Profit Lock Floors (Online)</label>
                    <div style="display:flex;gap:8px;">
                        <code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all;display:block;">curl -X POST http://localhost:5555/api/admin/reset_lockout</code>
                        <button class="btn-neutral btn-sm" onclick="copyText('curl -X POST http://localhost:5555/api/admin/reset_lockout', this)" style="font-size:11px;">Copy</button>
                    </div>
                </div>

                <!-- Command 2 -->
                <div>
                    <label style="display:block;color:#58a6ff;font-size:11px;font-weight:600;margin-bottom:4px;text-transform:uppercase;">🔄 Reset Peak HWM Only (Online)</label>
                    <div style="display:flex;gap:8px;">
                        <code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all;display:block;">curl -X POST http://localhost:5555/api/admin/reset_hwm</code>
                        <button class="btn-neutral btn-sm" onclick="copyText('curl -X POST http://localhost:5555/api/admin/reset_hwm', this)" style="font-size:11px;">Copy</button>
                    </div>
                </div>

                <!-- Command 3 -->
                <div>
                    <label style="display:block;color:#f85149;font-size:11px;font-weight:600;margin-bottom:4px;text-transform:uppercase;">🛑 Disable Daily Auto-Restarts</label>
                    <div style="display:flex;gap:8px;">
                        <code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all;display:block;">sudo systemctl stop risk-manager-restart.timer && sudo systemctl disable risk-manager-restart.timer && sudo systemctl disable risk-manager</code>
                        <button class="btn-neutral btn-sm" onclick="copyText('sudo systemctl stop risk-manager-restart.timer && sudo systemctl disable risk-manager-restart.timer && sudo systemctl disable risk-manager', this)" style="font-size:11px;">Copy</button>
                    </div>
                </div>

                <!-- Command 4 -->
                <div>
                    <label style="display:block;color:#3fb950;font-size:11px;font-weight:600;margin-bottom:4px;text-transform:uppercase;">🟢 Enable Daily Auto-Restarts</label>
                    <div style="display:flex;gap:8px;">
                        <code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all;display:block;">sudo systemctl enable --now risk-manager-restart.timer risk-manager</code>
                        <button class="btn-neutral btn-sm" onclick="copyText('sudo systemctl enable --now risk-manager-restart.timer risk-manager', this)" style="font-size:11px;">Copy</button>
                    </div>
                </div>

                <!-- Command 5 -->
                <div>
                    <label style="display:block;color:#d29922;font-size:11px;font-weight:600;margin-bottom:4px;text-transform:uppercase;">📊 View Live Status Diagnostics</label>
                    <div style="display:flex;gap:8px;">
                        <code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all;display:block;">cd ~/Risk-Management && ./venv/bin/python main.py --status</code>
                        <button class="btn-neutral btn-sm" onclick="copyText('cd ~/Risk-Management && ./venv/bin/python main.py --status', this)" style="font-size:11px;">Copy</button>
                    </div>
                </div>

                <!-- Command 6 -->
                <div>
                    <label style="display:block;color:#8b949e;font-size:11px;font-weight:600;margin-bottom:4px;text-transform:uppercase;">🛠️ Reset Lockout Offline (When Service is Down)</label>
                    <div style="display:flex;gap:8px;">
                        <code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all;display:block;">cd ~/Risk-Management && ./venv/bin/python reset_lockout.py</code>
                        <button class="btn-neutral btn-sm" onclick="copyText('cd ~/Risk-Management && ./venv/bin/python reset_lockout.py', this)" style="font-size:11px;">Copy</button>
                    </div>
                </div>

                <!-- Command 7 -->
                <div>
                    <label style="display:block;color:#58a6ff;font-size:11px;font-weight:600;margin-bottom:4px;text-transform:uppercase;">🔄 Reload Configuration (.env) in Memory (Online)</label>
                    <div style="display:flex;gap:8px;">
                        <code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all;display:block;">curl -X POST http://localhost:5555/api/admin/reload_config</code>
                        <button class="btn-neutral btn-sm" onclick="copyText('curl -X POST http://localhost:5555/api/admin/reload_config', this)" style="font-size:11px;">Copy</button>
                        <button class="btn-neutral btn-sm" onclick="runReloadConfig(this)" style="font-size:11px;background:#1f6feb;border-color:#388bfd;color:#fff;">Run</button>
                    </div>
                </div>

            </div>
            <div style="display:flex;justify-content:flex-end;margin-top:16px;border-top:1px solid #30363d;padding-top:12px;">
                <button class="btn-neutral" onclick="closeAdminModal()">Close</button>
            </div>
        </div>
    </div>

    <!-- Lot Size Warning Overlay Modal -->
    <div id="lot-warn-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:99999;align-items:center;justify-content:center;backdrop-filter:blur(4px);">
        <div style="background:#161b22;border:2px solid #f85149;border-radius:12px;padding:24px;width:90%;max-width:420px;text-align:center;box-shadow:0 10px 25px rgba(0,0,0,0.8);">
            <div style="font-size:48px;color:#f85149;margin-bottom:12px;">⚠</div>
            <h2 style="color:#f85149;margin:0 0 12px;font-size:20px;text-transform:uppercase;letter-spacing:1px;font-family:sans-serif;">Position Limit Exceeded</h2>
            <p style="font-size:14px;color:#c9d1d9;line-height:1.5;margin-bottom:16px;font-family:sans-serif;">
                Your open position for <strong id="lot-warn-symbol" style="color:#ffffff;">--</strong> has <strong id="lot-warn-qty" style="color:#ffffff;">0</strong> quantity. 
                This exceeds your safety limit of <strong id="lot-warn-limit" style="color:#ffffff;">0</strong>.
            </p>
            <div style="background:#21262d;border:1px solid #30363d;border-radius:8px;padding:12px;font-size:13px;color:#8b949e;margin-bottom:20px;font-family:sans-serif;">
                Reduce or close this position immediately or it will be auto-liquidated in:
                <div id="lot-warn-timer" style="font-size:32px;font-weight:700;color:#f85149;margin-top:8px;font-family:monospace;">25s</div>
            </div>
            <p style="font-size:11px;color:#484f58;margin:0;font-family:sans-serif;">(Direct terminal execution limit enforcement)</p>
        </div>
    </div>

    <!-- Right-Click Context Menu -->
    <div id="chart-context-menu" style="display:none;position:fixed;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:4px 0;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,0.6);width:170px;user-select:none;font-family:sans-serif;">
        <div onclick="selectContextAction('breakout')" style="padding:8px 14px;font-size:12px;color:#ff9900;cursor:pointer;font-weight:600;" onmouseover="this.style.background='#21262d'" onmouseout="this.style.background='none'">Place Breakout here</div>
        <div onclick="selectContextAction('sl')" style="padding:8px 14px;font-size:12px;color:#f85149;cursor:pointer;font-weight:600;" onmouseover="this.style.background='#21262d'" onmouseout="this.style.background='none'">Place Stop Loss here</div>
        <div onclick="selectContextAction('tp')" style="padding:8px 14px;font-size:12px;color:#3fb950;cursor:pointer;font-weight:600;" onmouseover="this.style.background='#21262d'" onmouseout="this.style.background='none'">Place Take Profit here</div>
    </div>

    <script>
        // Context menu pricing state
        window._ctxPrice = 0;
        
        // Listen to right-click on the chart canvas container
        setTimeout(function() {
            var chartCanvas = document.getElementById('dom-chart-canvas');
            if (chartCanvas) {
                chartCanvas.addEventListener('contextmenu', function(e) {
                    e.preventDefault();
                    if (!_lwSeries || !_lwChart) return;
                    
                    var rect = chartCanvas.getBoundingClientRect();
                    var y = e.clientY - rect.top;
                    var price = _lwSeries.coordinateToPrice(y);
                    
                    if (price) {
                        window._ctxPrice = Math.round(price * 20) / 20; // round to 0.05
                        var menu = document.getElementById('chart-context-menu');
                        if (menu) {
                            menu.style.left = e.clientX + 'px';
                            menu.style.top = e.clientY + 'px';
                            menu.style.display = 'block';
                        }
                    }
                });
            }
        }, 1000);
        
        // Hide menu when clicking elsewhere
        document.addEventListener('click', function(e) {
            var menu = document.getElementById('chart-context-menu');
            if (menu && !menu.contains(e.target)) {
                menu.style.display = 'none';
            }
        });
        
        window.selectContextAction = function(type) {
            var price = window._ctxPrice;
            var menu = document.getElementById('chart-context-menu');
            if (menu) menu.style.display = 'none';
            if (price <= 0) return;
            
            if (type === 'breakout') {
                window.setPriceLineValue('breakout', price);
            } else if (type === 'sl') {
                window.setPriceLineValue('sl', price);
            } else if (type === 'tp') {
                window.setPriceLineValue('tp', price);
            }
            showToast('Placed ' + type.toUpperCase() + ' line at ' + price.toFixed(2), 'success');
        };
    </script>
</body>
</html>
"""


@app.route("/api/broker/active")
def api_broker_active():
    """Get active broker."""
    return jsonify({"status": "ok", "active_broker": Config.ACTIVE_BROKER})


@app.route("/api/broker/toggle", methods=["POST"])
def api_broker_toggle():
    """Toggle the active broker at runtime."""
    data = request.json or {}
    broker = data.get("broker", "DHAN").upper()
    if broker not in ("DHAN", "KOTAK"):
        return jsonify({"status": "error", "message": "Invalid broker name"}), 400

    # Switch in BrokerAPI router
    success = _monitor.api.set_active_broker(broker) if _monitor else False
    if not success:
        # Fallback to direct config switch if monitor not initialized
        Config.ACTIVE_BROKER = broker
        success = True

    # Reload the instrument cache for the new broker!
    try:
        global _instrument_cache
        _instrument_cache = InstrumentCache()
        _instrument_cache.load()
        set_instrument_cache(_instrument_cache)
        logger.info("Instrument cache reloaded successfully on toggle to: %s", broker)
    except Exception as e:
        logger.error("Failed to reload instrument cache: %s", e)

    # Force reset the depth WS on next load
    global _depth_ws
    if _depth_ws:
        try:
            _depth_ws.stop()
        except Exception:
            pass
        _depth_ws = None

    return jsonify({"status": "ok", "active_broker": broker})


@app.route("/api/order/place_trigger_chart", methods=["POST"])
def place_trigger_chart():
    """Submit breakout entry order at exchange and save SL/TP protective targets in memory."""
    data = request.json or {}
    security_id = data.get("security_id")
    segment = data.get("exchange_segment", "NSE_FNO")
    breakout = float(data.get("breakout_price", 0))
    sl = float(data.get("sl_price", 0))
    tp = float(data.get("tp_price", 0))
    qty = int(data.get("quantity", 0))
    
    if not security_id or breakout <= 0 or qty <= 0:
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400
        
    # Get current LTP to determine transaction direction
    try:
        res = _monitor.api.get_ltp({segment: [int(security_id)]})
        ltp = 0.0
        if isinstance(res, dict):
            if security_id in res:
                ltp = float(res[security_id] or 0)
            elif str(security_id) in res:
                ltp = float(res[str(security_id)] or 0)
    except Exception:
        ltp = 0.0
        
    if ltp <= 0:
        ltp = breakout
        
    tx_type = "BUY" if breakout > ltp else "SELL"
    
    if not hasattr(_monitor, "chart_sl_tp"):
        _monitor.chart_sl_tp = {}
    _monitor.chart_sl_tp[str(security_id)] = {
        "sl": sl,
        "tp": tp,
        "qty": qty,
        "tx_type": tx_type
    }
    
    buffer = round(max(0.50, breakout * 0.01) * 20) / 20
    limit_price = breakout + buffer if tx_type == "BUY" else max(0.05, breakout - buffer)
    
    try:
        res_order = _monitor.api.place_order(
            security_id=security_id,
            exchange_segment=segment,
            transaction_type=tx_type,
            quantity=qty,
            order_type="STOP_LOSS_LIMIT",
            product_type="MARGIN",
            price=limit_price,
            trigger_price=breakout
        )
        return jsonify({"status": "success", "data": res_order})
    except Exception as e:
        logger.error("Failed to place chart breakout order: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


def _cancel_existing_sl_orders(security_id: str):
    """Cancel any existing pending SL or Break-Even SL orders for a specific contract before replacing."""
    if not _monitor:
        return
    try:
        orders = _monitor.api.get_order_book() or []
        for o in orders:
            o_sec_id = str(o.get("securityId", ""))
            status = str(o.get("orderStatus", "")).upper()
            if o_sec_id == str(security_id) and status in ("PENDING", "TRANSIT"):
                order_id = str(o.get("orderId", ""))
                if order_id:
                    logger.info("Cancelling existing pending SL order %s for security_id %s to replace with new SL", order_id, security_id)
                    try:
                        _monitor.api.cancel_order(order_id)
                    except Exception as cx:
                        logger.warning("Could not cancel order %s: %s", order_id, cx)
    except Exception as e:
        logger.warning("Error while checking existing SL orders for %s: %s", security_id, e)


@app.route("/api/order/submit_sl_1click", methods=["POST"])
def submit_sl_1click():
    """Instantly submit native Stop Loss order for the open position at the specified price."""
    data = request.json or {}
    security_id = data.get("security_id")
    segment = data.get("exchange_segment", "NSE_FNO")
    sl = float(data.get("sl_price", 0))
    qty = int(data.get("quantity", 0))
    
    if not security_id or sl <= 0 or qty <= 0:
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400
        
    positions = _monitor.api.get_positions() if _monitor else []
    pos_qty = 0
    product_type = "MARGIN"
    target_sec_id = security_id
    target_segment = segment

    for p in positions:
        sec_match = str(p.get("securityId", "")) == str(security_id) or str(p.get("security_id", "")) == str(security_id)
        net_q = p.get("netQty", p.get("net_qty", 0))
        if sec_match and net_q != 0:
            pos_qty = net_q
            product_type = p.get("productType", p.get("product_type", "MARGIN"))
            target_sec_id = str(p.get("securityId", p.get("security_id", security_id)))
            target_segment = p.get("exchangeSegment", p.get("exchange_segment", segment))
            break

    if pos_qty == 0:
        for p in positions:
            net_q = p.get("netQty", p.get("net_qty", 0))
            if net_q != 0:
                pos_qty = net_q
                product_type = p.get("productType", p.get("product_type", "MARGIN"))
                target_sec_id = str(p.get("securityId", p.get("security_id", security_id)))
                target_segment = p.get("exchangeSegment", p.get("exchange_segment", segment))
                break
            
    if pos_qty == 0:
        logger.warning("1-Click SL rejected for %s: No open position found", security_id)
        return jsonify({"status": "error", "message": f"No open position found for security_id {security_id} to attach a Stop Loss"}), 400
    
    tx_type = "SELL" if pos_qty > 0 else "BUY"
    qty = abs(pos_qty)
    
    # Cancel any existing pending SL order for this contract first before replacing
    _cancel_existing_sl_orders(target_sec_id)
        
    # Calculate limit price using user's configured slippage buffer (STOP_LOSS_LIMIT)
    slippage = float(data.get("slippage", 0.50))
    buffer = max(0.05, slippage)
    limit_price = sl + buffer if tx_type == "BUY" else max(0.05, sl - buffer)
        
    try:
        res_order = _monitor.api.place_order(
            security_id=target_sec_id,
            exchange_segment=target_segment,
            transaction_type=tx_type,
            quantity=qty,
            order_type="STOP_LOSS_LIMIT",
            product_type=product_type,
            price=limit_price,
            trigger_price=sl
        )
        return jsonify({"status": "success", "data": res_order})
    except Exception as e:
        logger.error("Failed to place 1-Click SL: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/order/submit_tp_1click", methods=["POST"])
def submit_tp_1click():
    """Instantly submit native Take Profit (Limit) target order for the open position at the specified price."""
    data = request.json or {}
    security_id = data.get("security_id")
    segment = data.get("exchange_segment", "NSE_FNO")
    tp = float(data.get("tp_price", 0))
    qty = int(data.get("quantity", 0))
    
    if not security_id or tp <= 0 or qty <= 0:
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400
        
    positions = _monitor.api.get_positions() if _monitor else []
    pos_qty = 0
    product_type = "MARGIN"
    target_sec_id = security_id
    target_segment = segment

    for p in positions:
        sec_match = str(p.get("securityId", "")) == str(security_id) or str(p.get("security_id", "")) == str(security_id)
        net_q = p.get("netQty", p.get("net_qty", 0))
        if sec_match and net_q != 0:
            pos_qty = net_q
            product_type = p.get("productType", p.get("product_type", "MARGIN"))
            target_sec_id = str(p.get("securityId", p.get("security_id", security_id)))
            target_segment = p.get("exchangeSegment", p.get("exchange_segment", segment))
            break

    if pos_qty == 0:
        for p in positions:
            net_q = p.get("netQty", p.get("net_qty", 0))
            if net_q != 0:
                pos_qty = net_q
                product_type = p.get("productType", p.get("product_type", "MARGIN"))
                target_sec_id = str(p.get("securityId", p.get("security_id", security_id)))
                target_segment = p.get("exchangeSegment", p.get("exchange_segment", segment))
                break
            
    if pos_qty == 0:
        logger.warning("1-Click TP rejected for %s: No open position found", security_id)
        return jsonify({"status": "error", "message": f"No open position found for security_id {security_id} to attach Take Profit"}), 400
    
    tx_type = "SELL" if pos_qty > 0 else "BUY"
    qty = abs(pos_qty)
        
    try:
        res_order = _monitor.api.place_order(
            security_id=target_sec_id,
            exchange_segment=target_segment,
            transaction_type=tx_type,
            quantity=qty,
            order_type="LIMIT",
            product_type=product_type,
            price=tp
        )
        return jsonify({"status": "success", "data": res_order})
    except Exception as e:
        logger.error("Failed to place 1-Click TP: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/order/move_sl_to_be", methods=["POST"])
def move_sl_to_be():
    """Locate open position strictly for the active chart contract, calculate Break-Even (entry +/- offset), cancel any existing pending SL, and submit native SL order."""
    data = request.json or {}
    security_id = data.get("security_id")
    offset = float(data.get("be_offset", 0.50))
    
    if not security_id:
        return jsonify({"status": "error", "message": "No contract security_id provided. Select/resolve a chart first."}), 400

    positions = _monitor.api.get_positions() if _monitor else []
    target_pos = None
    
    for p in positions:
        sec_match = str(p.get("securityId", "")) == str(security_id) or str(p.get("security_id", "")) == str(security_id)
        net_q = p.get("netQty", p.get("net_qty", 0))
        if sec_match and net_q != 0:
            target_pos = p
            break

    if not target_pos:
        logger.warning("move_sl_to_be rejected: No open position for security_id=%s", security_id)
        return jsonify({"status": "error", "message": f"No active open position found for contract {security_id}"}), 400

    net_qty = target_pos.get("netQty", target_pos.get("net_qty", 0))
    sec_id = str(target_pos.get("securityId", target_pos.get("security_id", "")))
    segment = target_pos.get("exchangeSegment", target_pos.get("exchange_segment", "BSE_FNO"))
    product_type = target_pos.get("productType", target_pos.get("product_type", "MARGIN"))
    
    # Extract entry fill price
    if net_qty < 0:
        entry_price = float(target_pos.get("sellAvg", 0) or target_pos.get("costPrice", 0) or target_pos.get("buyAvg", 0))
        be_sl = entry_price - offset
        tx_type = "BUY"
    else:
        entry_price = float(target_pos.get("buyAvg", 0) or target_pos.get("costPrice", 0) or target_pos.get("sellAvg", 0))
        be_sl = entry_price + offset
        tx_type = "SELL"
        
    if entry_price <= 0:
        return jsonify({"status": "error", "message": "Could not determine position entry price"}), 400
        
    be_sl = round(max(0.05, be_sl) * 20) / 20
    qty = abs(net_qty)
    
    # Cancel any existing pending SL order for this contract first before replacing
    _cancel_existing_sl_orders(sec_id)

    slippage = float(data.get("slippage", 0.50))
    buffer = max(0.05, slippage)
    limit_price = be_sl + buffer if tx_type == "BUY" else max(0.05, be_sl - buffer)
    
    logger.info("move_sl_to_be: sec_id=%s netQty=%d entry=%.2f be_sl=%.2f tx=%s", sec_id, net_qty, entry_price, be_sl, tx_type)
    
    try:
        res_order = _monitor.api.place_order(
            security_id=sec_id,
            exchange_segment=segment,
            transaction_type=tx_type,
            quantity=qty,
            order_type="STOP_LOSS_LIMIT",
            product_type=product_type,
            price=limit_price,
            trigger_price=be_sl
        )
        return jsonify({"status": "success", "be_price": be_sl, "entry_price": entry_price, "data": res_order})
    except Exception as e:
        logger.error("Failed to place Break-Even SL: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500 = p.get("netQty", p.get("net_qty", 0))
        if sec_match and net_q != 0:
            target_pos = p
            break

    if not target_pos:
        logger.warning("move_sl_to_be rejected: No open position for security_id=%s", security_id)
        return jsonify({"status": "error", "message": f"No active open position found for contract {security_id}"}), 400

    net_qty = target_pos.get("netQty", target_pos.get("net_qty", 0))
    sec_id = str(target_pos.get("securityId", target_pos.get("security_id", "")))
    segment = target_pos.get("exchangeSegment", target_pos.get("exchange_segment", "BSE_FNO"))
    product_type = target_pos.get("productType", target_pos.get("product_type", "MARGIN"))
    
    # Extract entry fill price
    if net_qty < 0:
        entry_price = float(target_pos.get("sellAvg", 0) or target_pos.get("costPrice", 0) or target_pos.get("buyAvg", 0))
        be_sl = entry_price - offset
        tx_type = "BUY"
    else:
        entry_price = float(target_pos.get("buyAvg", 0) or target_pos.get("costPrice", 0) or target_pos.get("sellAvg", 0))
        be_sl = entry_price + offset
        tx_type = "SELL"
        
    if entry_price <= 0:
        return jsonify({"status": "error", "message": "Could not determine position entry price"}), 400
        
    be_sl = round(max(0.05, be_sl) * 20) / 20
    qty = abs(net_qty)
    
    slippage = float(data.get("slippage", 0.50))
    buffer = max(0.05, slippage)
    limit_price = be_sl + buffer if tx_type == "BUY" else max(0.05, be_sl - buffer)
    
    logger.info("move_sl_to_be: sec_id=%s netQty=%d entry=%.2f be_sl=%.2f tx=%s", sec_id, net_qty, entry_price, be_sl, tx_type)
    
    try:
        res_order = _monitor.api.place_order(
            security_id=sec_id,
            exchange_segment=segment,
            transaction_type=tx_type,
            quantity=qty,
            order_type="STOP_LOSS_LIMIT",
            product_type=product_type,
            price=limit_price,
            trigger_price=be_sl
        )
        return jsonify({"status": "success", "be_price": be_sl, "entry_price": entry_price, "data": res_order})
    except Exception as e:
        logger.error("Failed to place Break-Even SL: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/order/cancel_all_pending", methods=["POST"])
def api_cancel_all_pending():
    """Cancel all pending/resting orders on the exchange orderbook."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    try:
        cancel_results = _monitor.api.cancel_all_pending_orders()
        return jsonify({
            "status": "success",
            "cancelled_orders": len(cancel_results)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/chart-trading")
def chart_trading():
    def fmt_inr(n):
        """Format number as INR for HTML template."""
        return "\u20B9{:,.0f}".format(abs(n))
    from flask import make_response
    resp = make_response(render_template_string(
        DASHBOARD_HTML,
        interval=Config.MONITOR_INTERVAL,
        default_risk=int(Config.DEFAULT_RISK_AMOUNT),
        quick_sl_offsets=json.dumps(Config.QUICK_SL_OFFSETS),
        quick_tp_offsets=json.dumps(Config.QUICK_TP_OFFSETS),
        loss_limit_fmt=fmt_inr(Config.DAILY_MAX_LOSS),
        profit_lock_threshold_fmt=fmt_inr(Config.PROFIT_LOCK_THRESHOLD),
        profit_lock_distance_fmt=fmt_inr(Config.PROFIT_LOCK_THRESHOLD),
    ))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    def fmt_inr(n):
        """Format number as INR for HTML template."""
        return "\u20B9{:,.0f}".format(abs(n))
    from flask import make_response
    resp = make_response(render_template_string(
        DASHBOARD_HTML,
        interval=Config.MONITOR_INTERVAL,
        default_risk=int(Config.DEFAULT_RISK_AMOUNT),
        quick_sl_offsets=json.dumps(Config.QUICK_SL_OFFSETS),
        quick_tp_offsets=json.dumps(Config.QUICK_TP_OFFSETS),
        loss_limit_fmt=fmt_inr(Config.DAILY_MAX_LOSS),
        profit_lock_threshold_fmt=fmt_inr(Config.PROFIT_LOCK_THRESHOLD),
        profit_lock_distance_fmt=fmt_inr(Config.PROFIT_LOCK_THRESHOLD),
    ))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/admin/unlock", methods=["POST"])
def api_admin_unlock():
    """Force-reset the daily state for a new trading day. Use when state is stale from prior day."""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        _monitor.state._reset_state()
        _monitor._lockout_executed = False
        logger.warning("MANUAL UNLOCK: daily state force-reset via admin endpoint")
        # Also deactivate Dhan kill switch if it was activated during lockout
        ks_msg = ""
        try:
            ks_result = _monitor.api.deactivate_kill_switch()
            if isinstance(ks_result, dict) and ks_result.get("status") == "success":
                ks_msg = " Kill switch deactivated on Dhan."
            else:
                ks_msg = f" Kill switch response: {ks_result}"
        except Exception as ke:
            ks_msg = f" Kill switch deactivation failed: {ke}"
        logger.warning("MANUAL UNLOCK: kill switch deactivation result:%s", ks_msg)
        return jsonify({"status": "ok", "message": f"State reset. Platform unlocked for today.{ks_msg}"})
    except Exception as e:
        logger.error("Admin unlock failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/admin/kill_switch", methods=["POST"])
def api_admin_kill_switch():
    """Activate or deactivate Dhan kill switch. Body: {"action": "activate"|"deactivate"}"""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    data = request.get_json(silent=True) or {}
    action = data.get("action", "deactivate").lower()
    try:
        if action == "activate":
            result = _monitor.api.activate_kill_switch()
        else:
            result = _monitor.api.deactivate_kill_switch()
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/admin/reset_hwm", methods=["POST"])
def api_admin_reset_hwm():
    """Reset only the HWM in state without clearing anything else. Use when HWM is stale/inflated."""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        old_hwm = _monitor.state.high_water_mark
        _monitor.state._state["high_water_mark"] = 0.0
        _monitor.state._state["trailing_drawdown_active"] = False
        _monitor.state._state["profit_lock_active"] = False
        _monitor.state._state["profit_lock_floor"] = 0.0
        _monitor.state._state["profit_lock_level"] = 0.0
        _monitor.state._save()
        logger.warning("MANUAL HWM RESET: HWM reset from ₹%.0f to 0 via admin endpoint", old_hwm)
        return jsonify({"status": "ok", "message": f"HWM reset from ₹{old_hwm:,.0f} to 0. Everything else preserved."})
    except Exception as e:
        logger.error("HWM reset failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/admin/extend_trade_limit", methods=["POST"])
def api_admin_extend_trade_limit():
    """Allow one-time extension of maximum daily trades limit by 10."""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        success, msg = _monitor.risk.extend_trade_limit()
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"status": "error", "message": msg}), 400
    except Exception as e:
        logger.error("Extend trade limit failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/admin/reset_lockout", methods=["POST"])
def api_admin_reset_lockout():
    """Manually clear today's lockout state via admin endpoint."""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        _monitor.state._state["is_locked_out"] = False
        _monitor.state._state["lockout_reason"] = ""
        _monitor.state._state["lockout_time"] = None
        # Also reset active floors and HWM to prevent instant re-lockout
        _monitor.state._state["high_water_mark"] = 0.0
        _monitor.state._state["drawdown_active"] = False
        _monitor.state._state["profit_lock_active"] = False
        _monitor.state._state["profit_lock_floor"] = 0.0
        _monitor.state._state["profit_lock_level"] = 0.0
        _monitor.state._save()
        _monitor._lockout_executed = False  # Reset monitor lockout flag
        logger.warning("MANUAL LOCKOUT RESET: Lockout and HWM floors cleared via admin endpoint")
        return jsonify({"status": "ok", "message": "Lockout and Profit Lock floors cleared successfully."})
    except Exception as e:
        logger.error("Lockout reset failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/admin/reload_config", methods=["POST"])
def api_admin_reload_config():
    """Reload config from .env dynamically in memory."""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        from dotenv import load_dotenv
        import os
        load_dotenv(override=True)
        Config.DAILY_MAX_LOSS = float(os.getenv("DAILY_MAX_LOSS", "7000"))
        Config.DAILY_PROFIT_TARGET = float(os.getenv("DAILY_PROFIT_TARGET", "20000"))
        Config.MAX_ORDER_QUANTITY = int(os.getenv("MAX_ORDER_QUANTITY", "1300"))
        Config.MAX_NIFTY_QUANTITY = int(os.getenv("MAX_NIFTY_QUANTITY", "1300"))
        Config.MAX_SENSEX_QUANTITY = int(os.getenv("MAX_SENSEX_QUANTITY", "400"))
        Config.PROFIT_LOCK_THRESHOLD = float(os.getenv("PROFIT_LOCK_THRESHOLD", "7000"))
        logger.warning("CONFIG RELOAD: Reloaded .env variables dynamically in memory! "
                       "DAILY_MAX_LOSS=%.0f, MAX_NIFTY_QUANTITY=%d",
                       Config.DAILY_MAX_LOSS, Config.MAX_NIFTY_QUANTITY)
        return jsonify({"status": "success", "message": "Configuration reloaded in memory!"})
    except Exception as e:
        logger.error("Config reload failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/status")
def api_status():
    if _monitor:
        try:
            return jsonify(_monitor.get_status())
        except Exception as e:
            logger.error("Status endpoint error: %s", e, exc_info=True)
            # Return minimal valid status so dashboard still shows config values
            from risk_engine import RiskEngine
            try:
                risk_status = _monitor.risk.get_risk_status()
                return jsonify({**risk_status, "monitor_running": _monitor._running,
                                "positions": [], "spreads": [], "sl_tp_orders": {},
                                "pending_spreads": [], "pnl_chart": [], "pending_orders": [],
                                "recent_orders": []})
            except Exception:
                pass
    # Fallback: return config-only status so dashboard can at least show limits
    return jsonify({
        "lockout": {"active": False, "reason": "", "time": None},
        "cooldown": {"active": False, "remaining_seconds": 0, "reason": ""},
        "pnl": {"realized": 0, "unrealized": 0, "total": 0, "peak": 0},
        "limits": {
            "daily_max_loss": Config.DAILY_MAX_LOSS,
            "loss_remaining": Config.DAILY_MAX_LOSS,
            "loss_used_pct": 0,
            "profit_target": Config.DAILY_PROFIT_TARGET,
            "profit_remaining": Config.DAILY_PROFIT_TARGET,
        },
        "profit_lock": {"active": False, "threshold": Config.PROFIT_LOCK_THRESHOLD,
                        "distance": Config.PROFIT_LOCK_THRESHOLD},
        "trailing_drawdown": {"enabled": Config.TRAILING_DRAWDOWN_ENABLED,
                              "high_water_mark": 0, "current_drawdown": 0,
                              "drawdown_limit": 0, "buffer": 0},
        "trades": {"total": 0, "winners": 0, "losers": 0,
                   "consecutive_losses": 0, "win_rate": 0, "history": []},
        "kill_switch": False,
        "can_trade": False,
        "monitor_running": False,
        "positions": [], "spreads": [], "sl_tp_orders": {},
        "pending_spreads": [], "pnl_chart": [], "pending_orders": [],
        "recent_orders": [],
    })


@app.route("/api/health")
def api_health():
    """Diagnostic endpoint for debugging issues."""
    health = {
        "monitor_set": _monitor is not None,
        "monitor_running": _monitor._running if _monitor else False,
        "instrument_cache_loaded": _instrument_cache is not None and _instrument_cache.count > 0,
        "instrument_count": _instrument_cache.count if _instrument_cache else 0,
        "config": {
            "daily_max_loss": Config.DAILY_MAX_LOSS,
            "daily_profit_target": Config.DAILY_PROFIT_TARGET,
            "profit_lock_threshold": Config.PROFIT_LOCK_THRESHOLD,
            "client_id": Config.DHAN_CLIENT_ID[:4] + "..." if Config.DHAN_CLIENT_ID else "NOT SET",
            "token_set": bool(Config.DHAN_ACCESS_TOKEN),
        },
    }
    if _monitor:
        try:
            health["positions_count"] = len(_monitor._last_positions or [])
            health["orders_count"] = len(_monitor._last_orders or [])
        except Exception:
            pass
    return jsonify(health)


# ── Instrument Search ──────────────────────────────────────────────

@app.route("/api/instruments/search")
def api_search_instruments():
    """Search instruments by query string."""
    if not _instrument_cache:
        return jsonify([])
    q = request.args.get("q", "")
    limit = int(request.args.get("limit", 20))
    results = _instrument_cache.search(q, limit)
    return jsonify(results)


@app.route("/api/instruments/reload", methods=["POST"])
def api_reload_instruments():
    """Force reload instrument data."""
    if not _instrument_cache:
        return jsonify({"error": "Instrument cache not initialized"}), 500
    count = _instrument_cache.reload()
    # Reset spot and ATM caches so stale values don't persist after reload
    global _bse_last_spot, _bse_futures_sids, _oc_last_feed_start
    _bse_last_spot = 0.0
    _bse_futures_sids.clear()
    _oc_atm_cache.clear()
    _oc_ltp_subscribed.clear()
    _oc_ltp_instruments.clear()
    _oc_last_feed_start = 0.0
    return jsonify({"status": "ok", "instruments_loaded": count})


# ── Option Chain ───────────────────────────────────────────────────

@app.route("/api/option_chain/expiries")
def api_option_chain_expiries():
    """Get expiry dates for an underlying from instrument cache."""
    if not _instrument_cache:
        return jsonify({"error": "Instrument cache not loaded"}), 500
    underlying = request.args.get("underlying", "NIFTY").upper()
    bse_underlyings = {"SENSEX", "BANKEX"}
    expected_exchange = "BSE" if underlying in bse_underlyings else "NSE"
    try:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        expiries = set()
        for inst in _instrument_cache._instruments:
            if inst.instrument_type != "OPTIDX":
                continue
            if not inst.trading_symbol.upper().startswith(underlying + "-"):
                continue
            if getattr(inst, "exchange", "NSE") != expected_exchange:
                continue
            if inst.expiry_date:
                exp_date = inst.expiry_date[:10]
                if exp_date >= today:
                    expiries.add(exp_date)
        sorted_expiries = sorted(expiries)[:2]
        return jsonify({"expiries": sorted_expiries})
    except Exception as e:
        logger.error("Failed to get expiries: %s", e)
        return jsonify({"error": str(e)}), 500


_oc_data_cache: dict = {}  # (underlying, expiry) -> (timestamp, response_dict)
_OC_CACHE_TTL = 1.0  # seconds — match frontend refresh rate
_bse_last_spot: float = 0.0  # last valid SENSEX spot (seeded at startup, updated by DepthWebSocket ticks)
_nse_last_spot: dict = {}   # underlying -> last valid NSE spot (survives API failures)
_oc_atm_cache: dict = {}    # (underlying, expiry) -> (atm_strike, atm_idx) with hysteresis


def _start_bse_spot_updater():
    """Background thread: polls Yahoo Finance every 5s for live SENSEX spot price.
    Completely independent of Dhan API and the DepthWebSocket."""
    import time as _time
    import requests as _requests

    _URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EBSESN?interval=1m&range=1d"
    _HEADERS = {"User-Agent": "Mozilla/5.0"}

    def _run():
        global _bse_last_spot
        consecutive_errors = 0
        while True:
            try:
                resp = _requests.get(_URL, headers=_HEADERS, timeout=5)
                if resp.status_code == 200:
                    price = (resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
                    if price and float(price) > 0:
                        _bse_last_spot = float(price)
                        consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors <= 3:
                    logger.warning("BSE spot fetch error: %s", e)
            _time.sleep(5)

    t = threading.Thread(target=_run, daemon=True, name="BseSpotUpdater")
    t.start()

# Exchange segment mapping for MarketFeed subscription (NSE_FNO default for options)
_OC_SEG_MAP = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2, "NSE_CURR": 3, "BSE_EQ": 4, "MCX": 5, "BSE_CURR": 7, "BSE_FNO": 8}

@app.route("/api/option_chain/subscribe_ltp", methods=["POST"])
def api_oc_subscribe_ltp():
    """Subscribe ATM±10 option chain strikes to MarketFeed for real-time LTP pushes."""
    global _oc_ltp_subscribed
    if _monitor is None:
        return jsonify({"status": "no_monitor"})
    data = request.get_json(force=True) or {}
    instruments_req = data.get("instruments", [])  # [{sid, type}]
    if not instruments_req:
        return jsonify({"status": "empty"})

    global _oc_ltp_instruments
    # Use exchange segment from each instrument (client passes 'BSE_FNO' for SENSEX)
    for i in instruments_req:
        if not i.get("sid"):
            continue
        sid = str(i["sid"])
        seg_key = i.get("exchange_segment", "NSE_FNO")
        seg_int = _OC_SEG_MAP.get(seg_key, 1)
        _oc_ltp_instruments[sid] = (seg_int, sid, 15)

    new_sids = {str(i["sid"]) for i in instruments_req if i.get("sid")}

    # Restart feed when: new SIDs appear, underlying changed (force=True),
    # or feed hasn't been started in >60s (page refresh / feed drop).
    # Never shrink the subscribed set — warm cache persists across ATM shifts.
    global _oc_last_feed_start
    force = data.get("force", False)
    feed_stale = (time.time() - _oc_last_feed_start) > 60
    truly_new = new_sids - _oc_ltp_subscribed
    if not truly_new and not force and not feed_stale:
        return jsonify({"status": "unchanged"})

    _oc_last_feed_start = time.time()
    _oc_ltp_subscribed = _oc_ltp_subscribed | new_sids
    instruments = list(_oc_ltp_instruments.values())

    # Route OC LTP through the existing DepthWebSocket (same connection as DOM).
    # This avoids Dhan's concurrent-connection limit (error 805).
    # Position instruments for SL/TP monitoring remain on their own feed (monitor.py).
    ltp_instruments = [(seg_key, str(i["sid"])) for i in instruments_req if i.get("sid")
                       for seg_key in [i.get("exchange_segment", "NSE_FNO")]]
    if _depth_ws is not None:
        _depth_ws.set_ltp_instruments(ltp_instruments, _monitor._on_market_tick)
    return jsonify({"status": "ok", "count": len(ltp_instruments)})


@app.route("/api/option_chain/data")
def api_option_chain_data():
    """Build option chain from instrument cache, optionally fetch LTPs."""
    if not _instrument_cache:
        return jsonify({"error": "Instrument cache not loaded"}), 500
    underlying = request.args.get("underlying", "NIFTY").upper()
    expiry = request.args.get("expiry", "")
    if not expiry:
        return jsonify({"error": "expiry parameter required"}), 400

    # Return cached response if fresh enough — prevents API rate-limit hits
    cache_key = (underlying, expiry)
    cached = _oc_data_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _OC_CACHE_TTL:
        return jsonify(cached[1])
    bse_underlyings = {"SENSEX", "BANKEX"}
    expected_exchange = "BSE" if underlying in bse_underlyings else "NSE"
    default_lot = 20 if underlying in bse_underlyings else 75
    try:
        # Build chain from instrument cache
        strikes = {}
        lot_size = default_lot
        for inst in _instrument_cache._instruments:
            if inst.instrument_type != "OPTIDX":
                continue
            if not inst.trading_symbol.upper().startswith(underlying + "-"):
                continue
            if not inst.expiry_date or inst.expiry_date[:10] != expiry:
                continue
            if getattr(inst, "exchange", "NSE") != expected_exchange:
                continue
            lot_size = inst.lot_size
            strike = inst.strike_price
            if strike not in strikes:
                strikes[strike] = {"strike": strike, "ce_security_id": "", "pe_security_id": "",
                                   "ce_ltp": 0, "pe_ltp": 0}
            if inst.option_type == "CE":
                strikes[strike]["ce_security_id"] = inst.security_id
            elif inst.option_type == "PE":
                strikes[strike]["pe_security_id"] = inst.security_id

        chain = sorted(strikes.values(), key=lambda x: x["strike"])
        if not chain:
            return jsonify({"spot": 0, "chain": [], "expiry": expiry, "lot_size": lot_size})

        # Fetch spot price and option LTPs
        spot = 0
        if _monitor:
            try:
                bse_underlyings = {"SENSEX", "BANKEX"}
                is_bse = underlying in bse_underlyings
                # NSE underlyings: use option_chain API (single call, fast)
                # BSE underlyings: Dhan's option_chain API doesn't support BSE_IDX,
                #                  so fetch spot + LTPs via market quote instead
                if not is_bse:
                    uid_map = {"NIFTY": (13, "IDX_I"), "BANKNIFTY": (25, "IDX_I")}
                    underlying_id, oc_exseg = uid_map.get(underlying, (13, "IDX_I"))
                    oc_result = _monitor.api.get_option_chain(underlying_id, expiry, oc_exseg)
                    if isinstance(oc_result, dict) and oc_result.get("status") == "success":
                        oc_data = oc_result.get("data", {})
                        if isinstance(oc_data, dict) and "data" in oc_data and isinstance(oc_data["data"], dict):
                            oc_data = oc_data["data"]
                        spot = oc_data.get("last_price", 0) or 0
                        if spot > 0:
                            _nse_last_spot[underlying] = spot
                        oc_strikes = oc_data.get("oc", {})
                        ltp_by_strike = {}
                        for strike_str, sides in oc_strikes.items():
                            try:
                                s = float(strike_str)
                            except (ValueError, TypeError):
                                continue
                            ce = sides.get("ce", {}) or {}
                            pe = sides.get("pe", {}) or {}
                            ltp_by_strike[s] = {
                                "ce_ltp": ce.get("last_price", 0) or 0,
                                "pe_ltp": pe.get("last_price", 0) or 0,
                            }
                        for row in chain:
                            strike_data = ltp_by_strike.get(row["strike"])
                            if strike_data:
                                row["ce_ltp"] = strike_data["ce_ltp"]
                                row["pe_ltp"] = strike_data["pe_ltp"]
                else:
                    # BSE: spot is maintained by _start_bse_spot_updater() background thread
                    spot = _bse_last_spot
                    # Two-pass approach:
                    #   Pass 1: sample every 10th strike (~20 IDs) to estimate spot
                    #   Pass 2: fetch ATM±12 (~50 IDs) using estimated spot

                    def _bse_fetch_ltps(rows):
                        """Fetch LTPs for chain rows in batches of 9 (Dhan BSE_FNO limit ~10)."""
                        ids = []
                        for r in rows:
                            if r["ce_security_id"]: ids.append(int(r["ce_security_id"]))
                            if r["pe_security_id"]: ids.append(int(r["pe_security_id"]))
                        if not ids:
                            return {}
                        result = {}
                        for i in range(0, len(ids), 9):
                            batch = ids[i:i + 9]
                            res = _monitor.api.get_ltp({"BSE_FNO": batch})
                            if not isinstance(res, dict) or res.get("status") != "success":
                                continue
                            outer = res.get("data", {})
                            if isinstance(outer, dict) and "data" in outer:
                                outer = outer["data"]
                            if isinstance(outer, dict):
                                for key, val in outer.items():
                                    if isinstance(val, dict):
                                        # Case 1: Direct key matching {"123": {"last_price": 100}}
                                        if "last_price" in val:
                                            result[str(key)] = float(val.get("last_price", 0) or 0)
                                        else:
                                            # Case 2: Segment-keyed nesting {"BSE_FNO": {"123": {"last_price": 100}}}
                                            for sub_key, sub_val in val.items():
                                                if isinstance(sub_val, dict) and "last_price" in sub_val:
                                                    result[str(sub_key)] = float(sub_val.get("last_price", 0) or 0)
                                                elif isinstance(sub_val, (int, float)):
                                                    result[str(sub_key)] = float(sub_val or 0)
                                    elif isinstance(val, (int, float)):
                                        result[str(key)] = float(val or 0)
                        return result

                    # Pass 1: sample every 10th strike to find approximate ATM
                    step = max(1, len(chain) // 20)
                    sample_rows = chain[::step]
                    sample_ltps = _bse_fetch_ltps(sample_rows)
                    for row in sample_rows:
                        row["ce_ltp"] = sample_ltps.get(str(row["ce_security_id"]), 0)
                        row["pe_ltp"] = sample_ltps.get(str(row["pe_security_id"]), 0)

                    # Derive approximate spot from sample (strike where |CE-PE| is smallest)
                    best_row, best_diff = None, float("inf")
                    for row in sample_rows:
                        if row["ce_ltp"] > 0 and row["pe_ltp"] > 0:
                            d = abs(row["ce_ltp"] - row["pe_ltp"])
                            if d < best_diff:
                                best_diff, best_row = d, row
                    if best_row:
                        spot = best_row["strike"] + best_row["ce_ltp"] - best_row["pe_ltp"]
                        # Pass 2: find ATM index in full chain and fetch ±8 strikes (34 IDs max)
                        atm_approx = min(range(len(chain)), key=lambda i: abs(chain[i]["strike"] - spot))
                        atm_slice = chain[max(0, atm_approx - 8): atm_approx + 9]
                        atm_ltps = _bse_fetch_ltps(atm_slice)
                        if atm_ltps:  # only update if fetch succeeded; don't zero out pass-1 prices
                            for row in atm_slice:
                                row["ce_ltp"] = atm_ltps.get(str(row["ce_security_id"]), row["ce_ltp"])
                                row["pe_ltp"] = atm_ltps.get(str(row["pe_security_id"]), row["pe_ltp"])
                            # Refine spot from pass-2 data
                            best_row2, best_diff2 = None, float("inf")
                            for row in atm_slice:
                                if row["ce_ltp"] > 0 and row["pe_ltp"] > 0:
                                    d = abs(row["ce_ltp"] - row["pe_ltp"])
                                    if d < best_diff2:
                                        best_diff2, best_row2 = d, row
                            if best_row2:
                                spot = best_row2["strike"] + best_row2["ce_ltp"] - best_row2["pe_ltp"]

                    # Put-call parity gives a local estimate for ATM centering only;
                    # don't overwrite _bse_last_spot (futures-based, more reliable)
            except Exception as e:
                logger.error("Option chain price fetch failed: %s", e, exc_info=True)

        # Use cached NSE spot if current poll failed to get one
        if spot == 0 and not (underlying in {"SENSEX", "BANKEX"}):
            spot = _nse_last_spot.get(underlying, 0)

        # Find ATM index with hysteresis — only shift if spot moves >40% of strike interval
        # away from current ATM strike, preventing oscillation when spot sits near midpoint
        atm_idx = len(chain) // 2
        if spot > 0 and chain:
            # Estimate strike interval from chain
            if len(chain) > 1:
                strike_interval = chain[1]["strike"] - chain[0]["strike"]
            else:
                strike_interval = 50
            hysteresis = strike_interval * 0.6

            prev_atm_strike, prev_atm_idx = _oc_atm_cache.get(cache_key, (None, None))
            if prev_atm_strike is not None and abs(spot - prev_atm_strike) < hysteresis:
                # Spot hasn't moved far enough from current ATM — keep cached ATM
                # but re-validate the index (chain may have been rebuilt)
                best_i, best_d = prev_atm_idx, float("inf")
                for i, row in enumerate(chain):
                    d = abs(row["strike"] - prev_atm_strike)
                    if d < best_d:
                        best_d, best_i = d, i
                atm_idx = best_i
            else:
                # Spot has moved significantly — find new ATM
                min_dist = float("inf")
                for i, row in enumerate(chain):
                    d = abs(row["strike"] - spot)
                    if d < min_dist:
                        min_dist = d
                        atm_idx = i
                _oc_atm_cache[cache_key] = (chain[atm_idx]["strike"], atm_idx)

        # Trim to ATM +/- 6 strikes (13 total)
        start = max(0, atm_idx - 6)
        end = min(len(chain), atm_idx + 7)
        chain = chain[start:end]

        # Log a summary for debugging auto-refresh
        result_data = {"spot": spot, "chain": chain, "expiry": expiry, "lot_size": lot_size}
        _oc_data_cache[cache_key] = (time.time(), result_data)
        return jsonify(result_data)
    except Exception as e:
        logger.error("Option chain error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── LTP ────────────────────────────────────────────────────────────

@app.route("/api/ltp/<security_id>")
def api_get_ltp(security_id):
    """Get last traded price for an instrument."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    exchange_segment = request.args.get("exchange_segment", "NSE_FNO")
    try:
        data = _monitor.api.get_ltp({exchange_segment: [int(security_id)]})
        # Extract LTP from Dhan response
        ltp = None
        if isinstance(data, dict) and "data" in data:
            inner = data["data"]
            if isinstance(inner, dict):
                for key, val in inner.items():
                    if isinstance(val, dict) and "last_price" in val:
                        ltp = val["last_price"]
                        break
                    elif isinstance(val, (int, float)):
                        ltp = val
                        break
        return jsonify({"ltp": ltp, "raw": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market_data")
def api_market_data():
    """Get NIFTY and SENSEX spot index 1-minute candle history and live tick."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
        
    res = {}
    with _monitor._lock:
        for name, security_id in [("NIFTY", "13"), ("SENSEX", "51")]:
            key = (security_id, 60)
            closed = list(_monitor._closed_candles_history.get(key, []))
            live = _monitor._live_candles.get(key, None)
            ltp = _monitor._ltp_cache.get(security_id, None)
            
            res[name] = {
                "closed_candles": closed,
                "live_candle": live,
                "ltp": ltp
            }
            
    return jsonify(res)


@app.route("/api/order/candle_close_trigger", methods=["POST"], strict_slashes=False)
def api_candle_close_trigger():
    """Queue a single-leg order to trigger at candle close."""
    if not _monitor:
        return jsonify({"status": "error", "message": "Monitor not ready"}), 503
    data = request.json or {}

    security_id = data.get("security_id", "")
    if not security_id:
        return jsonify({"status": "error", "message": "No security_id"}), 400

    quantity = int(data.get("quantity", 0))
    if quantity <= 0:
        return jsonify({"status": "error", "message": "Quantity must be > 0"}), 400

    timeframe = int(data.get("timeframe", 60))
    if timeframe not in (5, 15, 60, 300):
        return jsonify({"status": "error", "message": "Unsupported timeframe (only 5s, 15s, 1m, 5m allowed)"}), 400

    buffer = float(data.get("buffer", 0.0))
    direction = data.get("direction", "SELL").upper()
    product_type = data.get("product_type", "MARGIN")
    exseg = data.get("exchange_segment", "NSE_FNO")
    stop_loss = float(data.get("stop_loss", 0.0))

    try:
        trigger_id = _monitor.queue_candle_close_trigger(
            security_id=security_id,
            direction=direction,
            quantity=quantity,
            buffer=buffer,
            timeframe=timeframe,
            product_type=product_type,
            exchange_segment=exseg,
            stop_loss=stop_loss,
        )
        return jsonify({
            "status": "success",
            "message": f"Candle-close trigger queued successfully for {security_id}",
            "trigger_id": trigger_id
        })
    except Exception as e:
        logger.error("Failed to queue candle close trigger: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Order Placement ────────────────────────────────────────────────

@app.route("/api/order/place", methods=["POST"], strict_slashes=False)
def api_place_order():
    """Place an order through the risk-checked interceptor."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json

    security_id = data.get("security_id", "")
    if not security_id:
        return jsonify({"status": "error", "message": "No security_id"}), 400

    order_type = data.get("order_type", "MARKET")
    price = float(data.get("price", 0))
    trigger_price = float(data.get("trigger_price", 0))
    quantity = int(data.get("quantity", 0))

    # Validate required fields based on order type
    if quantity <= 0:
        return jsonify({"status": "error", "message": "Quantity must be > 0"}), 400
    if order_type in ("STOP_LOSS", "STOP_LOSS_MARKET") and trigger_price <= 0:
        return jsonify({"status": "error", "message": "Trigger price required for stop-loss orders"}), 400
    if order_type in ("STOP_LOSS", "LIMIT") and price <= 0:
        return jsonify({"status": "error", "message": "Price required for LIMIT/STOP_LOSS orders"}), 400

    try:
        result = _monitor.interceptor.place_order(
            security_id=security_id,
            exchange_segment=data.get("exchange_segment", "NSE_FNO"),
            transaction_type=data.get("transaction_type", "BUY"),
            quantity=quantity,
            order_type=order_type,
            product_type=data.get("product_type", "MARGIN"),
            price=price,
            trigger_price=trigger_price,
            sl_price=float(data.get("sl_price", 0)),
        )

        # Check if the order was blocked by risk engine
        if result.get("status") == "BLOCKED":
            return jsonify(result)

        # Dhan SDK wraps all responses as:
        #   {"status": "success"/"failure", "data": {...}, "remarks": ""/{"error_code":..., "error_message":...}}
        dhan_status = result.get("status", "")
        dhan_data = result.get("data", {})
        if isinstance(dhan_data, str):
            dhan_data = {}
        remarks = result.get("remarks", {})

        # Case 1: Dhan API-level failure (bad request, auth error, etc.)
        if dhan_status == "failure":
            reject_reason = ""
            if isinstance(remarks, dict):
                reject_reason = remarks.get("error_message", "") or remarks.get("message", "")
            elif isinstance(remarks, str) and remarks:
                reject_reason = remarks
            if not reject_reason and isinstance(dhan_data, dict):
                reject_reason = dhan_data.get("errorMessage", "") or dhan_data.get("message", "")
            logger.warning("Order FAILED at Dhan API: reason=%s | full=%s", reject_reason, result)
            return jsonify({
                "status": "REJECTED",
                "orderId": "",
                "reason": reject_reason or "Order rejected by broker API",
            })

        # Case 2: API accepted but order was rejected by exchange
        order_id = ""
        order_status = ""
        if isinstance(dhan_data, dict):
            order_id = str(dhan_data.get("orderId", ""))
            order_status = dhan_data.get("orderStatus", "")
        # Also check top-level (some SDK versions flatten the response)
        if not order_id:
            order_id = str(result.get("orderId", ""))
        if not order_status:
            order_status = result.get("orderStatus", "")

        if order_status == "REJECTED":
            reject_reason = ""
            if isinstance(dhan_data, dict):
                reject_reason = (dhan_data.get("omsErrorDescription", "")
                                 or dhan_data.get("rejectedReason", ""))
            logger.warning("Order REJECTED by exchange: %s | reason=%s", order_id, reject_reason)
            return jsonify({
                "status": "REJECTED",
                "orderId": order_id,
                "reason": reject_reason or "Order rejected by exchange",
            })

        # Case 3: Order accepted - but re-fetch to catch fast rejections
        # (some orders get accepted then rejected within milliseconds)
        if order_id:
            try:
                order_detail = _monitor.api.get_order_by_id(order_id)
                actual_status = ""
                if isinstance(order_detail, dict):
                    detail_data = order_detail.get("data", order_detail)
                    if isinstance(detail_data, dict):
                        actual_status = detail_data.get("orderStatus", "")
                    if not actual_status:
                        actual_status = order_detail.get("orderStatus", "")

                if actual_status == "REJECTED":
                    reject_reason = ""
                    if isinstance(detail_data, dict):
                        reject_reason = (detail_data.get("omsErrorDescription", "")
                                         or detail_data.get("rejectedReason", ""))
                    logger.warning("Order %s REJECTED after placement: %s", order_id, reject_reason)
                    return jsonify({
                        "status": "REJECTED",
                        "orderId": order_id,
                        "reason": reject_reason or "Order rejected by exchange/broker",
                    })
                # Update status for frontend display
                if actual_status:
                    order_status = actual_status
            except Exception as e:
                logger.debug("Could not fetch order detail for %s: %s", order_id, e)

        # Order is accepted/pending/transit - set SL/TP if specified
        if data.get("sl_price"):
            sl_price = float(data["sl_price"])
            if sl_price > 0:
                _monitor.trade_mgr.set_stop_loss(
                    security_id=security_id,
                    sl_price=sl_price,
                )
                logger.info("Auto-set SL at %.2f for %s", sl_price, security_id)
                
                # Queue native Stop Loss placement when this specific entry order fills
                if order_id:
                    tx_type = data.get("transaction_type", "BUY")
                    _monitor._pending_sl_orders[order_id] = {
                        "security_id": security_id,
                        "exchange_segment": segment,
                        "quantity": qty,
                        "product_type": product_type,
                        "direction": tx_type,
                        "stop_loss": sl_price
                    }
                    logger.info("Queued automated native Stop Loss at %.2f for order %s", sl_price, order_id)

        if data.get("tp_price"):
            tp_price = float(data["tp_price"])
            if tp_price > 0:
                _monitor.trade_mgr.set_take_profit(
                    security_id=security_id,
                    tp_price=tp_price,
                )
                logger.info("Auto-set TP at %.2f for %s", tp_price, security_id)

        return jsonify({
            "status": "SUCCESS",
            "orderId": order_id,
            "orderStatus": order_status,
        })
    except Exception as e:
        logger.error("Order placement error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/order/calculate_size", methods=["POST"])
def api_calculate_size():
    """Calculate position size from risk parameters."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500

    data = request.json
    risk_amount = float(data.get("risk_amount", 0))
    entry_price = float(data.get("entry_price", 0))
    sl_price = float(data.get("sl_price", 0))
    security_id = str(data.get("security_id", ""))

    if risk_amount <= 0:
        return jsonify({"error": "Risk amount must be positive"}), 400
    if entry_price <= 0:
        return jsonify({"error": "Entry price must be positive"}), 400

    risk_per_unit = abs(entry_price - sl_price)
    if risk_per_unit == 0:
        return jsonify({"error": "Entry and SL cannot be the same"}), 400

    # Get lot size from instrument cache
    lot_size = 1
    if _instrument_cache:
        inst = _instrument_cache.get_by_id(security_id)
        if inst:
            lot_size = inst.lot_size

    raw_qty = risk_amount / risk_per_unit
    num_lots = None

    if lot_size > 1:
        num_lots = int(raw_qty // lot_size)
        quantity = num_lots * lot_size
    else:
        quantity = int(raw_qty)

    # Enforce max order quantity
    quantity = min(quantity, Config.MAX_ORDER_QUANTITY)
    if lot_size > 1:
        num_lots = quantity // lot_size

    actual_risk = risk_per_unit * quantity

    # Margin calculation (best effort)
    margin_required = None
    try:
        exchange_segment = data.get("exchange_segment", "")
        if not exchange_segment and _instrument_cache:
            exchange_segment = _instrument_cache.get_exchange_segment(security_id)
        margin_resp = _monitor.api.get_margin_calculator(
            security_id=security_id,
            exchange_segment=exchange_segment or "NSE_FNO",
            transaction_type=data.get("transaction_type", "BUY"),
            quantity=quantity,
            product_type=data.get("product_type", "MARGIN"),
            price=entry_price,
        )
        if isinstance(margin_resp, dict) and "data" in margin_resp:
            margin_required = margin_resp["data"].get("totalMargin")
    except Exception:
        pass

    # Feasibility check
    feasible = True
    feasibility_reason = ""
    try:
        exchange_segment = data.get("exchange_segment", "")
        if not exchange_segment and _instrument_cache:
            exchange_segment = _instrument_cache.get_exchange_segment(security_id)
        decision = _monitor.interceptor.check_order_feasibility(
            security_id=security_id,
            exchange_segment=exchange_segment or "NSE_FNO",
            transaction_type=data.get("transaction_type", "BUY"),
            quantity=quantity,
            price=entry_price,
            sl_price=sl_price,
        )
        feasible = bool(decision)
        if not feasible:
            feasibility_reason = decision.reason
    except Exception:
        pass

    return jsonify({
        "quantity": quantity,
        "num_lots": num_lots,
        "lot_size": lot_size,
        "risk_per_unit": round(risk_per_unit, 2),
        "actual_risk": round(actual_risk, 2),
        "margin_required": margin_required,
        "feasible": feasible,
        "feasibility_reason": feasibility_reason,
    })


# ── Spread Orders ──────────────────────────────────────────────────

def _cancel_exchange_sl(security_id: str) -> str:
    """Cancel the stored exchange SL order. Returns cancelled order_id or ''."""
    sl_config = _monitor.trade_mgr._sl_tp_orders.get(str(security_id))
    if not sl_config:
        return ""
    order_id = sl_config.exchange_sl_order_id
    if not order_id:
        return ""
    try:
        _monitor.api.cancel_order(order_id)
        sl_config.exchange_sl_order_id = None
        logger.info("Cancelled exchange SL order %s for security %s", order_id, security_id)
        return order_id
    except Exception as e:
        logger.warning("Failed to cancel SL order %s: %s", order_id, e)
        return ""


@app.route("/api/order/cancel_sl/<security_id>", methods=["POST"])
def api_cancel_sl(security_id):
    """Cancel the exchange-level SL order for a position before full manual exit."""
    if not _monitor:
        return jsonify({"status": "error", "message": "Monitor not initialized"}), 500
    cancelled = _cancel_exchange_sl(security_id)
    return jsonify({"status": "ok", "cancelled": cancelled})


@app.route("/api/order/replace_sl/<security_id>", methods=["POST"])
def api_replace_sl(security_id):
    """After a partial exit, replace the exchange SL with the correct remaining quantity."""
    if not _monitor:
        return jsonify({"status": "error", "message": "Monitor not initialized"}), 500
    data = request.json or {}
    remaining_qty = int(data.get("remaining_qty", 0))
    if remaining_qty <= 0:
        return jsonify({"status": "error", "message": "invalid remaining_qty"}), 400

    sl_config = _monitor.trade_mgr._sl_tp_orders.get(str(security_id))
    if not sl_config or not sl_config.stop_loss_price:
        return jsonify({"status": "ok", "message": "no sl registered"})

    # Cancel old SL order
    _cancel_exchange_sl(security_id)

    # Place new SL order for remaining quantity
    # Need exchange_segment — look it up from position cache
    pos = _monitor.trade_mgr._position_cache.get(str(security_id), {})
    exchange_segment = pos.get("exchangeSegment", "NSE_FNO")
    product_type = pos.get("productType", "MARGIN")
    try:
        sl_result = _monitor.api.place_order(
            security_id=str(security_id),
            exchange_segment=exchange_segment,
            transaction_type="BUY",
            quantity=remaining_qty,
            order_type="STOP_LOSS_MARKET",
            product_type=product_type,
            price=0,
            trigger_price=sl_config.stop_loss_price,
        )
        new_order_id = ""
        if isinstance(sl_result, dict) and sl_result.get("status") != "failure":
            new_order_id = str(sl_result.get("orderId", sl_result.get("data", {}).get("orderId", "")))
            sl_config.exchange_sl_order_id = new_order_id or None
            logger.info("Replaced exchange SL for %s: qty=%d trigger=%.2f order=%s",
                        security_id, remaining_qty, sl_config.stop_loss_price, new_order_id)
        else:
            logger.warning("Replace SL failed for %s: %s", security_id, sl_result)
        return jsonify({"status": "ok", "new_order_id": new_order_id})
    except Exception as e:
        logger.warning("Replace SL exception for %s: %s", security_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/order/place_spread", methods=["POST"])
def api_place_spread():
    """Queue or immediately execute a spread order (hedge buy at market + sell at limit)."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json

    instant = bool(data.get("instant", False))

    # For trigger mode, sell_trigger_price is required; for instant it is accepted but unused
    sell_order_type = data.get("sell_order_type", "LIMIT")
    required = ["sell_security_id", "buy_security_id", "quantity"]
    # sell_price only required for LIMIT orders (MARKET sends 0 which is falsy)
    if sell_order_type == "LIMIT":
        required.append("sell_price")
    if not instant:
        required.append("sell_trigger_price")
    for field in required:
        if not data.get(field):
            return jsonify({"status": "error", "message": f"Missing {field}"}), 400

    try:
        if instant:
            spread_action = {
                "spread_id": f"INSTANT-{int(time.time())}",
                "sell_security_id": str(data["sell_security_id"]),
                "sell_exchange_segment": data.get("sell_exchange_segment", "NSE_FNO"),
                "sell_order_type": data.get("sell_order_type", "LIMIT"),
                "sell_price": float(data.get("sell_price") or 0),
                "sell_sl": float(data.get("sell_sl", 0)),
                "product_type": data.get("product_type", "MARGIN"),
                "buy_security_id": str(data["buy_security_id"]),
                "buy_exchange_segment": data.get("buy_exchange_segment", "NSE_FNO"),
                "quantity": int(data["quantity"]),
                "ltp": float(data.get("sell_price") or 0),
            }
            # Execute in background thread so HTTP response returns immediately
            t = threading.Thread(target=_monitor._execute_spread,
                                 args=(spread_action,), daemon=True)
            t.start()
            return jsonify({"status": "ok", "instant": True,
                            "spread_id": spread_action["spread_id"]})
        else:
            spread_id = _monitor.trade_mgr.add_pending_spread(data)
            return jsonify({"status": "ok", "spread_id": spread_id})
    except Exception as e:
        logger.error("Failed to create/execute spread order: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/order/pending_spreads")
def api_pending_spreads():
    """Get all pending/active spread orders."""
    if not _monitor:
        return jsonify([])
    return jsonify(_monitor.trade_mgr.get_pending_spreads_summary())


@app.route("/api/order/cancel_spread", methods=["POST"])
def api_cancel_spread():
    """Cancel a pending spread order."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json
    spread_id = data.get("spread_id", "")
    if not spread_id:
        return jsonify({"status": "error", "message": "Missing spread_id"}), 400
    ok = _monitor.trade_mgr.cancel_pending_spread(spread_id)
    if ok:
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Spread not found or not pending"}), 404


# ── Quick SL/TP ───────────────────────────────────────────────────

@app.route("/api/sl/quick", methods=["POST"])
def api_quick_sl():
    """Set SL or TP at a point offset from current LTP."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500

    data = request.json
    security_id = data.get("security_id", "")
    offset_points = float(data.get("offset_points", 0))
    mode = data.get("mode", "sl")
    direction = int(data.get("direction", 1))
    exchange_segment = data.get("exchange_segment", "NSE_FNO")

    # Get current LTP from positions (faster than API call)
    ltp = None
    for pos in (_monitor._last_positions or []):
        if str(pos.get("securityId", "")) == security_id:
            ltp = pos.get("lastTradedPrice", 0)
            break

    if ltp is None or ltp == 0:
        # Fallback to API (handles both Dhan and Kotak nested formats)
        try:
            ltp_data = _monitor.api.get_ltp({exchange_segment: [security_id]})
            if isinstance(ltp_data, dict) and "data" in ltp_data:
                inner = ltp_data["data"]
                if isinstance(inner, dict) and "data" in inner:
                    inner = inner["data"]
                if isinstance(inner, dict):
                    # Check for direct key match (e.g. flat under "data")
                    if security_id in inner:
                        val = inner[security_id]
                        ltp = val.get("last_price", 0) if isinstance(val, dict) else val
                    else:
                        # Scan nested segment dicts
                        for seg_val in inner.values():
                            if isinstance(seg_val, dict) and security_id in seg_val:
                                val = seg_val[security_id]
                                ltp = val.get("last_price", 0) if isinstance(val, dict) else val
                                break
        except Exception:
            pass

    if not ltp:
        return jsonify({"error": "Could not get LTP"}), 400

    if mode == "sl":
        if direction == 1:
            price = ltp - abs(offset_points)
        else:
            price = ltp + abs(offset_points)
        _monitor.trade_mgr.set_stop_loss(security_id=security_id, sl_price=price)
    else:
        if direction == 1:
            price = ltp + abs(offset_points)
        else:
            price = ltp - abs(offset_points)
        _monitor.trade_mgr.set_take_profit(security_id=security_id, tp_price=price)

    return jsonify({"status": "ok", "price": round(price, 2), "ltp": round(ltp, 2)})


# ── Existing Endpoints ─────────────────────────────────────────────

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


# ── Snapshot Chart Data ────────────────────────────────────────────

@app.route("/api/chart/<security_id>")
def api_chart(security_id):
    """Return intraday 1-minute OHLCV candles for today + previous trading day."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    exchange_segment = request.args.get("exchange_segment", "NSE_FNO")
    instrument_type  = request.args.get("instrument_type", "OPTIDX")
    try:
        from datetime import datetime as _dt, date as _date, timedelta as _td

        def _prev_trading_day(d):
            """Return the most recent weekday before d (skips weekends, not holidays)."""
            prev = d - _td(days=1)
            while prev.weekday() >= 5:  # 5=Sat, 6=Sun
                prev -= _td(days=1)
            return prev

        def _parse_candles(raw):
            data = raw if isinstance(raw, dict) else {}
            if "data" in data and isinstance(data["data"], dict):
                data = data["data"]
            timestamps = data.get("timestamp", data.get("start_Time", []))
            opens  = data.get("open",  [])
            highs  = data.get("high",  [])
            lows   = data.get("low",   [])
            closes = data.get("close", [])
            result = []
            for i, ts in enumerate(timestamps):
                try:
                    from datetime import timezone, timedelta
                    ist_tz = timezone(timedelta(hours=5, minutes=30))
                    unix_ts = int(_dt.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ist_tz).timestamp())
                except (ValueError, TypeError):
                    try:
                        unix_ts = int(ts)
                    except (ValueError, TypeError):
                        continue
                o  = opens[i]  if i < len(opens)  else 0
                h  = highs[i]  if i < len(highs)  else 0
                lo = lows[i]   if i < len(lows)   else 0
                c  = closes[i] if i < len(closes) else 0
                if o > 0 and h > 0 and lo > 0 and c > 0:
                    result.append({"time": unix_ts, "open": o, "high": h, "low": lo, "close": c})
            return result

        today     = _date.today()
        prev_day  = _prev_trading_day(today)
        today_str = today.strftime("%Y-%m-%d")
        prev_str  = prev_day.strftime("%Y-%m-%d")

        # Fetch both days (prev day first so candles are in time order)
        prev_raw  = _monitor.api.get_chart_data(security_id, exchange_segment, instrument_type,
                                                 from_date=prev_str, to_date=prev_str)
        today_raw = _monitor.api.get_chart_data(security_id, exchange_segment, instrument_type,
                                                 from_date=today_str, to_date=today_str)

        prev_candles  = _parse_candles(prev_raw)
        today_candles = _parse_candles(today_raw)
        candles = prev_candles + today_candles

        logger.info("Chart %s: %d prev-day + %d today candles", security_id,
                    len(prev_candles), len(today_candles))
        return jsonify({"candles": candles})
    except Exception as e:
        logger.error("Failed to get chart data for %s: %s", security_id, e)
        return jsonify({"error": str(e), "candles": []}), 500


# ── Trade Journal Endpoints ────────────────────────────────────────

@app.route("/api/journal/trades")
def api_journal_trades():
    """Get trades for a specific day (defaults to today)."""
    if not _monitor:
        return jsonify([])
    day = request.args.get("date", str(date.today()))
    limit = int(request.args.get("limit", 200))
    return jsonify(_monitor.state.journal.get_trades(day=day, limit=limit))


ANALYSER_URL = "http://localhost:5556"


def _analyser_import():
    """Trigger trade-analyser to re-import from Dhan. Safe to call any time."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{ANALYSER_URL}/api/import",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        logger.warning("analyser import trigger failed: %s", e)


def _analyser_trades(days: int = 30) -> list:
    """Fetch trades from trade-analyser, using /api/dates to avoid iterating empty days.
    Auto-triggers a reimport if today's data is missing."""
    import urllib.request, json as _json
    from datetime import timedelta, date as _date
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(ist).strftime("%Y-%m-%d")
    cutoff = (datetime.now(ist) - timedelta(days=days)).strftime("%Y-%m-%d")
    # Get list of dates that actually have data
    try:
        with urllib.request.urlopen(f"{ANALYSER_URL}/api/dates", timeout=5) as r:
            all_dates = _json.loads(r.read())
        dates = [d for d in all_dates if d >= cutoff]
        # If today is missing from the date list, trigger a reimport and retry once
        if today not in dates:
            _analyser_import()
            with urllib.request.urlopen(f"{ANALYSER_URL}/api/dates", timeout=5) as r:
                all_dates = _json.loads(r.read())
            dates = [d for d in all_dates if d >= cutoff]
    except Exception:
        # Fallback: iterate last N days (slower)
        dates = []
        for i in range(min(days, 60)):
            dates.append((datetime.now(ist) - timedelta(days=i)).strftime("%Y-%m-%d"))
    trades = []
    for d in dates:
        try:
            with urllib.request.urlopen(f"{ANALYSER_URL}/api/trades?date={d}", timeout=5) as r:
                day_trades = _json.loads(r.read())
                # If a date returns empty unexpectedly, trigger reimport and retry once
                if not day_trades and d == today:
                    _analyser_import()
                    with urllib.request.urlopen(f"{ANALYSER_URL}/api/trades?date={d}", timeout=5) as r2:
                        day_trades = _json.loads(r2.read())
                trades.extend(day_trades)
        except Exception:
            pass
    return trades


@app.route("/api/equity_curve")
def api_equity_curve():
    """Return today's per-trade realized P&L sequence for equity curve chart."""
    import urllib.request as _ur
    import json as _json
    try:
        from datetime import date as _date
        today = str(_date.today())
        # Trigger import first so data is fresh
        try:
            req = _ur.Request(
                f"{ANALYSER_URL}/api/import",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            _ur.urlopen(req, timeout=5)
        except Exception:
            pass
        # Fetch today's trades
        with _ur.urlopen(f"{ANALYSER_URL}/api/trades?date={today}", timeout=5) as r:
            trades = _json.loads(r.read())
    except Exception as e:
        logger.warning("equity_curve fetch failed: %s", e)
        return jsonify({"trades": [], "floor": 0})

    closed = [t for t in trades if t.get("status") == "CLOSED"]
    # Sort by exit time
    closed.sort(key=lambda t: t.get("exit_time") or t.get("time") or "")

    # Get profit lock floor from risk state
    floor = 0.0
    if _monitor:
        try:
            st = _monitor.state
            if st.profit_lock_active:
                floor = st.profit_lock_floor
        except Exception:
            pass

    result = []
    for t in closed:
        result.append({
            "time": t.get("exit_time") or t.get("time") or "",
            "pnl": float(t.get("pnl") or 0),
            "symbol": t.get("symbol") or t.get("security_id") or "",
        })

    return jsonify({"trades": result, "floor": floor})


@app.route("/api/journal/daily_summaries")
def api_journal_daily_summaries():
    """Get daily P&L summaries from trade-analyser."""
    try:
        import urllib.request, json as _json
        days = int(request.args.get("days", 30))
        trades = _analyser_trades(days)
        # Group by date
        from collections import defaultdict
        by_date = defaultdict(list)
        for t in trades:
            by_date[t["date"]].append(t)
        summaries = []
        for d, ts in sorted(by_date.items(), reverse=True):
            pnls = [t["pnl"] for t in ts if t.get("pnl") is not None]
            winners = [p for p in pnls if p >= 0]
            losers  = [p for p in pnls if p < 0]
            summaries.append({
                "date": d,
                "total_trades": len(ts),
                "winners": len(winners),
                "losers": len(losers),
                "total_pnl": round(sum(pnls), 2),
                "gross_profit": round(sum(winners), 2),
                "gross_loss": round(sum(losers), 2),
                "win_rate": round(len(winners) / len(ts) * 100, 1) if ts else 0,
                "avg_win": round(sum(winners) / len(winners), 2) if winners else 0,
                "avg_loss": round(sum(losers) / len(losers), 2) if losers else 0,
            })
        return jsonify(summaries)
    except Exception as e:
        logger.warning("daily_summaries from analyser failed: %s", e)
        return jsonify([])


@app.route("/api/journal/analytics")
def api_journal_analytics():
    """Get aggregated analytics from trade-analyser."""
    try:
        days = int(request.args.get("days", 30))
        trades = _analyser_trades(days)
        if not trades:
            return jsonify({"total_days": 0, "total_trades": 0, "overall_pnl": 0,
                            "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                            "best_day": None, "worst_day": None,
                            "profitable_days": 0, "losing_days": 0, "avg_daily_pnl": 0,
                            "pnl_by_hour": [], "pnl_by_instrument": []})
        from collections import defaultdict
        pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
        winners = [p for p in pnls if p >= 0]
        losers  = [p for p in pnls if p < 0]
        by_date = defaultdict(float)
        for t in trades:
            if t.get("pnl") is not None:
                by_date[t["date"]] += t["pnl"]
        profitable_days = sum(1 for v in by_date.values() if v >= 0)
        best_day  = max(by_date.items(), key=lambda x: x[1]) if by_date else (None, 0)
        worst_day = min(by_date.items(), key=lambda x: x[1]) if by_date else (None, 0)
        # P&L by hour
        by_hour = defaultdict(float)
        for t in trades:
            if t.get("pnl") is not None and t.get("entry_time"):
                hr = int(t["entry_time"].split(":")[0])
                by_hour[hr] += t["pnl"]
        # P&L by instrument
        by_inst = defaultdict(lambda: {"total_pnl": 0, "trade_count": 0, "winners": 0})
        for t in trades:
            if t.get("pnl") is not None:
                key = f"{t.get('underlying','?')} {t.get('strike','?')} {t.get('option_type','?')}"
                by_inst[key]["total_pnl"] += t["pnl"]
                by_inst[key]["trade_count"] += 1
                if t["pnl"] >= 0:
                    by_inst[key]["winners"] += 1
        return jsonify({
            "total_days":      len(by_date),
            "total_trades":    len(trades),
            "overall_pnl":     round(sum(pnls), 2),
            "win_rate":        round(len(winners) / len(pnls) * 100, 1) if pnls else 0,
            "avg_win":         round(sum(winners) / len(winners), 2) if winners else 0,
            "avg_loss":        round(sum(losers) / len(losers), 2) if losers else 0,
            "best_day":        {"date": best_day[0],  "pnl": round(best_day[1], 2)},
            "worst_day":       {"date": worst_day[0], "pnl": round(worst_day[1], 2)},
            "profitable_days": profitable_days,
            "losing_days":     len(by_date) - profitable_days,
            "avg_daily_pnl":   round(sum(pnls) / len(by_date), 2) if by_date else 0,
            "pnl_by_hour":     [{"hour": h, "total_pnl": round(v, 2)} for h, v in sorted(by_hour.items())],
            "pnl_by_instrument": [{"instrument": k, **v, "total_pnl": round(v["total_pnl"], 2)}
                                   for k, v in sorted(by_inst.items(), key=lambda x: x[1]["total_pnl"])],
        })
    except Exception as e:
        logger.warning("analytics from analyser failed: %s", e)
        return jsonify({})


@app.route("/api/analyser/dates")
def api_analyser_dates():
    """Proxy /api/dates from trade-analyser."""
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"{ANALYSER_URL}/api/dates", timeout=5) as r:
            return jsonify(json.loads(r.read().decode()))
    except Exception as e:
        logger.warning("analyser /api/dates failed: %s", e)
        return jsonify([])


@app.route("/api/analytics/day_trades")
def api_analytics_day_trades():
    """Return individual trades for a given date from trade-analyser."""
    date_str = request.args.get("date", "")
    if not date_str:
        return jsonify([])
    try:
        import urllib.request as _ur
        url = f"{ANALYSER_URL}/api/trades?date={date_str}"
        with _ur.urlopen(url, timeout=3) as r:
            trades = json.loads(r.read().decode())
        return jsonify(trades if isinstance(trades, list) else [])
    except Exception as e:
        logger.warning("day_trades fetch failed for %s: %s", date_str, e)
        return jsonify([])


# ── Journal Entry Endpoints (screenshots + detailed per-trade) ───

@app.route("/api/journal/entry", methods=["POST"])
def api_journal_create_entry():
    data = request.json or {}
    if not _monitor:
        logger.warning("Journal entry POST: monitor not ready")
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    journal = _monitor.state.journal
    entry_id = journal.create_entry(data)
    logger.info("Journal entry created: %s instrument=%s", entry_id, data.get("instrument"))
    return jsonify({"status": "ok", "entry_id": entry_id})


@app.route("/api/journal/entry/<entry_id>", methods=["PUT"])
def api_journal_update_entry(entry_id):
    data = request.json or {}
    if not _monitor:
        return jsonify({"status": "error"}), 503
    journal = _monitor.state.journal
    action = data.get("action")
    if action == "exit":
        journal.update_entry_exit(entry_id, data)
    elif action == "notes":
        journal.update_notes(entry_id, data.get("notes", ""))
    return jsonify({"status": "ok"})


@app.route("/api/journal/entry/<entry_id>", methods=["DELETE"])
def api_journal_delete_entry(entry_id):
    if not _monitor:
        return jsonify({"status": "error"}), 503
    _monitor.state.journal.delete_entry(entry_id)
    return jsonify({"status": "ok"})


@app.route("/api/journal/clear_date", methods=["POST"])
def api_journal_clear_date():
    """Delete all journal entries for a given IST date."""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        date_str = request.json.get("date") if request.json else None
        if not date_str:
            from datetime import timezone, timedelta
            ist = timezone(timedelta(hours=5, minutes=30))
            date_str = datetime.now(ist).strftime("%Y-%m-%d")
        entries = _monitor.state.journal.get_entries(limit=2000, date_str=date_str)
        count = 0
        for e in entries:
            _monitor.state.journal.delete_entry(e["id"])
            count += 1
        # Reset journaling state so fresh import works
        _monitor._journaled_order_ids = set()
        if hasattr(_monitor, "_auto_journal_seeded"):
            del _monitor._auto_journal_seeded
        logger.info("Cleared %d journal entries for %s", count, date_str)
        return jsonify({"status": "ok", "deleted": count, "date": date_str})
    except Exception as e:
        logger.error("Clear date error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/journal/entries")
def api_journal_entries():
    if not _monitor:
        return jsonify([])
    limit = int(request.args.get("limit", 200))
    date_str = request.args.get("date")  # "YYYY-MM-DD" IST
    return jsonify(_monitor.state.journal.get_entries(limit=limit, date_str=date_str))


@app.route("/api/journal/backfill", methods=["POST"])
def api_journal_backfill():
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        from datetime import date as _date
        today = str(_date.today())
        # Trade book = only executed trades (no cancelled/rejected noise)
        # Prefer it over order book for backfill
        trades = _monitor.api.get_trade_book()
        orders = None
        source = None
        if trades:
            orders = _monitor._normalize_trade_book(trades)
            source = "trade_book"
        if not orders:
            orders = _monitor.api.get_order_book()
            if orders:
                source = "order_book"
        if not orders:
            # Trade history uses DD-MM-YYYY format
            today_dmy = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d-%m-%Y")
            hist = _monitor.api.get_trade_history(today_dmy, today_dmy)
            if hist:
                orders = _monitor._normalize_trade_book(hist)
                source = "trade_history"
        if not orders:
            cached = getattr(_monitor, "_last_orders", [])
            if cached:
                orders = cached
                source = "cached"
        if not orders:
            return jsonify({"status": "ok", "message": "No orders found. Try uploading a CSV from Dhan's Order Book → Export.", "created": 0})

        # Trade book / trade history / cached = individual legs → use CSV-style FIFO pairing
        # Order book = paired orders → use legacy auto-journal
        if source in ("trade_book", "trade_history", "cached"):
            created = _monitor._process_csv_trades(orders)
        else:
            _monitor._journaled_order_ids = set()
            if hasattr(_monitor, "_auto_journal_seeded"):
                del _monitor._auto_journal_seeded
            before_count = len(_monitor.state.journal.get_entries(limit=1000))
            _monitor._auto_journal_orders(orders)
            after_count = len(_monitor.state.journal.get_entries(limit=1000))
            created = after_count - before_count
        logger.info("Manual journal backfill (%s): %d records, %d new entries", source, len(orders), created)
        return jsonify({"status": "ok", "orders_scanned": len(orders), "created": created, "source": source})
    except Exception as e:
        logger.error("Journal backfill error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/journal/upload_csv", methods=["POST"])
def api_journal_upload_csv():
    """Import trades from a Dhan CSV order/trade export."""
    if not _monitor:
        return jsonify({"status": "error", "message": "monitor not ready"}), 503
    try:
        import csv, io
        f = request.files.get("file")
        if not f:
            return jsonify({"status": "error", "message": "No file uploaded"}), 400
        content = f.read().decode("utf-8-sig")  # utf-8-sig strips BOM
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            return jsonify({"status": "error", "message": "CSV is empty"}), 400
        all_keys = list(rows[0].keys())
        logger.info("CSV UPLOAD fields: %s", all_keys)
        logger.info("CSV UPLOAD first row: %s", dict(rows[0]))

        # Normalise CSV rows to order-book format
        # Supports two Dhan CSV formats:
        #   Trade book: Trade #, Stock Name, Transaction, Product Type, Quantity, Price (₹), Net Amount (₹), Timestamp
        #   Order book: Order Id, Trading Symbol, Transaction Type, Order Status, Filled Qty, Average Traded Price, ...
        def _parse_csv_row(r, row_idx):
            r_norm = {k.strip(): v.strip() for k, v in r.items()}
            r_lower = {k.lower(): v for k, v in r_norm.items()}
            def g(*keys):
                for k in keys:
                    v = r_norm.get(k) or r_lower.get(k.lower())
                    if v and v.strip():
                        return v.strip()
                return ""

            # Trade book format (Dhan trade history export)
            is_trade_book = "Stock Name" in r_norm or "Trade #" in r_norm

            symbol = g("Stock Name", "Trading Symbol", "Symbol", "Instrument", "tradingsymbol", "Scrip")
            txn    = g("Transaction", "Transaction Type", "Txn Type", "Buy/Sell", "Side", "transactiontype")
            price  = g("Price (₹)", "Price", "Average Traded Price", "Avg. Price", "Traded Price", "averagetradedprice") or "0"
            qty    = g("Quantity", "Filled Qty", "Traded Qty", "Qty", "filledqty", "Traded Quantity") or "0"
            ctime  = g("Timestamp", "Create Time", "Order Time", "Date Time", "Order Date Time", "Time", "Date")
            oid    = g("Trade #", "Order Id", "Order ID", "orderid") or (symbol + "_" + str(row_idx))
            seg    = g("Exchange Segment", "Exchange", "exchangesegment")
            # Security ID: trade book has no numeric ID — use symbol as key
            sid    = g("Security Id", "SecurityId", "Scrip Code", "securityid") or symbol

            # In trade book every row IS an executed trade
            if is_trade_book:
                status = "TRADED"
            else:
                raw_status = g("Order Status", "Status", "order_status")
                status = "TRADED" if raw_status.upper().replace(" ", "") in (
                    "TRADED", "COMPLETE", "COMPLETED", "FILLED", "FULLYEXECUTED", "EXECUTED") else raw_status

            txn_up = txn.upper().strip()
            if txn_up in ("S", "SELL", "SHORT", "SELL ORDER"):
                txn = "SELL"
            elif txn_up in ("B", "BUY", "LONG", "BUY ORDER"):
                txn = "BUY"

            try:
                price_f = float(str(price).replace(",", "").replace("₹", "").strip())
            except Exception:
                price_f = 0.0
            try:
                qty_i = int(float(str(qty).replace(",", "")))
            except Exception:
                qty_i = 0

            return {
                "orderId": oid,
                "orderStatus": status,
                "transactionType": txn,
                "securityId": sid,
                "tradingSymbol": symbol,
                "exchangeSegment": seg,
                "price": price_f,
                "averageTradedPrice": price_f,
                "filledQty": qty_i,
                "quantity": qty_i,
                "createTime": ctime,
                "updateTime": ctime,
                "exchangeTime": ctime,
            }

        all_parsed = [_parse_csv_row(r, i) for i, r in enumerate(rows)]
        orders = [o for o in all_parsed if o["orderStatus"] == "TRADED"]
        if not orders:
            if all_parsed:
                sample = all_parsed[0]
                logger.warning("CSV: 0 TRADED found. Sample: status=%r txn=%r symbol=%r sid=%r qty=%r",
                               sample["orderStatus"], sample["transactionType"],
                               sample["tradingSymbol"], sample["securityId"], sample["quantity"])
            return jsonify({"status": "ok",
                            "message": f"No executed trades found in CSV ({len(all_parsed)} rows parsed)",
                            "rows_scanned": 0, "created": 0})

        created = _monitor._process_csv_trades(orders)
        logger.info("CSV journal import: %d rows processed, %d entries created", len(orders), created)
        return jsonify({"status": "ok", "rows_scanned": len(orders), "created": created, "source": "csv"})
    except Exception as e:
        logger.error("CSV upload error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/journal/open_entry/<security_id>")
def api_journal_open_entry(security_id):
    """Return the most recent open journal entry for a sell leg security_id."""
    if not _monitor:
        return jsonify({"entry_id": None})
    journal = _monitor.state.journal
    with journal._lock:
        row = journal._conn.execute(
            "SELECT id FROM trade_entries WHERE sell_security_id=? AND status='open' ORDER BY created_at DESC LIMIT 1",
            (str(security_id),)
        ).fetchone()
    return jsonify({"entry_id": row[0] if row else None})


@app.route("/api/journal/screenshot", methods=["POST"])
def api_journal_screenshot():
    data = request.json or {}
    data_url = data.get("data_url", "")
    if not data_url or not _monitor:
        return jsonify({"status": "error"}), 400
    filename = _monitor.state.journal.save_screenshot(data_url)
    return jsonify({"status": "ok", "filename": filename})


@app.route("/api/journal/screenshots/<filename>")
def api_journal_screenshot_file(filename):
    import os as _os
    from flask import send_file
    from trade_journal import SCREENSHOTS_DIR
    path = _os.path.join(SCREENSHOTS_DIR, filename)
    if not _os.path.exists(path):
        return "", 404
    return send_file(path, mimetype="image/png")


@app.route("/api/chart/nifty")
def api_chart_nifty():
    """Nifty index 1m candles for today — used for journal screenshots."""
    if not _monitor:
        return jsonify({"candles": []})
    from datetime import date as _date, timedelta as _td
    today = _date.today().strftime("%Y-%m-%d")
    # Try NIFTY index (security_id=13, NSE_EQ)
    raw = _monitor.api.get_chart_data("13", "NSE_EQ", "INDEX", today, today)
    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    candles = []
    ts_list = data.get("timestamp", [])
    opens   = data.get("open",   [])
    highs   = data.get("high",   [])
    lows    = data.get("low",    [])
    closes  = data.get("close",  [])
    for i, ts in enumerate(ts_list):
        try:
            if isinstance(ts, str):
                from datetime import datetime as _dt
                unix_ts = int(_dt.strptime(f"{today} {ts}", "%Y-%m-%d %H:%M:%S").timestamp())
            else:
                unix_ts = int(ts)
            candles.append({"time": unix_ts, "open": float(opens[i]),
                             "high": float(highs[i]), "low": float(lows[i]),
                             "close": float(closes[i])})
        except Exception:
            pass
    candles.sort(key=lambda c: c["time"])
    return jsonify({"candles": candles[-150:]})


@app.route("/straddle")
def straddle_page():
    return _build_straddle_page()


@app.route("/api/straddle/spot")
def api_straddle_spot():
    """Return current spot price for the given underlying."""
    underlying = request.args.get("underlying", "NIFTY").upper()
    if underlying in ("SENSEX", "BANKEX"):
        return jsonify({"spot": _bse_last_spot, "underlying": underlying})
    # NIFTY: find most recent option chain cache entry for this underlying
    try:
        spot = 0
        for key, val in _oc_data_cache.items():
            if isinstance(key, tuple) and key[0] == underlying:
                cached_spot = val[1].get("spot", 0) if isinstance(val, tuple) else val.get("spot", 0)
                if cached_spot > 0:
                    spot = cached_spot
                    break
        return jsonify({"spot": spot, "underlying": underlying})
    except Exception:
        return jsonify({"spot": 0})


@app.route("/api/straddle/strikes")
def api_straddle_strikes():
    """Return sorted list of available strikes for a given underlying + expiry."""
    underlying = request.args.get("underlying", "NIFTY").upper()
    expiry     = request.args.get("expiry", "")
    exchange   = "BSE" if underlying in ("SENSEX", "BANKEX") else "NSE"
    if not _instrument_cache or not expiry:
        return jsonify([])
    strikes = set()
    for inst in _instrument_cache._instruments:
        if inst.instrument_type != "OPTIDX":
            continue
        if not inst.trading_symbol.upper().startswith(underlying + "-"):
            continue
        if not inst.expiry_date or inst.expiry_date[:10] != expiry:
            continue
        if getattr(inst, "exchange", "NSE") != exchange:
            continue
        strikes.add(inst.strike_price)
    return jsonify(sorted(strikes))


@app.route("/api/straddle/expiries")
def api_straddle_expiries():
    """Return sorted list of expiry dates for a given underlying."""
    underlying = request.args.get("underlying", "NIFTY").upper()
    exchange = "BSE" if underlying in ("SENSEX", "BANKEX") else "NSE"
    if not _instrument_cache:
        return jsonify([])
    from datetime import datetime as _dt2, timedelta, timezone
    _ist = timezone(timedelta(hours=5, minutes=30))
    today_ist = _dt2.now(_ist).date().strftime("%Y-%m-%d")
    seen = set()
    for inst in _instrument_cache._instruments:
        if inst.instrument_type != "OPTIDX":
            continue
        if not inst.trading_symbol.upper().startswith(underlying + "-"):
            continue
        if getattr(inst, "exchange", "NSE") != exchange:
            continue
        if inst.expiry_date and inst.expiry_date[:10] >= today_ist:
            seen.add(inst.expiry_date[:10])
    return jsonify(sorted(seen))


@app.route("/api/straddle/chart")
def api_straddle_chart():
    """
    Fetch combined CE+PE intraday 1m candles and resample to requested timeframe.
    Returns [{time, open, high, low, close}] summed across both legs.
    """
    underlying = request.args.get("underlying", "NIFTY").upper()
    expiry     = request.args.get("expiry", "")
    try:
        ce_strike = float(request.args.get("ce_strike", 0))
        pe_strike = float(request.args.get("pe_strike", 0))
    except ValueError:
        return jsonify({"error": "invalid strike"}), 400
    tf = int(request.args.get("tf", 1))  # 1, 3, or 5

    exchange = "BSE" if underlying in ("SENSEX", "BANKEX") else "NSE"
    exchange_segment = "BSE_FNO" if exchange == "BSE" else "NSE_FNO"

    # Find CE and PE security IDs from instrument cache (different strikes)
    ce_id = pe_id = None
    if _instrument_cache:
        for inst in _instrument_cache._instruments:
            if inst.instrument_type != "OPTIDX":
                continue
            if not inst.trading_symbol.upper().startswith(underlying + "-"):
                continue
            if not inst.expiry_date or inst.expiry_date[:10] != expiry:
                continue
            if getattr(inst, "exchange", "NSE") != exchange:
                continue
            if inst.option_type == "CE" and abs(inst.strike_price - ce_strike) < 0.01:
                ce_id = inst.security_id
            elif inst.option_type == "PE" and abs(inst.strike_price - pe_strike) < 0.01:
                pe_id = inst.security_id

    if not ce_id or not pe_id:
        return jsonify({"error": f"Could not find instruments: CE {underlying} {ce_strike} / PE {underlying} {pe_strike} exp {expiry}"}), 404

    # Fetch 1m candles for both legs (today + previous trading day)
    from datetime import timedelta, date as _date, datetime as _dt, timezone

    def _prev_weekday(d):
        prev = d - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        return prev

    # Use IST date — VPS is UTC; after 18:30 UTC the IST date has already rolled over
    _ist = timezone(timedelta(hours=5, minutes=30))
    today_d    = _dt.now(_ist).date()
    prev_d     = _prev_weekday(today_d)

    def _parse_raw(raw):
        """Parse get_chart_data response into {unix_ts: {o,h,l,c}} dict."""
        data = raw if isinstance(raw, dict) else {}
        if "data" in data and isinstance(data["data"], dict):
            data = data["data"]
        timestamps = data.get("timestamp", data.get("start_Time", []))
        opens  = data.get("open",  [])
        highs  = data.get("high",  [])
        lows   = data.get("low",   [])
        closes = data.get("close", [])
        result = {}
        _IST_OFFSET = 19800  # IST = UTC+5:30 = 5.5*3600 seconds
        for i, ts in enumerate(timestamps):
            try:
                # Dhan returns IST strings. Naive parse on UTC server treats them as UTC
                # (5.5h ahead). Subtract IST offset to get real UTC unix timestamp.
                unix_ts = int(_dt.strptime(str(ts), "%Y-%m-%d %H:%M:%S").timestamp()) - _IST_OFFSET
            except (ValueError, TypeError):
                try:
                    unix_ts = int(ts)
                except (ValueError, TypeError):
                    continue
            o = opens[i]  if i < len(opens)  else 0
            h = highs[i]  if i < len(highs)  else 0
            l = lows[i]   if i < len(lows)   else 0
            c = closes[i] if i < len(closes) else 0
            if o > 0 and h > 0 and l > 0 and c > 0:
                result[unix_ts] = {"o": o, "h": h, "l": l, "c": c}
        return result

    def fetch_candles(sec_id):
        import time as _time
        result = {}
        for d in (prev_d, today_d):
            date_str = d.strftime("%Y-%m-%d")
            for attempt in range(3):
                try:
                    if attempt:
                        _time.sleep(attempt * 1.5)
                    raw = _monitor.api.get_chart_data(
                        security_id=sec_id,
                        exchange_segment=exchange_segment,
                        instrument_type="OPTIDX",
                        from_date=date_str,
                        to_date=date_str,
                    )
                    # Retry on rate limit
                    if isinstance(raw, dict) and raw.get("status") == "failure":
                        err = (raw.get("remarks") or {}).get("error_code", "")
                        if err == "DH-904" and attempt < 2:
                            logger.warning("straddle rate-limit %s %s, retry %d", sec_id, date_str, attempt + 1)
                            continue
                    result.update(_parse_raw(raw))
                    break
                except Exception as e:
                    logger.warning("straddle fetch_candles %s %s: %s", sec_id, date_str, e)
                    break
            _time.sleep(0.4)  # space out calls to stay under rate limit
        return result

    ce_bars = fetch_candles(ce_id)
    pe_bars = fetch_candles(pe_id)
    logger.info("straddle chart: ce=%d bars pe=%d bars common=%d", len(ce_bars), len(pe_bars), len(set(ce_bars) & set(pe_bars)))

    # Merge on common timestamps
    common_ts = sorted(set(ce_bars) & set(pe_bars))
    merged = []
    for ts in common_ts:
        ce = ce_bars[ts]
        pe = pe_bars[ts]
        comb_o = ce["o"] + pe["o"]
        comb_c = ce["c"] + pe["c"]
        body_hi = max(comb_o, comb_c)
        body_lo = min(comb_o, comb_c)
        # CE and PE are inversely correlated on spot moves — if CE wicks high, PE wicks low.
        # Use average of both legs' wicks * 0.5: spot-driven wicks roughly cancel (×0.5 discount),
        # IV-driven wicks (same direction on both) show through at ~full size.
        ce_wu = ce["h"] - max(ce["o"], ce["c"])
        pe_wu = pe["h"] - max(pe["o"], pe["c"])
        ce_wd = min(ce["o"], ce["c"]) - ce["l"]
        pe_wd = min(pe["o"], pe["c"]) - pe["l"]
        wick_up = round((ce_wu + pe_wu) * 0.5, 2)
        wick_dn = round((ce_wd + pe_wd) * 0.5, 2)
        merged.append({
            "time":  ts,
            "open":  round(comb_o, 2),
            "high":  round(body_hi + wick_up, 2),
            "low":   round(body_lo - wick_dn, 2),
            "close": round(comb_c, 2),
        })

    # Resample to tf minutes
    if tf > 1:
        resampled = []
        i = 0
        while i < len(merged):
            bucket = merged[i:i+tf]
            resampled.append({
                "time":  bucket[0]["time"],
                "open":  bucket[0]["open"],
                "high":  max(b["high"] for b in bucket),
                "low":   min(b["low"]  for b in bucket),
                "close": bucket[-1]["close"],
            })
            i += tf
        merged = resampled

    # Last close of each leg for the stat bar
    last_ce_ltp = ce_bars[common_ts[-1]]["c"] if common_ts else 0
    last_pe_ltp = pe_bars[common_ts[-1]]["c"] if common_ts else 0
    return jsonify({"candles": merged, "ce_id": ce_id, "pe_id": pe_id,
                    "ce_ltp": last_ce_ltp, "pe_ltp": last_pe_ltp})


def _build_straddle_page():
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Strangle Chart</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}
body{display:flex;flex-direction:column;}
.topbar{background:#161b22;border-bottom:1px solid #21262d;padding:6px 14px;display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap;}
.topbar h1{font-size:13px;font-weight:700;white-space:nowrap;}
.topbar a{font-size:11px;color:#8b949e;text-decoration:none;padding:3px 8px;border:1px solid #30363d;border-radius:5px;}
.spot-pill{background:#0d1117;border:1px solid #58a6ff;border-radius:16px;padding:3px 12px;font-size:12px;font-weight:700;color:#58a6ff;}
select{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:4px 8px;border-radius:5px;font-size:12px;outline:none;cursor:pointer;}
select:focus{border-color:#58a6ff;}
.lbl{font-size:10px;color:#8b949e;white-space:nowrap;}
.tf-btn{padding:3px 8px;border-radius:4px;border:1px solid #30363d;background:none;color:#8b949e;cursor:pointer;font-size:11px;font-weight:600;}
.tf-btn.active{background:#1f6feb;border-color:#1f6feb;color:#fff;}
.btn-go{padding:4px 14px;border-radius:5px;border:none;background:#238636;color:#fff;cursor:pointer;font-size:12px;font-weight:700;}
.btn-go:hover{background:#2ea043;}
.btn-fs{padding:3px 8px;border-radius:4px;border:1px solid #30363d;background:none;color:#8b949e;cursor:pointer;font-size:14px;line-height:1;}
.btn-fs:hover{color:#e6edf3;}
.statbar{background:#0d1117;border-bottom:1px solid #21262d;padding:4px 14px;display:none;gap:18px;align-items:center;font-size:11px;flex-shrink:0;}
.si{display:flex;gap:4px;align-items:baseline;}
.sl{color:#8b949e;}
.sv{font-weight:700;}
.pos{color:#3fb950;}.neg{color:#f85149;}.neu{color:#e6edf3;}
#err{color:#f85149;font-size:11px;}
#panels{flex:1;display:flex;flex-direction:column;min-height:0;}
.panel{position:relative;overflow:hidden;min-height:40px;}
#panel-main{flex:1;min-height:60px;}
#panel-macd{height:120px;}
#panel-rsi {height:100px;}
.drag-handle{height:5px;background:transparent;cursor:ns-resize;flex-shrink:0;
             border-top:1px solid #21262d;position:relative;z-index:10;}
.drag-handle:hover,.drag-handle.dragging{background:#1f6feb;}
.panel-label{position:absolute;top:4px;left:8px;font-size:10px;color:#6e7681;z-index:2;white-space:nowrap;display:flex;align-items:center;gap:6px;}
.exp-btn{background:none;border:none;color:#484f58;cursor:pointer;font-size:12px;padding:0 2px;line-height:1;pointer-events:all;}
.exp-btn:hover{color:#8b949e;}
.overlay-msg{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#8b949e;font-size:13px;pointer-events:none;text-align:center;z-index:5;}
</style>
</head>
<body>

<div class="topbar">
  <h1>&#x1F4C8; Strangle</h1>
  <a href="/">&#x2190; Dashboard</a>
  <span class="lbl">Index</span>
  <select id="sel-ul" onchange="onUlChange()">
    <option value="NIFTY">NIFTY</option>
    <option value="SENSEX">SENSEX</option>
  </select>
  <span class="spot-pill" id="spot-display">Spot: --</span>
  <span class="lbl">Expiry</span>
  <select id="sel-exp" onchange="onExpChange()"><option value="">-- --</option></select>
  <span class="lbl">CE</span>
  <select id="sel-ce"><option value="">--</option></select>
  <span class="lbl">PE</span>
  <select id="sel-pe"><option value="">--</option></select>
  <div style="display:flex;gap:3px;">
    <button class="tf-btn active" data-tf="1" onclick="setTf(1)">1m</button>
    <button class="tf-btn" data-tf="3" onclick="setTf(3)">3m</button>
    <button class="tf-btn" data-tf="5" onclick="setTf(5)">5m</button>
  </div>
  <button class="btn-go" onclick="doLoad()">&#x25B6; Load</button>
  <span id="err"></span>
  <button class="btn-fs" onclick="toggleFullscreen()" title="Fullscreen">&#x26F6;</button>
  <span style="margin-left:auto;font-size:10px;color:#484f58;" id="ts"></span>
</div>

<div class="statbar" id="statbar">
  <div class="si"><span class="sl">Premium</span><span class="sv neu" id="s-ltp">-</span></div>
  <div class="si"><span class="sl">Open</span><span class="sv neu" id="s-open">-</span></div>
  <div class="si"><span class="sl">High</span><span class="sv pos" id="s-high">-</span></div>
  <div class="si"><span class="sl">Low</span><span class="sv neg" id="s-low">-</span></div>
  <div class="si"><span class="sl">Chg</span><span class="sv" id="s-chg">-</span></div>
  <div class="si"><span class="sl">CE</span><span class="sv neu" id="s-ce-val">-</span></div>
  <div class="si"><span class="sl">PE</span><span class="sv neu" id="s-pe-val">-</span></div>
</div>

<div id="panels">
  <div class="panel" id="panel-main">
    <div class="panel-label">STRANGLE &nbsp;&#x2013;&nbsp; <span id="lbl-strikes"></span> &nbsp;&#x2013;&nbsp; <span style="color:#3db8f5;">EMA20</span> &nbsp;<span style="color:#f0883e;">EMA50</span>
      <button class="exp-btn" onclick="expandPane('main')" title="Expand">&#x2922;</button></div>
    <div id="chart-main" style="width:100%;height:100%;"></div>
    <div class="overlay-msg" id="overlay">Select index, expiry &amp; strikes then click Load</div>
  </div>
  <div class="drag-handle" id="drag-macd" title="Drag to resize"></div>
  <div class="panel" id="panel-macd">
    <div class="panel-label">MACD (12,26,9) &nbsp;&#x2013;&nbsp; <span style="color:#ff6d00;">&#x25A0;</span> MACD &nbsp;<span style="color:#2962ff;">&#x25A0;</span> Signal
      <button class="exp-btn" onclick="expandPane('macd')" title="Expand">&#x2922;</button></div>
    <div id="chart-macd" style="width:100%;height:100%;"></div>
  </div>
  <div class="drag-handle" id="drag-rsi" title="Drag to resize"></div>
  <div class="panel" id="panel-rsi">
    <div class="panel-label">RSI (14)
      <button class="exp-btn" onclick="expandPane('rsi')" title="Expand">&#x2922;</button></div>
    <div id="chart-rsi" style="width:100%;height:100%;"></div>
  </div>
</div>

<script>
var _tf=1, _spot=0, _refreshTimer=null, _rangeSyncing=false, _crossSyncing=false;
var _cMain=null, _sCandle=null, _sEma20=null, _sEma50=null;
var _cMacd=null, _sMacdBars=null, _sMacdSigBars=null, _sMacdZero=null;
var _cRsi=null, _sRsi=null, _sRsiOb=null, _sRsiOs=null;

var BG='#0d1117', GRID='#161b22', BORDER='#21262d', TEXT='#8b949e';
var GRN='#3fb950', RED='#f85149', BLUE='#3db8f5', AMBER='#f0883e';

var _MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
// Always display as IST (UTC+5:30) regardless of browser timezone
function _istDate(unixSec) {
    return new Date((unixSec + 19800) * 1000);
}
function _fmtTimeCross(t) {
    var d = _istDate(t);
    return d.getUTCHours().toString().padStart(2,'0') + ':' + d.getUTCMinutes().toString().padStart(2,'0');
}
function _fmtTick(t, type) {
    var d = _istDate(t);
    if (type === 0) return d.getUTCFullYear().toString();
    if (type === 1) return _MONTHS[d.getUTCMonth()];
    if (type === 2) return d.getUTCDate() + ' ' + _MONTHS[d.getUTCMonth()];
    return d.getUTCHours().toString().padStart(2,'0') + ':' + d.getUTCMinutes().toString().padStart(2,'0');
}

function _base(showTime) {
    return {
        width:0, height:0,
        layout:{background:{type:'Solid',color:BG}, textColor:TEXT},
        grid:{vertLines:{color:GRID}, horzLines:{color:GRID}},
        crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
        rightPriceScale:{borderColor:BORDER, scaleMargins:{top:0.08,bottom:0.08}},
        localization:{timeFormatter:_fmtTimeCross},
        timeScale:{borderColor:BORDER, timeVisible:true, secondsVisible:false,
                   visible:showTime, tickMarkFormatter:_fmtTick},
        handleScroll:true, handleScale:true,
    };
}

// ── Init ─────────────────────────────────────────────────────────
function initCharts() {
    var mainEl=document.getElementById('chart-main');
    var macdEl=document.getElementById('chart-macd');
    var rsiEl =document.getElementById('chart-rsi');

    _cMain = LightweightCharts.createChart(mainEl, _base(false));
    _sCandle = _cMain.addSeries(LightweightCharts.CandlestickSeries, {
        upColor:GRN, downColor:RED, borderUpColor:GRN, borderDownColor:RED,
        wickUpColor:GRN, wickDownColor:RED,
    });
    _sEma20 = _cMain.addSeries(LightweightCharts.LineSeries,
        {color:BLUE,  lineWidth:1, priceLineVisible:false, lastValueVisible:false,
         crosshairMarkerVisible:false});
    _sEma50 = _cMain.addSeries(LightweightCharts.LineSeries,
        {color:AMBER, lineWidth:1, priceLineVisible:false, lastValueVisible:false,
         crosshairMarkerVisible:false});

    _cMacd = LightweightCharts.createChart(macdEl, _base(false));
    _sMacdBars = _cMacd.addSeries(LightweightCharts.HistogramSeries,
        {color:'#ff6d00', priceLineVisible:false, lastValueVisible:false,
         crosshairMarkerVisible:false});
    _sMacdSigBars = _cMacd.addSeries(LightweightCharts.HistogramSeries,
        {color:'#2962ff', priceLineVisible:false, lastValueVisible:false,
         crosshairMarkerVisible:false});
    // Zero line anchors MACD chart time axis over full range (like _sRsiOb for RSI)
    _sMacdZero = _cMacd.addSeries(LightweightCharts.LineSeries,
        {color:'rgba(100,100,100,0.35)', lineWidth:1, priceLineVisible:false,
         lastValueVisible:false, crosshairMarkerVisible:false});

    // RSI owns the time axis at the bottom
    _cRsi = LightweightCharts.createChart(rsiEl, _base(true));
    _sRsi   = _cRsi.addSeries(LightweightCharts.LineSeries,
        {color:'#2962ff', lineWidth:1, priceLineVisible:false, lastValueVisible:true,
         crosshairMarkerVisible:false});
    _sRsiOb = _cRsi.addSeries(LightweightCharts.LineSeries,
        {color:'rgba(248,81,73,0.4)', lineWidth:1, priceLineVisible:false,
         lastValueVisible:false, lineStyle:LightweightCharts.LineStyle.Dashed,
         crosshairMarkerVisible:false});
    _sRsiOs = _cRsi.addSeries(LightweightCharts.LineSeries,
        {color:'rgba(63,185,80,0.4)', lineWidth:1, priceLineVisible:false,
         lastValueVisible:false, lineStyle:LightweightCharts.LineStyle.Dashed,
         crosshairMarkerVisible:false});

    syncRange();
    syncCrosshairs();
}

// ── Crosshair sync — official LW Charts pattern ───────────────────
function getCrossDataPoint(series, param) {
    if (!param.time) return null;
    return param.seriesData.get(series) || null;
}
function applySync(chart, series, dp) {
    if (dp) {
        var price = dp.close !== undefined ? dp.close : dp.value;
        chart.setCrosshairPosition(price, dp.time, series);
    } else {
        chart.clearCrosshairPosition();
    }
}
function syncCrosshairs() {
    // Guard against feedback loop: setCrosshairPosition fires subscribeCrosshairMove on target
    _cMain.subscribeCrosshairMove(function(p) {
        if (_crossSyncing) return;
        _crossSyncing = true;
        var dp = getCrossDataPoint(_sCandle, p);
        if (dp && p.time) document.getElementById('s-ltp').textContent = dp.close.toFixed(2);
        applySync(_cMacd, _sMacdZero, dp);
        applySync(_cRsi,  _sRsiOb,    dp);
        _crossSyncing = false;
    });
    _cMacd.subscribeCrosshairMove(function(p) {
        if (_crossSyncing) return;
        _crossSyncing = true;
        var dp = getCrossDataPoint(_sMacdZero, p);
        applySync(_cMain, _sCandle, dp);
        applySync(_cRsi,  _sRsiOb,  dp);
        _crossSyncing = false;
    });
    _cRsi.subscribeCrosshairMove(function(p) {
        if (_crossSyncing) return;
        _crossSyncing = true;
        var dp = getCrossDataPoint(_sRsiOb, p);
        applySync(_cMain, _sCandle,   dp);
        applySync(_cMacd, _sMacdZero, dp);
        _crossSyncing = false;
    });
}


// ── Per-pane expand/collapse (like TradingView) ───────────────────
var _expandedPane = null;
var _paneIds = ['main','macd','rsi'];

function expandPane(id) {
    if (_expandedPane === id) {
        // Restore all panes
        _expandedPane = null;
        _paneIds.forEach(function(p) {
            var el = document.getElementById('panel-'+p);
            el.style.display = '';
            el.style.flex    = '';
            el.style.height  = '';
        });
        document.getElementById('panel-main').style.flex = '1';
        // Fixed heights restored by resizeAll
    } else {
        _expandedPane = id;
        _paneIds.forEach(function(p) {
            var el = document.getElementById('panel-'+p);
            if (p === id) {
                el.style.display = '';
                el.style.flex    = '1';
                el.style.height  = '';
            } else {
                el.style.display = 'none';
            }
        });
    }
    resizeAll();
}

// ── Sync scrolling/zoom ──────────────────────────────────────────
function syncRange() {
    [_cMain, _cMacd, _cRsi].forEach(function(src) {
        src.timeScale().subscribeVisibleLogicalRangeChange(function(r) {
            if (_rangeSyncing || !r) return;
            _rangeSyncing = true;
            [_cMain, _cMacd, _cRsi].forEach(function(dst) {
                if (dst !== src) dst.timeScale().setVisibleLogicalRange(r);
            });
            _rangeSyncing = false;
        });
    });
}


// ── Resize ───────────────────────────────────────────────────────
function resizeAll() {
    var panelsEl = document.getElementById('panels');
    var totalH = panelsEl.clientHeight;
    var w = panelsEl.clientWidth;
    if (_expandedPane) {
        var h = totalH;
        if (_expandedPane==='main' && _cMain) _cMain.resize(w, h);
        if (_expandedPane==='macd' && _cMacd) _cMacd.resize(w, h);
        if (_expandedPane==='rsi'  && _cRsi)  _cRsi.resize(w, h);
        return;
    }
    var macdEl = document.getElementById('panel-macd');
    var rsiEl  = document.getElementById('panel-rsi');
    var macdH = macdEl.clientHeight || 120;
    var rsiH  = rsiEl.clientHeight  || 100;
    // Account for drag handle heights (5px each)
    var mainH = Math.max(60, totalH - macdH - rsiH - 10);
    document.getElementById('panel-main').style.height = mainH + 'px';
    if (_cMain) _cMain.resize(w, mainH);
    if (_cMacd) _cMacd.resize(w, macdH);
    if (_cRsi)  _cRsi.resize(w, rsiH);

    // Also resize Lightweight Chart inside Option Chain workspace if visible
    var canvasDiv = document.getElementById('dom-chart-canvas');
    if (canvasDiv && _lwChart) {
        var canvasW = canvasDiv.clientWidth || 600;
        var canvasH = 420;
        if (document.documentElement.classList.contains('chart-trading-mode')) {
            canvasH = window.innerHeight - 230;
            if (canvasH < 300) canvasH = 300;
        } else if (window._chartMaximized) {
            canvasH = 550;
        }
        canvasDiv.style.height = canvasH + 'px';
        _lwChart.resize(canvasW, canvasH);
    }
}

// ── Drag-to-resize handles ────────────────────────────────────────
function initDragHandles() {
    makeDraggable('drag-macd', 'panel-macd', false);
    makeDraggable('drag-rsi',  'panel-rsi',  false);
}

function makeDraggable(handleId, panelId, above) {
    var handle = document.getElementById(handleId);
    var panel  = document.getElementById(panelId);
    var startY, startH;
    handle.addEventListener('mousedown', function(e) {
        e.preventDefault();
        startY = e.clientY;
        startH = panel.clientHeight;
        handle.classList.add('dragging');
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
    function onMove(e) {
        var delta = e.clientY - startY;
        var newH  = Math.max(40, startH + delta);
        panel.style.height = newH + 'px';
        resizeAll();
    }
    function onUp() {
        handle.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
    }
}

// ── Fullscreen toggle ────────────────────────────────────────────
function toggleFullscreen() {
    if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(function(){});
    } else {
        document.exitFullscreen().catch(function(){});
    }
}
document.addEventListener('fullscreenchange', function() { resizeAll(); });

// ── Spot poll ────────────────────────────────────────────────────
function pollSpot() {
    var ul = document.getElementById('sel-ul').value;
    fetch('/api/straddle/spot?underlying='+ul)
        .then(function(r){return r.json();})
        .then(function(d) {
            _spot = d.spot || 0;
            if (_spot > 0)
                document.getElementById('spot-display').textContent =
                    ul + '\\u00A0' + Math.round(_spot).toLocaleString('en-IN');
        }).catch(function(){});
}

// ── Controls ─────────────────────────────────────────────────────
function onUlChange() {
    var u = document.getElementById('sel-ul').value;
    var expSel = document.getElementById('sel-exp');
    expSel.innerHTML = '<option value="">Loading...</option>';
    document.getElementById('sel-ce').innerHTML = '<option value="">--</option>';
    document.getElementById('sel-pe').innerHTML = '<option value="">--</option>';
    _spot = 0;
    document.getElementById('spot-display').textContent = 'Spot: --';
    Promise.all([
        fetch('/api/straddle/expiries?underlying='+u).then(function(r){return r.json();}),
        fetch('/api/straddle/spot?underlying='+u).then(function(r){return r.json();})
    ]).then(function(res) {
        var dates=res[0], spotD=res[1];
        _spot = spotD.spot || 0;
        if (_spot > 0)
            document.getElementById('spot-display').textContent =
                u + '\\u00A0' + Math.round(_spot).toLocaleString('en-IN');
        expSel.innerHTML = '<option value="">-- Expiry --</option>';
        dates.forEach(function(d){
            var o=document.createElement('option'); o.value=d; o.textContent=d; expSel.appendChild(o);
        });
        if (dates.length > 0) { expSel.value = dates[0]; onExpChange(); }
    }).catch(function(){ expSel.innerHTML = '<option value="">Error</option>'; });
}

function onExpChange() {
    var u=document.getElementById('sel-ul').value;
    var ex=document.getElementById('sel-exp').value;
    if (!ex) return;
    fetch('/api/straddle/strikes?underlying='+u+'&expiry='+ex)
        .then(function(r){return r.json();})
        .then(function(strikes) {
            populateStrikes('sel-ce', strikes, 'CE');
            populateStrikes('sel-pe', strikes, 'PE');
        }).catch(function(){});
}

function populateStrikes(selId, strikes, side) {
    var sel = document.getElementById(selId);
    sel.innerHTML = '';
    strikes.forEach(function(s){
        var o=document.createElement('option'); o.value=s;
        o.textContent=s.toLocaleString('en-IN'); sel.appendChild(o);
    });
    if (_spot > 0 && strikes.length > 0) {
        if (side === 'CE') {
            var above = strikes.filter(function(s){return s > _spot;});
            sel.value = above.length > 0 ? above[0] : strikes[strikes.length-1];
        } else {
            var below = strikes.filter(function(s){return s < _spot;});
            sel.value = below.length > 0 ? below[below.length-1] : strikes[0];
        }
    }
}

function setTf(tf) {
    _tf = tf;
    document.querySelectorAll('.tf-btn').forEach(function(b){
        b.classList.toggle('active', parseInt(b.dataset.tf) === tf);
    });
}

// ── Indicators ───────────────────────────────────────────────────
function calcEma(closes, period) {
    var k=2/(period+1), ema=closes[0], out=[];
    for (var i=0; i<closes.length; i++) {
        if (i < period-1) { out.push(null); continue; }
        ema = i===0 ? closes[0] : closes[i]*k + ema*(1-k);
        out.push(parseFloat(ema.toFixed(4)));
    }
    return out;
}

function calcMacd(closes, fast, slow, sig) {
    // Compute raw EMAs without null warm-up for internal use
    var k_f=2/(fast+1), k_s=2/(slow+1), k_g=2/(sig+1);
    var ef=[], es=[];
    var ema_f=closes[0], ema_s=closes[0];
    for (var i=0; i<closes.length; i++) {
        ema_f = i===0 ? closes[0] : closes[i]*k_f + ema_f*(1-k_f);
        ema_s = i===0 ? closes[0] : closes[i]*k_s + ema_s*(1-k_s);
        ef.push(ema_f); es.push(ema_s);
    }
    var ml=[], sl=[], hi=[], ema_g=ef[0]-es[0];
    for (var i=0; i<closes.length; i++) {
        var m = ef[i]-es[i];
        ema_g = i===0 ? m : m*k_g + ema_g*(1-k_g);
        // MACD null for first slow-1 bars; Signal null for first slow+sig-2 bars
        ml.push(i < slow-1 ? null : parseFloat(m.toFixed(4)));
        sl.push(i < slow+sig-2 ? null : parseFloat(ema_g.toFixed(4)));
        hi.push((i < slow+sig-2) ? null : parseFloat((m - ema_g).toFixed(4)));
    }
    return {macd:ml, signal:sl, hist:hi};
}

function calcRsi(closes, period) {
    var out=[];
    for (var i=0; i<closes.length; i++) {
        if (i < period) { out.push(null); continue; }
        var g=0, l=0;
        for (var j=i-period+1; j<=i; j++) {
            var d=closes[j]-closes[j-1];
            if (d>0) g+=d; else l-=d;
        }
        var rsi = l===0 ? 100 : 100-(100/(1+g/period/(l/period)));
        out.push(parseFloat(rsi.toFixed(2)));
    }
    return out;
}

// ── Load & render ────────────────────────────────────────────────
function doLoad() {
    var u=document.getElementById('sel-ul').value;
    var ex=document.getElementById('sel-exp').value;
    var ceSt=document.getElementById('sel-ce').value;
    var peSt=document.getElementById('sel-pe').value;
    var errEl=document.getElementById('err');
    errEl.textContent='';
    if (!ex)   {errEl.textContent='Select expiry'; return;}
    if (!ceSt) {errEl.textContent='Select CE strike'; return;}
    if (!peSt) {errEl.textContent='Select PE strike'; return;}

    var ov=document.getElementById('overlay');
    ov.textContent='Loading 30 days...'; ov.style.display='block';

    fetch('/api/straddle/chart?underlying='+u+'&expiry='+ex+
          '&ce_strike='+ceSt+'&pe_strike='+peSt+'&tf='+_tf)
        .then(function(r){return r.json();})
        .then(function(d) {
            ov.style.display='none';
            if (d.error) {errEl.textContent=d.error; return;}
            var candles=d.candles||[];
            if (candles.length===0) {errEl.textContent='No candle data (market may be closed)'; return;}

            _sCandle.setData(candles);

            var closes=candles.map(function(c){return c.close;});
            var times =candles.map(function(c){return c.time;});

            // EMA — filter null warm-up bars
            var e20=calcEma(closes,20), e50=calcEma(closes,50);
            var ema20pts=[], ema50pts=[];
            for (var i=0;i<times.length;i++) {
                if (e20[i]!==null) ema20pts.push({time:times[i],value:e20[i]});
                if (e50[i]!==null) ema50pts.push({time:times[i],value:e50[i]});
            }
            _sEma20.setData(ema20pts);
            _sEma50.setData(ema50pts);

            // MACD — zero line spans full range to anchor time axis; filter null warm-up bars
            var md=calcMacd(closes,12,26,9);
            _sMacdZero.setData(times.map(function(t){return {time:t,value:0};}));
            var macdPts=[], sigPts=[];
            for (var i=0;i<times.length;i++) {
                if (md.macd[i]!==null) macdPts.push({time:times[i],value:md.macd[i]});
                if (md.signal[i]!==null) sigPts.push({time:times[i],value:md.signal[i]});
            }
            _sMacdSigBars.setData(sigPts);
            _sMacdBars.setData(macdPts);

            // RSI — OB/OS span ALL times so RSI chart's time axis matches main
            var rsiData=calcRsi(closes,14);
            var rsiPts=[];
            for (var i=0;i<times.length;i++) {
                if (rsiData[i]!==null) rsiPts.push({time:times[i],value:rsiData[i]});
            }
            // OB/OS use full times array — this aligns RSI chart's logical indices with main
            _sRsiOb.setData(times.map(function(t){return {time:t,value:70};}));
            _sRsiOs.setData(times.map(function(t){return {time:t,value:30};}));
            _sRsi.setData(rsiPts);

            _cMain.timeScale().fitContent();
            // Sync MACD and RSI to same range
            var r=_cMain.timeScale().getVisibleLogicalRange();
            if (r) { _cMacd.timeScale().setVisibleLogicalRange(r);
                     _cRsi.timeScale().setVisibleLogicalRange(r); }

            updateStats(candles, d.ce_ltp, d.pe_ltp);
            document.getElementById('statbar').style.display='flex';
            document.getElementById('lbl-strikes').textContent='CE '+ceSt+' / PE '+peSt;
            document.getElementById('ts').textContent='Updated '+new Date().toLocaleTimeString('en-IN');
            scheduleRefresh(u,ex,ceSt,peSt);
        })
        .catch(function(e){ov.textContent='Error: '+e;});
}

function updateStats(candles, ceLtp, peLtp) {
    var now=new Date();
    var istMs=now.getTime()+(5*60+30)*60*1000;
    var todayIST=new Date(istMs).toISOString().slice(0,10);
    var today=candles.filter(function(c){
        return new Date(c.time*1000+(5*60+30)*60*1000).toISOString().slice(0,10)===todayIST;
    });
    if (today.length===0) today=candles;
    var open=today[0].open;
    var high=Math.max.apply(null,today.map(function(c){return c.high;}));
    var low =Math.min.apply(null,today.map(function(c){return c.low;}));
    var close=today[today.length-1].close;
    var chg=close-open, pct=open>0?chg/open*100:0;
    document.getElementById('s-ltp').textContent =close.toFixed(2);
    document.getElementById('s-open').textContent=open.toFixed(2);
    document.getElementById('s-high').textContent=high.toFixed(2);
    document.getElementById('s-low').textContent =low.toFixed(2);
    var chgEl=document.getElementById('s-chg');
    chgEl.textContent=(chg>=0?'+':'')+chg.toFixed(2)+' ('+pct.toFixed(1)+'%)';
    chgEl.className='sv '+(chg>=0?'pos':'neg');
    if (ceLtp) document.getElementById('s-ce-val').textContent=ceLtp.toFixed(2);
    if (peLtp) document.getElementById('s-pe-val').textContent=peLtp.toFixed(2);
}

function scheduleRefresh(u,ex,ceSt,peSt) {
    if (_refreshTimer) clearTimeout(_refreshTimer);
    _refreshTimer=setTimeout(function(){
        var h=new Date().getUTCHours()*60+new Date().getUTCMinutes()+330;
        if (h%1440>=555 && h%1440<=930) doLoad();
        else scheduleRefresh(u,ex,ceSt,peSt);
    }, 60000);
}

// ── Bootstrap ────────────────────────────────────────────────────
window.onload = function() {
    initCharts();
    resizeAll();
    initDragHandles();
    onUlChange();
    window.addEventListener('resize', resizeAll);
    setInterval(pollSpot, 5000);
    
    // Intercept full-screen chart trading page
    if (window.location.pathname === '/chart-trading') {
        var header = document.querySelector('.header') || document.querySelector('header');
        if (header) header.style.display = 'none';
        var grid = document.querySelector('.grid');
        if (grid) grid.style.display = 'none';
        var desktopOnlies = document.querySelectorAll('.desktop-only');
        desktopOnlies.forEach(function(el) {
            if (!el.querySelector('#trading-workspace')) el.style.display = 'none';
        });
        var ocScroll = document.getElementById('oc-scroll-container');
        if (ocScroll) {
            ocScroll.style.maxHeight = 'none';
            ocScroll.style.height = 'calc(100vh - 100px)';
        }
        window._chartMaximized = true;
        var maxBtn = document.getElementById('dom-maximize-btn');
        if (maxBtn) {
            maxBtn.textContent = '🗗 Minimize';
            maxBtn.style.background = '#21262d';
        }
        var canvasDiv = document.getElementById('dom-chart-canvas');
        if (canvasDiv) {
            canvasDiv.style.height = 'calc(100vh - 180px)';
        }
        document.body.style.padding = '8px 0';
        switchDomTab('chart');
    }
};
</script>
</body>
</html>'''


@app.route("/journal")
def journal_page():
    # Redirect to trade-analyser running on port 5556
    host = request.host.split(":")[0]
    return redirect(f"http://{host}/analyser/", code=302)


@app.route("/mobile")
def mobile_page():
    return _build_mobile_page()


def _build_mobile_page():
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Risk Dashboard</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; padding-bottom: 80px; }
.header { background: #161b22; border-bottom: 1px solid #21262d; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 100; }
.header-pnl { font-size: 22px; font-weight: 700; }
.header-time { font-size: 11px; color: #484f58; }
.badge { font-size: 11px; padding: 3px 10px; border-radius: 12px; font-weight: 600; }
.badge-green { background: #1f2d1f; color: #3fb950; border: 1px solid #3fb950; }
.badge-red { background: #2d1117; color: #f85149; border: 1px solid #f85149; }
.badge-grey { background: #21262d; color: #484f58; border: 1px solid #30363d; }
.section { padding: 12px 16px; border-bottom: 1px solid #21262d; }
.section-title { font-size: 11px; color: #484f58; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; font-weight: 600; }
.pnl-row { display: flex; gap: 8px; margin-bottom: 8px; }
.pnl-box { flex: 1; background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 10px 12px; }
.pnl-label { font-size: 10px; color: #484f58; margin-bottom: 4px; }
.pnl-val { font-size: 18px; font-weight: 700; }
.pos-card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 12px; margin-bottom: 8px; }
.pos-name { font-size: 12px; color: #8b949e; margin-bottom: 6px; word-break: break-all; }
.pos-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.pos-qty { font-size: 13px; font-weight: 600; }
.pos-pnl { font-size: 16px; font-weight: 700; }
.exit-btns { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.exit-btn { flex: 1; min-width: 60px; padding: 10px 4px; border: 1px solid #30363d; border-radius: 6px; background: #21262d; color: #e6edf3; font-size: 12px; font-weight: 600; cursor: pointer; text-align: center; }
.exit-btn-full { background: #2d1117; border-color: #f85149; color: #f85149; }
.exit-btn:active { opacity: 0.7; }
.dd-bar-wrap { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 12px; }
.dd-bar-bg { background: #21262d; border-radius: 4px; height: 8px; margin: 8px 0; overflow: hidden; }
.dd-bar-fill { height: 100%; border-radius: 4px; transition: width 0.4s; }
.pending-card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 10px 12px; margin-bottom: 6px; font-size: 12px; }
.pending-label { color: #d29922; font-size: 10px; font-weight: 600; margin-bottom: 4px; }
.btn-cancel { padding: 8px 16px; background: none; border: 1px solid #484f58; color: #8b949e; border-radius: 6px; font-size: 12px; cursor: pointer; margin-top: 6px; }
.btn-exit-all { width: 100%; padding: 14px; background: #2d1117; border: 1px solid #f85149; color: #f85149; border-radius: 8px; font-size: 15px; font-weight: 700; cursor: pointer; margin-top: 4px; }
.btn-exit-all:active { background: #f85149; color: #0d1117; }
.eq-wrap { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 12px; }
.green { color: #3fb950; } .red { color: #f85149; } .grey { color: #484f58; } .amber { color: #d29922; }
.empty { color: #484f58; text-align: center; padding: 20px; font-size: 12px; }
.toast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%); background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 10px 20px; border-radius: 8px; font-size: 13px; z-index: 999; opacity: 0; transition: opacity 0.3s; pointer-events: none; white-space: nowrap; }
.toast.show { opacity: 1; }
.refresh-btn { position: fixed; bottom: 16px; right: 16px; width: 52px; height: 52px; border-radius: 50%; background: #21262d; border: 1px solid #30363d; color: #8b949e; font-size: 22px; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 100; }
.refresh-btn:active { background: #30363d; }
</style>
</head>
<body>

<div class="header">
    <div>
        <div class="header-pnl" id="m-total-pnl">₹0</div>
        <div class="header-time" id="m-time">--:--:--</div>
    </div>
    <span id="m-status-badge" class="badge badge-grey">...</span>
</div>

<!-- P&L Summary -->
<div class="section">
    <div class="section-title">P&amp;L</div>
    <div class="pnl-row">
        <div class="pnl-box">
            <div class="pnl-label">Realized</div>
            <div class="pnl-val" id="m-realized">₹0</div>
        </div>
        <div class="pnl-box">
            <div class="pnl-label">Unrealized</div>
            <div class="pnl-val" id="m-unrealized">₹0</div>
        </div>
    </div>
    <div class="pnl-row">
        <div class="pnl-box">
            <div class="pnl-label">Loss Remaining</div>
            <div class="pnl-val" id="m-loss-remain">--</div>
        </div>
        <div class="pnl-box">
            <div class="pnl-label">Trades</div>
            <div class="pnl-val" id="m-trades">0</div>
        </div>
    </div>
</div>

<!-- Trailing Drawdown -->
<div class="section">
    <div class="section-title">Trailing Drawdown</div>
    <div class="dd-bar-wrap">
        <div id="m-dd-inactive" style="color:#484f58;font-size:12px;">Activates when realized ≥ ₹10,000</div>
        <div id="m-dd-active" style="display:none;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                <span style="font-size:12px;color:#8b949e;">Gap to lockout</span>
                <span id="m-dd-gap" style="font-size:16px;font-weight:700;">--</span>
            </div>
            <div class="dd-bar-bg"><div id="m-dd-bar" class="dd-bar-fill" style="width:100%;background:#3fb950;"></div></div>
            <div style="display:flex;justify-content:space-between;font-size:10px;color:#484f58;margin-top:4px;">
                <span>HWM: <span id="m-dd-hwm">--</span></span>
                <span>Floor: <span id="m-dd-floor">--</span></span>
            </div>
        </div>
    </div>
</div>

<!-- Positions -->
<div class="section">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <div class="section-title" style="margin:0;">Positions <span id="m-pos-count" class="grey"></span></div>
        <button class="btn-exit-all" style="width:auto;padding:8px 16px;font-size:12px;" onclick="mExitAll()">EXIT ALL</button>
    </div>
    <div id="m-positions"><div class="empty">No open positions</div></div>
</div>

<!-- Pending Spreads -->
<div class="section">
    <div class="section-title">Pending Spreads</div>
    <div id="m-pending"><div class="empty">None</div></div>
</div>

<!-- Equity Curve -->
<div class="section">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <div class="section-title" style="margin:0;">Equity Curve</div>
        <span id="m-eq-summary" style="font-size:11px;color:#8b949e;"></span>
    </div>
    <div class="eq-wrap" style="position:relative;height:160px;">
        <canvas id="m-eq-chart"></canvas>
        <div id="m-eq-empty" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;" class="empty">No closed trades today</div>
    </div>
</div>

<div id="toast" class="toast"></div>
<button class="refresh-btn" onclick="fetchStatus()" title="Refresh">↻</button>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
var _eqChart = null;

function fmt(v) {
    var s = Math.abs(v).toLocaleString("en-IN", {maximumFractionDigits: 0});
    return (v < 0 ? "-" : "+") + "\\u20B9" + s;
}
function fmtAbs(v) {
    return "\\u20B9" + Math.abs(v).toLocaleString("en-IN", {maximumFractionDigits: 0});
}
function colorVal(v) { return v > 0 ? "green" : v < 0 ? "red" : "grey"; }

function showToast(msg, cls) {
    var t = document.getElementById("toast");
    t.textContent = msg;
    t.className = "toast show" + (cls ? " " + cls : "");
    setTimeout(function() { t.className = "toast"; }, 3000);
}

function fetchStatus() {
    fetch("/api/status")
        .then(function(r) { return r.json(); })
        .then(renderStatus)
        .catch(function(e) { console.warn(e); });
}

function renderStatus(d) {
    document.getElementById("m-time").textContent = new Date().toLocaleTimeString("en-IN");

    // Status badge
    var badge = document.getElementById("m-status-badge");
    if (d.lockout && d.lockout.active) {
        badge.className = "badge badge-red"; badge.textContent = "LOCKED";
    } else if (d.can_trade) {
        badge.className = "badge badge-green"; badge.textContent = "CAN TRADE";
    } else {
        badge.className = "badge badge-red"; badge.textContent = "BLOCKED";
    }

    // P&L
    var pnl = d.pnl || {};
    var total = pnl.total || 0, realized = pnl.realized || 0, unrealized = pnl.unrealized || 0;
    var totalEl = document.getElementById("m-total-pnl");
    totalEl.textContent = fmt(total);
    totalEl.className = "header-pnl " + colorVal(total);
    var rEl = document.getElementById("m-realized");
    rEl.textContent = fmt(realized); rEl.className = "pnl-val " + colorVal(realized);
    var uEl = document.getElementById("m-unrealized");
    uEl.textContent = fmt(unrealized); uEl.className = "pnl-val " + colorVal(unrealized);

    var limits = d.limits || {};
    var lrEl = document.getElementById("m-loss-remain");
    lrEl.textContent = fmtAbs(limits.loss_remaining || 0);
    lrEl.className = "pnl-val " + ((limits.loss_remaining || 0) < 5000 ? "red" : "green");

    var trades = d.trades || {};
    document.getElementById("m-trades").textContent =
        (trades.winners || 0) + "W / " + (trades.losers || 0) + "L";

    // Trailing drawdown
    var dd = d.trailing_drawdown || {};
    if (dd.enabled && dd.high_water_mark > 0) {
        document.getElementById("m-dd-inactive").style.display = "none";
        document.getElementById("m-dd-active").style.display = "block";
        var hwm = dd.high_water_mark, floor = hwm - dd.drawdown_limit, gap = dd.buffer || 0;
        var pct = dd.drawdown_limit > 0 ? Math.max(0, Math.min(100, (gap / dd.drawdown_limit) * 100)) : 100;
        var gapEl = document.getElementById("m-dd-gap");
        gapEl.textContent = fmt(gap);
        gapEl.className = gap < dd.drawdown_limit * 0.25 ? "red" : gap < dd.drawdown_limit * 0.6 ? "amber" : "green";
        var bar = document.getElementById("m-dd-bar");
        bar.style.width = pct + "%";
        bar.style.background = pct < 25 ? "#f85149" : pct < 60 ? "#d29922" : "#3fb950";
        document.getElementById("m-dd-hwm").textContent = fmtAbs(hwm);
        document.getElementById("m-dd-floor").textContent = fmtAbs(floor > 0 ? floor : 0);
    } else {
        document.getElementById("m-dd-inactive").style.display = "block";
        document.getElementById("m-dd-active").style.display = "none";
    }

    // Positions
    renderPositions(d.positions || [], d.sl_tp_orders || {});

    // Pending
    renderPending(d.pending_orders || []);
}

function renderPositions(positions, slOrders) {
    var el = document.getElementById("m-positions");
    var open = positions.filter(function(p) { return (p.netQty || 0) !== 0; });
    document.getElementById("m-pos-count").textContent = open.length ? "(" + open.length + ")" : "";
    if (!open.length) { el.innerHTML = \'<div class="empty">No open positions</div>\'; return; }
    el.innerHTML = open.map(function(p) {
        var sid = p.securityId || p.security_id || "";
        var name = p.tradingSymbol || p.symbol || sid;
        var qty = Math.abs(p.netQty || 0);
        var upnl = p.unrealizedProfit || 0;
        var side = (p.netQty || 0) > 0 ? "LONG" : "SHORT";
        var sideColor = side === "SHORT" ? "red" : "green";
        var pnlColor = upnl >= 0 ? "green" : "red";
        var exSeg = p.exchangeSegment || "NSE_FNO";
        var prod = p.productType || "MARGIN";
        return \'<div class="pos-card">\' +
            \'<div class="pos-name">\' + name + \'</div>\' +
            \'<div class="pos-row">\' +
                \'<span class="pos-qty \' + sideColor + \'">\' + side + \' \' + qty + \'</span>\' +
                \'<span class="pos-pnl \' + pnlColor + \'">\' + fmt(upnl) + \'</span>\' +
            \'</div>\' +
            \'<div class="exit-btns">\' +
                \'<button class="exit-btn" onclick="mExit(\\\'\' + sid + \'\\\',\\\'\' + exSeg + \'\\\',\\\'\' + prod + \'\\\',\\\'\' + side + \'\\\',\' + Math.round(qty*0.5) + \',\' + qty + \')">50%</button>\' +
                \'<button class="exit-btn exit-btn-full" onclick="mExit(\\\'\' + sid + \'\\\',\\\'\' + exSeg + \'\\\',\\\'\' + prod + \'\\\',\\\'\' + side + \'\\\',\' + qty + \',\' + qty + \')">EXIT ALL</button>\' +
            \'</div>\' +
        \'</div>\';
    }).join("");
}

function renderPending(orders) {
    var el = document.getElementById("m-pending");
    if (!orders.length) { el.innerHTML = \'<div class="empty">None</div>\'; return; }
    el.innerHTML = orders.map(function(o) {
        var sid = o.spread_id || o.id || "";
        return \'<div class="pending-card">\' +
            \'<div class="pending-label">PENDING SPREAD</div>\' +
            (o.sell_symbol || o.sell_security_id || "") + " → " + (o.buy_symbol || o.buy_security_id || "") +
            "<br>Trigger ≤ ₹" + (o.sell_trigger_price || "--") + "  Qty: " + (o.quantity || "--") +
            \'<br><button class="btn-cancel" onclick="mCancelPending(\\\'\' + sid + \'\\\')">Cancel</button>\' +
        \'</div>\';
    }).join("");
}

function mExit(sid, exSeg, prod, side, qty, fullQty) {
    var dir = side === "SHORT" ? "BUY" : "SELL";
    if (!confirm("Exit " + qty + " @ MARKET?")) return;
    fetch("/api/order/place", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            security_id: sid, exchange_segment: exSeg, product_type: prod,
            transaction_type: dir, quantity: qty, order_type: "MARKET", price: 0
        })
    }).then(function(r) { return r.json(); })
      .then(function(d) {
          showToast(d.status === "ok" ? "Exit placed" : (d.error || "Error"));
          setTimeout(fetchStatus, 2000);
      });
}

function mExitAll() {
    if (!confirm("Exit ALL positions at MARKET?")) return;
    fetch("/api/order/exit_all", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" })
        .then(function(r) { return r.json(); })
        .then(function(d) { showToast("Exit all sent"); setTimeout(fetchStatus, 2000); });
}

function mCancelPending(sid) {
    fetch("/api/order/cancel_spread/" + sid, { method: "POST" })
        .then(function() { showToast("Cancelled"); fetchStatus(); });
}

// Equity curve
function initMobileChart() {
    var c = document.getElementById("m-eq-chart");
    if (!c || typeof Chart === "undefined") return;
    _eqChart = new Chart(c, {
        type: "line",
        data: {
            labels: [],
            datasets: [{
                data: [], borderColor: "#58a6ff", borderWidth: 2, fill: false,
                tension: 0.3, pointRadius: 4, pointBackgroundColor: [], pointBorderColor: []
            }, {
                data: [], borderColor: "#f85149", borderWidth: 1,
                borderDash: [6,3], fill: false, pointRadius: 0, tension: 0
            }]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: "#484f58", font: { size: 9 }, maxTicksLimit: 8 }, grid: { color: "#161b22" } },
                y: { ticks: { color: "#8b949e", font: { size: 9 }, callback: function(v) { return "\\u20B9" + v.toLocaleString("en-IN"); } }, grid: { color: "#21262d" } }
            },
            animation: { duration: 0 }
        }
    });
    fetchEquity();
}

function fetchEquity() {
    fetch("/api/equity_curve")
        .then(function(r) { return r.json(); })
        .then(function(d) {
            var trades = d.trades || [], floor = d.floor || 0;
            var empty = document.getElementById("m-eq-empty");
            var sumEl = document.getElementById("m-eq-summary");
            if (!_eqChart) return;
            if (!trades.length) { empty.style.display = "flex"; _eqChart.data.labels = []; _eqChart.data.datasets[0].data = []; _eqChart.update("none"); return; }
            empty.style.display = "none";
            var labels = [], vals = [], colors = [], borders = [], floorLine = [];
            var cumul = 0, wins = 0, losses = 0;
            trades.forEach(function(t) {
                cumul += t.pnl || 0;
                labels.push((t.time || "").substring(11, 16));
                vals.push(Math.round(cumul));
                var w = (t.pnl || 0) >= 0; if (w) wins++; else losses++;
                colors.push(w ? "#3fb950" : "#f85149");
                borders.push(w ? "#3fb950" : "#f85149");
                floorLine.push(floor > 0 ? Math.round(floor) : null);
            });
            _eqChart.data.labels = labels;
            _eqChart.data.datasets[0].data = vals;
            _eqChart.data.datasets[0].pointBackgroundColor = colors;
            _eqChart.data.datasets[0].pointBorderColor = borders;
            _eqChart.data.datasets[1].data = floor > 0 ? floorLine : [];
            _eqChart.update("none");
            var total = wins + losses;
            sumEl.textContent = wins + "W / " + losses + "L (" + (total ? Math.round(wins/total*100) : 0) + "%)";
            sumEl.style.color = cumul >= 0 ? "#3fb950" : "#f85149";
        });
}

// Init
if (typeof Chart !== "undefined") { initMobileChart(); } else {
    window.addEventListener("load", function() { if (typeof Chart !== "undefined") initMobileChart(); });
}
fetchStatus();
setInterval(fetchStatus, 3000);
setInterval(fetchEquity, 60000);
</script>
</body>
</html>'''


@app.route("/analytics")
def analytics_page():
    return _build_analytics_page()


def _build_analytics_page():
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Analytics</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;}
.header{background:#161b22;border-bottom:1px solid #21262d;padding:14px 24px;display:flex;align-items:center;gap:16px;}
.header h1{font-size:16px;font-weight:700;color:#e6edf3;}
.header a{font-size:12px;color:#8b949e;text-decoration:none;padding:4px 10px;border:1px solid #30363d;border-radius:6px;}
.header a:hover{color:#e6edf3;}
.content{padding:16px;max-width:1200px;margin:0 auto;}
/* Stats cards */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:28px;}
.stat-card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:14px 16px;text-align:center;}
.stat-label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;}
.stat-val{font-size:20px;font-weight:700;}
.positive{color:#3fb950;}.negative{color:#f85149;}.neutral{color:#e6edf3;}
/* Calendar */
.cal-nav{display:flex;align-items:center;gap:12px;margin-bottom:16px;}
.cal-nav button{background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:14px;}
.cal-nav button:hover{background:#21262d;}
.cal-month-label{font-size:16px;font-weight:600;color:#e6edf3;min-width:140px;text-align:center;}
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:28px;}
.cal-dow{text-align:center;font-size:11px;color:#8b949e;padding:6px 0;font-weight:600;}
.cal-cell{background:#161b22;border:1px solid #21262d;border-radius:6px;min-height:60px;padding:6px;cursor:default;position:relative;transition:border-color .15s;}
.cal-cell.has-data{cursor:pointer;}
.cal-cell.has-data:hover{border-color:#58a6ff;}
.cal-cell.selected{border-color:#58a6ff;background:#0d2137;}
.cal-cell.today-cell{border-color:#30363d;}
.cal-cell.profit{border-left:3px solid #3fb950;}
.cal-cell.loss{border-left:3px solid #f85149;}
.cal-cell.empty{background:transparent;border-color:transparent;}
.cal-day-num{font-size:10px;color:#8b949e;margin-bottom:2px;}
.cal-day-pnl{font-size:11px;font-weight:700;}
.cal-day-trades{font-size:9px;color:#8b949e;margin-top:2px;}
/* Day detail */
#day-detail{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:28px;display:none;}
#day-detail h2{font-size:14px;font-weight:700;margin-bottom:16px;color:#58a6ff;}
.day-stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;margin-bottom:18px;}
.day-stat{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 12px;text-align:center;}
.day-stat .lbl{font-size:10px;color:#8b949e;text-transform:uppercase;}
.day-stat .val{font-size:15px;font-weight:700;margin-top:3px;}
table{width:100%;border-collapse:collapse;font-size:12px;}
th{color:#8b949e;text-align:left;padding:8px 6px;border-bottom:1px solid #21262d;font-weight:600;font-size:11px;text-transform:uppercase;}
td{padding:8px 6px;border-bottom:1px solid #161b22;color:#e6edf3;}
tr:hover td{background:#1c2128;}
/* Thrash / scatter sections */
.section-hdr{font-size:12px;font-weight:700;margin-bottom:10px;margin-top:18px;padding-bottom:6px;border-bottom:1px solid #21262d;}
.thrash-hdr{color:#f0883e;}.scatter-hdr{color:#58a6ff;}.bounce-hdr{color:#f0883e;font-size:11px;font-weight:700;margin:12px 0 6px;}
.legend-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:3px;}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700;}
.tag-ce{background:#0d2137;color:#58a6ff;border:1px solid #1f4f7a;}
.tag-pe{background:#2d1a00;color:#e3b341;border:1px solid #7d4e00;}
/* Mobile */
@media(max-width:600px){
  .content{padding:10px;}
  .stat-grid{grid-template-columns:repeat(2,1fr);gap:8px;}
  .stat-card{padding:10px;}
  .stat-val{font-size:16px;}
  .cal-cell{min-height:48px;padding:4px;}
  .cal-day-pnl{font-size:10px;}
  .day-stat-grid{grid-template-columns:repeat(2,1fr);}
  table{font-size:11px;}
  th,td{padding:6px 4px;}
  .header{padding:10px 14px;}
}
</style>
</head>
<body>
<div class="header">
  <h1>&#x1F4CA; Analytics</h1>
  <a href="/" title="Back to dashboard">&#x2190; Dashboard</a>
</div>
<div class="content">

  <!-- Year toggle + Overall stats -->
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
    <button onclick="yearNav(-1)" style="background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:14px;">&#x2039;</button>
    <span id="year-label" style="font-size:16px;font-weight:700;min-width:60px;text-align:center;"></span>
    <button onclick="yearNav(1)" style="background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:14px;">&#x203A;</button>
  </div>
  <div id="overall-stats" class="stat-grid">
    <div style="grid-column:1/-1;text-align:center;color:#8b949e;padding:20px;">Loading...</div>
  </div>

  <!-- Calendar nav -->
  <div class="cal-nav">
    <button onclick="calPrev()">&#x2039;</button>
    <span id="cal-month-label" class="cal-month-label"></span>
    <button onclick="calNext()">&#x203A;</button>
  </div>
  <div id="cal-grid" class="cal-grid"></div>

  <!-- Day detail panel -->
  <div id="day-detail">
    <h2 id="day-detail-title"></h2>
    <div id="day-detail-stats" class="day-stat-grid"></div>
    <div id="day-detail-trades"></div>

    <!-- Thrash sessions -->
    <div id="thrash-section" style="display:none;">
      <div class="section-hdr thrash-hdr">&#x26A1; Thrash Sessions <span id="thrash-badge" style="font-size:10px;font-weight:400;color:#8b949e;margin-left:6px;"></span></div>
      <div id="thrash-content"></div>
    </div>

    <!-- Trade clusters -->
    <div id="cluster-section" style="display:none;">
      <div class="section-hdr" style="color:#a371f7;">&#x29D6; Trade Clusters <span id="cluster-badge" style="font-size:10px;font-weight:400;color:#8b949e;margin-left:6px;"></span></div>
      <div id="cluster-content"></div>
    </div>

    <!-- Day equity curve -->
    <div id="day-eq-section" style="display:none;margin-top:18px;">
      <div class="section-hdr" style="color:#58a6ff;display:flex;align-items:center;justify-content:space-between;">
        <span>&#x1F4C8; Equity Curve</span>
        <div style="display:flex;align-items:center;gap:10px;">
          <span style="font-size:9px;color:#484f58;font-weight:400;">scroll=zoom &middot; drag=pan &middot; dblclick=reset</span>
          <button onclick="openEqModal()" style="background:none;border:1px solid #30363d;color:#8b949e;font-size:10px;padding:2px 8px;border-radius:4px;cursor:pointer;">&#x26F6; Expand</button>
        </div>
      </div>
      <div style="position:relative;height:200px;margin-top:10px;">
        <canvas id="day-eq-chart"></canvas>
      </div>
    </div>
  </div>

  <!-- Equity curve expand modal -->
  <div id="eq-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:1000;align-items:center;justify-content:center;"
       onclick="if(event.target===this)closeEqModal()">
    <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;width:92vw;max-width:1100px;padding:20px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <h3 id="eq-modal-title" style="font-size:14px;font-weight:700;color:#58a6ff;">&#x1F4C8; Equity Curve</h3>
        <div style="display:flex;align-items:center;gap:12px;">
          <span style="font-size:9px;color:#484f58;">scroll=zoom &middot; drag=pan &middot; dblclick=reset</span>
          <button onclick="closeEqModal()" style="background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer;line-height:1;">&times;</button>
        </div>
      </div>
      <div style="position:relative;height:62vh;">
        <canvas id="day-eq-chart-full"></canvas>
      </div>
    </div>
  </div>

</div>
<script>
var _byDate = {};       // date string -> summary object
var _calYear = 0;
var _calMonth = 0;      // 0-indexed
var _selectedDate = null;
var _todayStr = '';
var _statsYear = 0;     // FY start year e.g. 2025 means FY 2025-26 (Apr 2025 – Mar 2026)

function fmt(v) {
    if (v == null) return '-';
    var s = Math.abs(v).toLocaleString('en-IN', {minimumFractionDigits:0, maximumFractionDigits:0});
    return (v >= 0 ? '+₹' : '-₹') + s;
}
function fmtPct(v) { return v == null ? '-' : v.toFixed(1) + '%'; }

// ── Boot ────────────────────────────────────────────────────────────
window.onload = function() {
    var now = new Date();
    _calYear  = now.getFullYear();
    _calMonth = now.getMonth();
    // FY start year: if current month >= April (3), FY start = this year, else last year
    _statsYear = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1;
    _todayStr  = now.getFullYear() + '-' +
                 String(now.getMonth()+1).padStart(2,'0') + '-' +
                 String(now.getDate()).padStart(2,'0');
    document.getElementById('year-label').textContent = fyLabel(_statsYear);
    loadAll();
};

function fyLabel(y) { return 'FY ' + y + '-' + String(y+1).slice(2); }

function yearNav(dir) {
    _statsYear += dir;
    document.getElementById('year-label').textContent = fyLabel(_statsYear);
    renderOverallStats(Object.values(_byDate));
}

function fetchWithTimeout(url, ms) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function(){ ctrl.abort(); }, ms) : null;
    var opts = ctrl ? {signal: ctrl.signal} : {};
    return fetch(url, opts).finally(function(){ if(timer) clearTimeout(timer); });
}

function buildSummary(date, trades) {
    var pnls = trades.map(function(t){ return t.pnl || 0; });
    var winners = pnls.filter(function(p){ return p >= 0; });
    var losers  = pnls.filter(function(p){ return p <  0; });
    return {
        date: date,
        total_trades:  trades.length,
        winners:       winners.length,
        losers:        losers.length,
        total_pnl:     pnls.reduce(function(a,b){return a+b;}, 0),
        gross_profit:  winners.reduce(function(a,b){return a+b;}, 0),
        gross_loss:    losers.reduce(function(a,b){return a+b;},  0),
        win_rate:      trades.length ? winners.length/trades.length*100 : 0,
        avg_win:       winners.length ? winners.reduce(function(a,b){return a+b;},0)/winners.length : 0,
        avg_loss:      losers.length  ? losers.reduce(function(a,b){return a+b;},0)/losers.length   : 0,
    };
}

// Silent background refresh — re-fetches data without showing "Loading..."
setInterval(function() { loadAll(true); }, 120000);

function loadAll(silent) {
    var statsEl = document.getElementById('overall-stats');
    if (!silent) statsEl.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#8b949e;padding:20px;">Loading...</div>';

    fetchWithTimeout('/api/analyser/dates', 5000)
        .then(function(r){ return r.json(); })
        .then(function(dates) {
            if (!dates || dates.length === 0) {
                statsEl.innerHTML = '<div style="grid-column:1/-1;color:#8b949e;padding:20px;text-align:center;">No trading data found.</div>';
                renderCalendar();
                return Promise.resolve([]);
            }
            var promises = dates.map(function(d) {
                return fetchWithTimeout('/api/analytics/day_trades?date='+d, 5000)
                    .then(function(r){ return r.json(); })
                    .then(function(trades){ return buildSummary(d, Array.isArray(trades) ? trades : []); })
                    .catch(function(){ return buildSummary(d, []); });
            });
            return Promise.all(promises);
        })
        .then(function(summaries) {
            summaries = (summaries || []).filter(function(s){ return s.total_trades > 0; });
            _byDate = {};
            summaries.forEach(function(s){ _byDate[s.date] = s; });
            renderOverallStats(Object.values(_byDate));
            renderCalendar();
        })
        .catch(function(e) {
            statsEl.innerHTML = '<div style="grid-column:1/-1;color:#f85149;padding:20px;text-align:center;">Error: ' + e + '</div>';
            renderCalendar();
        });
}

// ── Overall stats ───────────────────────────────────────────────────
function renderOverallStats(data) {
    var el = document.getElementById('overall-stats');
    // Filter to selected FY (Apr _statsYear – Mar _statsYear+1)
    var fyStart = String(_statsYear) + '-04-01';
    var fyEnd   = String(_statsYear+1) + '-03-31';
    data = (data || []).filter(function(d) { return d.date && d.date >= fyStart && d.date <= fyEnd; });
    if (!data || data.length === 0) {
        el.innerHTML = '<div class="stat-card" style="grid-column:1/-1;text-align:center;"><div class="stat-label">No data for ' + fyLabel(_statsYear) + '</div></div>';
        return;
    }
    var totalTrades = 0, totalPnl = 0, winners = 0, losers = 0;
    var profDays = 0, lossDays = 0, allPnls = [];
    for (var i = 0; i < data.length; i++) {
        var d = data[i];
        totalTrades += d.total_trades || 0;
        totalPnl    += d.total_pnl || 0;
        winners     += d.winners || 0;
        losers      += d.losers || 0;
        allPnls.push(d.total_pnl);
        if (d.total_pnl >= 0) profDays++; else lossDays++;
    }
    var winRate  = totalTrades > 0 ? (winners / totalTrades * 100) : 0;
    var dayWinRate = data.length > 0 ? (profDays / data.length * 100) : 0;
    var avgDaily = data.length > 0 ? totalPnl / data.length : 0;
    var bestDay  = data.reduce(function(a,b){ return b.total_pnl > a.total_pnl ? b : a; });
    var worstDay = data.reduce(function(a,b){ return b.total_pnl < a.total_pnl ? b : a; });

    var cards = [
        {lbl:'Total Days', val: data.length, cls:'neutral'},
        {lbl:'Total Trades', val: totalTrades, cls:'neutral'},
        {lbl:'Overall P&L', val: fmt(totalPnl), cls: totalPnl>=0?'positive':'negative'},
        {lbl:'Trade Win Rate', val: fmtPct(winRate), cls: winRate>=50?'positive':'negative'},
        {lbl:'Day Win Rate', val: fmtPct(dayWinRate), cls: dayWinRate>=50?'positive':'negative'},
        {lbl:'Avg Daily P&L', val: fmt(avgDaily), cls: avgDaily>=0?'positive':'negative'},
        {lbl:'Profitable Days', val: profDays, cls:'positive'},
        {lbl:'Losing Days', val: lossDays, cls:'negative'},
        {lbl:'Best Day', val: fmt(bestDay.total_pnl) + '<br><span style="font-size:10px;color:#8b949e;">'+bestDay.date+'</span>', cls:'positive'},
        {lbl:'Worst Day', val: fmt(worstDay.total_pnl) + '<br><span style="font-size:10px;color:#8b949e;">'+worstDay.date+'</span>', cls:'negative'},
    ];
    var html = '';
    for (var c = 0; c < cards.length; c++) {
        html += '<div class="stat-card"><div class="stat-label">'+cards[c].lbl+'</div>';
        html += '<div class="stat-val '+cards[c].cls+'">'+cards[c].val+'</div></div>';
    }
    el.innerHTML = html;
}

// ── Calendar ────────────────────────────────────────────────────────
var MONTHS = ['January','February','March','April','May','June',
              'July','August','September','October','November','December'];
var DAYS   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

function calPrev() {
    _calMonth--;
    if (_calMonth < 0) { _calMonth = 11; _calYear--; }
    renderCalendar();
}
function calNext() {
    _calMonth++;
    if (_calMonth > 11) { _calMonth = 0; _calYear++; }
    renderCalendar();
}

function renderCalendar() {
    document.getElementById('cal-month-label').textContent = MONTHS[_calMonth] + ' ' + _calYear;
    var grid = document.getElementById('cal-grid');
    var html = '';
    // Day-of-week headers
    for (var d = 0; d < 7; d++) html += '<div class="cal-dow">'+DAYS[d]+'</div>';

    var first = new Date(_calYear, _calMonth, 1).getDay(); // 0=Sun
    var daysInMonth = new Date(_calYear, _calMonth+1, 0).getDate();

    // Empty leading cells
    for (var e = 0; e < first; e++) html += '<div class="cal-cell empty"></div>';

    for (var day = 1; day <= daysInMonth; day++) {
        var ds = _calYear + '-' +
                 String(_calMonth+1).padStart(2,'0') + '-' +
                 String(day).padStart(2,'0');
        var summary = _byDate[ds];
        var isToday = ds === _todayStr;
        var isSelected = ds === _selectedDate;
        var hasPnl = summary != null;
        var pnlClass = hasPnl ? (summary.total_pnl >= 0 ? 'profit' : 'loss') : '';
        var selectedClass = isSelected ? ' selected' : '';
        var todayClass = isToday ? ' today-cell' : '';
        var hasClass = hasPnl ? ' has-data' : '';
        html += '<div class="cal-cell'+pnlClass+selectedClass+todayClass+hasClass+'"' +
                (hasPnl ? ' onclick="selectDay(this.dataset.d)" data-d="'+ds+'"' : '') + '>';
        html += '<div class="cal-day-num">' + day + (isToday ? ' &#x2022;' : '') + '</div>';
        if (hasPnl) {
            var pnl = summary.total_pnl;
            html += '<div class="cal-day-pnl '+(pnl>=0?'positive':'negative')+'">'+fmt(pnl)+'</div>';
            html += '<div class="cal-day-trades">'+summary.total_trades+' trade'+(summary.total_trades!==1?'s':'')+'</div>';
        }
        html += '</div>';
    }
    grid.innerHTML = html;
}

// ── Day detail ──────────────────────────────────────────────────────
function selectDay(date) {
    _selectedDate = date;
    renderCalendar();
    var s = _byDate[date];
    var detail = document.getElementById('day-detail');
    detail.style.display = 'block';
    document.getElementById('day-detail-title').textContent = date;

    // Summary stats
    var stats = [
        {lbl:'Total P&L', val: fmt(s.total_pnl), cls: s.total_pnl>=0?'positive':'negative'},
        {lbl:'Trades', val: s.total_trades, cls:'neutral'},
        {lbl:'Win Rate', val: fmtPct(s.win_rate), cls: s.win_rate>=50?'positive':'negative'},
        {lbl:'Winners', val: s.winners, cls:'positive'},
        {lbl:'Losers', val: s.losers, cls:'negative'},
        {lbl:'Gross Profit', val: fmt(s.gross_profit), cls:'positive'},
        {lbl:'Gross Loss', val: fmt(s.gross_loss), cls:'negative'},
        {lbl:'Avg Win', val: fmt(s.avg_win), cls:'positive'},
        {lbl:'Avg Loss', val: fmt(s.avg_loss), cls:'negative'},
    ];
    var sh = '';
    for (var i = 0; i < stats.length; i++) {
        sh += '<div class="day-stat"><div class="lbl">'+stats[i].lbl+'</div>';
        sh += '<div class="val '+stats[i].cls+'">'+stats[i].val+'</div></div>';
    }
    document.getElementById('day-detail-stats').innerHTML = sh;

    // Fetch trade list from analyser
    document.getElementById('day-detail-trades').innerHTML =
        '<div class="spinner">Loading trades...</div>';
    fetchWithTimeout('/api/analytics/day_trades?date='+date, 5000)
        .then(function(r){return r.json();})
        .then(function(trades){renderDayTrades(trades);})
        .catch(function(){
            document.getElementById('day-detail-trades').innerHTML =
                '<div style="color:#f85149;padding:12px;">Could not load trades for this day.</div>';
        });

    detail.scrollIntoView({behavior:'smooth', block:'nearest'});
}

var _allDayTrades = [];

function filterDayTrades(tab) {
    document.querySelectorAll('.trade-type-tab').forEach(function(b){
        b.style.borderBottom = '2px solid transparent';
        b.style.color = '#8b949e';
        b.style.fontWeight = '400';
    });
    var active = document.getElementById('tab-'+tab);
    if (active) { active.style.borderBottom = '2px solid #3fb950'; active.style.color = '#e6edf3'; active.style.fontWeight = '700'; }
    var filtered = tab === 'all' ? _allDayTrades :
                   _allDayTrades.filter(function(t){ return (t.option_type||'').toUpperCase() === tab.toUpperCase(); });
    document.getElementById('trade-table-body').innerHTML = tradeTable(filtered);
}

function renderDayTrades(trades) {
    var el = document.getElementById('day-detail-trades');
    if (!trades || trades.length === 0) {
        el.innerHTML = '<div style="color:#484f58;padding:12px;text-align:center;">No trade detail available.</div>';
        document.getElementById('thrash-section').style.display = 'none';
        document.getElementById('cluster-section').style.display = 'none';
        return;
    }
    var sorted = trades.slice().sort(function(a,b){ return (a.entry_time||'').localeCompare(b.entry_time||''); });
    _allDayTrades = sorted;
    // Inject tabs above table
    var ceCount = sorted.filter(function(t){ return (t.option_type||'').toUpperCase()==='CE'; }).length;
    var peCount = sorted.filter(function(t){ return (t.option_type||'').toUpperCase()==='PE'; }).length;
    var tabBar = '<div id="trade-tab-bar" style="display:flex;gap:4px;margin-bottom:8px;border-bottom:1px solid #21262d;">'
        + '<button id="tab-all" class="trade-type-tab" onclick="filterDayTrades(&quot;all&quot;)" style="background:none;border:none;padding:6px 12px;cursor:pointer;font-size:12px;border-bottom:2px solid #3fb950;color:#e6edf3;font-weight:700;">All ('+sorted.length+')</button>'
        + '<button id="tab-CE" class="trade-type-tab" onclick="filterDayTrades(&quot;CE&quot;)" style="background:none;border:none;padding:6px 12px;cursor:pointer;font-size:12px;border-bottom:2px solid transparent;color:#8b949e;">CE ('+ceCount+')</button>'
        + '<button id="tab-PE" class="trade-type-tab" onclick="filterDayTrades(&quot;PE&quot;)" style="background:none;border:none;padding:6px 12px;cursor:pointer;font-size:12px;border-bottom:2px solid transparent;color:#8b949e;">PE ('+peCount+')</button>'
        + '</div>'
        + '<div id="trade-table-body"></div>';
    el.innerHTML = tabBar;
    document.getElementById('trade-table-body').innerHTML = tradeTable(sorted);
    renderThrashSessions(sorted);
    renderClusters(sorted);
    renderDayEquityCurve(sorted);
}

// ── Helpers ──────────────────────────────────────────────────────────

function timeToMin(t) {
    if (!t) return 0;
    var p = t.split(':');
    return parseInt(p[0]||0)*60 + parseInt(p[1]||0) + parseInt(p[2]||0)/60;
}

function tradeTable(trades) {
    if (!trades || trades.length === 0) return '<div style="color:#484f58;font-size:11px;padding:8px;">No trades.</div>';
    var html = '<table style="margin-top:6px;"><thead><tr><th>Entry</th><th>Exit</th><th>Instrument</th><th>Dir</th><th>Entry &#8377;</th><th>Exit &#8377;</th><th>Qty</th><th>P&L</th></tr></thead><tbody>';
    trades.forEach(function(t) {
        var pnl = t.pnl != null ? t.pnl : null;
        var cls = pnl == null ? '' : (pnl >= 0 ? 'positive' : 'negative');
        var otype = (t.option_type||'').toUpperCase();
        var tag = otype ? '<span class="tag '+(otype==='CE'?'tag-ce':'tag-pe')+'">'+otype+'</span> ' : '';
        var dir = (t.direction||'').toUpperCase();
        var dirHtml = dir === 'SHORT'
            ? '<span style="color:#f85149;font-size:10px;font-weight:700;">SHORT</span>'
            : dir === 'LONG'
                ? '<span style="color:#3fb950;font-size:10px;font-weight:700;">LONG</span>'
                : '<span style="color:#484f58;font-size:10px;">-</span>';
        html += '<tr>';
        html += '<td style="white-space:nowrap;">'+(t.entry_time||'-')+'</td>';
        html += '<td style="white-space:nowrap;color:#8b949e;">'+(t.exit_time||'-')+'</td>';
        html += '<td>'+tag+(t.strike||t.symbol||t.underlying||'-')+'</td>';
        html += '<td>'+dirHtml+'</td>';
        html += '<td>&#8377;'+(t.entry_price!=null?t.entry_price.toFixed(2):'-')+'</td>';
        html += '<td>&#8377;'+(t.exit_price!=null?t.exit_price.toFixed(2):'-')+'</td>';
        html += '<td>'+(t.quantity||'-')+'</td>';
        html += '<td class="'+cls+'" style="font-weight:600;">'+(pnl!=null?fmt(pnl):'-')+'</td>';
        html += '</tr>';
    });
    return html + '</tbody></table>';
}

// ── Thrash sessions (same instrument, adaptive window) ───────────────

function adaptiveThrashWindow(trades) {
    // Compute gaps between consecutive same-symbol trades, use median * 1.5 as threshold
    var gaps = [];
    var bySymbol = {};
    trades.forEach(function(t) {
        var sym = (t.underlying||'')+'|'+(t.strike||'')+'|'+(t.option_type||'');
        if (!bySymbol[sym]) bySymbol[sym] = [];
        bySymbol[sym].push(timeToMin(t.entry_time));
    });
    Object.keys(bySymbol).forEach(function(sym) {
        var times = bySymbol[sym].sort(function(a,b){return a-b;});
        for (var i = 1; i < times.length; i++) gaps.push(times[i]-times[i-1]);
    });
    if (gaps.length === 0) return 5;
    // Exclude session-level breaks before computing threshold
    var sessionGaps = gaps.filter(function(g){ return g >= 0 && g <= 30; });
    if (sessionGaps.length === 0) return 5;
    sessionGaps.sort(function(a,b){return a-b;});
    var median = sessionGaps[Math.floor(sessionGaps.length/2)];
    // Thrash = revenge re-entry: keep window tight (max 5 min — beyond that it's deliberate)
    var w = Math.max(1.5, Math.min(5, median * 1.5));
    return parseFloat(w.toFixed(1));
}

function detectThrash(trades, windowMin) {
    var sessions = [], used = {};
    for (var i = 0; i < trades.length; i++) {
        if (used[i]) continue;
        var t = trades[i];
        var sym = (t.underlying||'')+'|'+(t.strike||'')+'|'+(t.option_type||'');
        var tMin = timeToMin(t.entry_time);
        var group = [i];
        for (var j = i+1; j < trades.length; j++) {
            if (used[j]) continue;
            var sym2 = (trades[j].underlying||'')+'|'+(trades[j].strike||'')+'|'+(trades[j].option_type||'');
            if (sym2 === sym && (timeToMin(trades[j].entry_time) - tMin) <= windowMin) {
                group.push(j); used[j] = true;
            }
        }
        used[i] = true;
        if (group.length >= 2) {
            var gt = group.map(function(idx){ return trades[idx]; });
            var netPnl = gt.reduce(function(a,r){ return a+(r.pnl||0); }, 0);
            var otype = (t.option_type||'').toUpperCase();
            sessions.push({
                label: otype + ' ' + (t.strike||''),
                count: group.length,
                trades: gt,
                netPnl: netPnl,
                startTime: t.entry_time,
                endTime: gt[gt.length-1].exit_time || gt[gt.length-1].entry_time
            });
        }
    }
    return sessions;
}

var _thrashExpanded = {};
function toggleThrash(idx) {
    _thrashExpanded[idx] = !_thrashExpanded[idx];
    var detail = document.getElementById('thrash-detail-'+idx);
    var arrow  = document.getElementById('thrash-arrow-'+idx);
    if (detail) detail.style.display = _thrashExpanded[idx] ? 'block' : 'none';
    if (arrow)  arrow.textContent = _thrashExpanded[idx] ? '▲' : '▼';
}

function renderThrashSessions(trades) {
    var section = document.getElementById('thrash-section');
    var w = adaptiveThrashWindow(trades);
    var sessions = detectThrash(trades, w);
    if (sessions.length === 0) { section.style.display='none'; return; }
    section.style.display = 'block';
    _thrashExpanded = {};
    document.getElementById('thrash-badge').textContent =
        sessions.length+' session'+(sessions.length!==1?'s':'')+' · auto window: '+w+'m';
    var thrashPnl   = sessions.reduce(function(a,s){ return a+s.netPnl; }, 0);
    var thrashCount = sessions.reduce(function(a,s){ return a+s.count; }, 0);
    var html = '<div style="font-size:11px;color:#8b949e;margin-bottom:8px;">Same instrument traded ≥2× within '+w+' min (auto-detected). '+thrashCount+' trades &nbsp;|&nbsp; Net: <span class="'+(thrashPnl>=0?'positive':'negative')+'" style="font-weight:600;">'+fmt(thrashPnl)+'</span></div>';
    sessions.forEach(function(s, idx) {
        var cls = s.netPnl >= 0 ? '#3fb950' : '#f85149';
        html += '<div style="background:#0d1117;border:1px solid #21262d;border-radius:6px;margin-bottom:6px;">';
        html += '<div onclick="toggleThrash('+idx+')" style="display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer;user-select:none;">';
        html += '<span style="font-size:12px;font-weight:700;color:#e6edf3;flex:1;">'+s.label+'</span>';
        html += '<span style="font-size:11px;color:#8b949e;">'+s.count+' flips &nbsp; '+s.startTime+' → '+(s.endTime||'-')+'</span>';
        html += '<span style="font-size:12px;font-weight:700;color:'+cls+';margin-left:10px;">'+fmt(s.netPnl)+'</span>';
        html += '<span id="thrash-arrow-'+idx+'" style="color:#484f58;margin-left:8px;font-size:10px;">▼</span>';
        html += '</div>';
        html += '<div id="thrash-detail-'+idx+'" style="display:none;border-top:1px solid #21262d;padding:8px 12px;">'+tradeTable(s.trades)+'</div>';
        html += '</div>';
    });
    document.getElementById('thrash-content').innerHTML = html;
}

// ── Trade clusters (any instrument, adaptive window) ─────────────────

function adaptiveClusterWindow(trades) {
    if (trades.length < 2) return 5;
    var gaps = [];
    for (var i = 1; i < trades.length; i++)
        gaps.push(timeToMin(trades[i].entry_time) - timeToMin(trades[i-1].entry_time));
    // Exclude session-level breaks (>30 min) so threshold reflects trading pace only
    var sessionGaps = gaps.filter(function(g){ return g >= 0 && g <= 30; });
    if (sessionGaps.length === 0) return 5;
    sessionGaps.sort(function(a,b){return a-b;});
    // Split at P70 — the top 30% of gaps become cluster boundaries.
    // This always creates natural breaks at the widest pauses in the session.
    var p70idx = Math.floor(sessionGaps.length * 0.70);
    var w = Math.max(1, Math.min(10, sessionGaps[p70idx]));
    return parseFloat(w.toFixed(1));
}

function detectClusters(trades, windowMin) {
    // A cluster = contiguous run where each consecutive gap ≤ windowMin, and cluster has ≥2 trades
    if (trades.length === 0) return [];
    var clusters = [], current = [trades[0]];
    for (var i = 1; i < trades.length; i++) {
        var gap = timeToMin(trades[i].entry_time) - timeToMin(trades[i-1].entry_time);
        if (gap >= 0 && gap <= windowMin) {
            current.push(trades[i]);
        } else {
            if (current.length >= 2) clusters.push(current);
            current = [trades[i]];
        }
    }
    if (current.length >= 2) clusters.push(current);
    return clusters;
}

var _clusterExpanded = {};
function toggleCluster(idx) {
    _clusterExpanded[idx] = !_clusterExpanded[idx];
    var detail = document.getElementById('cluster-detail-'+idx);
    var arrow  = document.getElementById('cluster-arrow-'+idx);
    if (detail) detail.style.display = _clusterExpanded[idx] ? 'block' : 'none';
    if (arrow)  arrow.textContent = _clusterExpanded[idx] ? '▲' : '▼';
}

function renderClusters(trades) {
    var section = document.getElementById('cluster-section');
    var w = adaptiveClusterWindow(trades);
    var clusters = detectClusters(trades, w);
    if (clusters.length === 0) { section.style.display='none'; return; }
    section.style.display = 'block';
    _clusterExpanded = {};
    document.getElementById('cluster-badge').textContent =
        clusters.length+' cluster'+(clusters.length!==1?'s':'')+' · auto window: '+w+'m';
    var html = '<div style="font-size:11px;color:#8b949e;margin-bottom:8px;">Consecutive trades with gaps ≤'+w+' min (auto-detected).</div>';
    clusters.forEach(function(cluster, idx) {
        var netPnl = cluster.reduce(function(a,t){ return a+(t.pnl||0); }, 0);
        var spanMin = timeToMin(cluster[cluster.length-1].entry_time) - timeToMin(cluster[0].entry_time);
        var spanStr = spanMin < 1 ? '<1m' : spanMin.toFixed(1)+'m';
        var col = netPnl >= 0 ? '#3fb950' : '#f85149';
        html += '<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;margin-bottom:6px;">';
        html += '<div onclick="toggleCluster('+idx+')" style="display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer;user-select:none;">';
        html += '<span style="font-size:11px;color:#8b949e;flex:1;">'+cluster[0].entry_time+' → '+(cluster[cluster.length-1].exit_time||cluster[cluster.length-1].entry_time)+' &nbsp;<span style="color:#484f58;">('+spanStr+')</span></span>';
        html += '<span style="font-size:11px;color:#8b949e;">'+cluster.length+' trades</span>';
        html += '<span style="font-size:12px;font-weight:700;color:'+col+';margin-left:10px;">'+fmt(netPnl)+'</span>';
        html += '<span id="cluster-arrow-'+idx+'" style="color:#484f58;margin-left:8px;font-size:10px;">▼</span>';
        html += '</div>';
        html += '<div id="cluster-detail-'+idx+'" style="display:none;border-top:1px solid #21262d;padding:8px 12px;">'+tradeTable(cluster)+'</div>';
        html += '</div>';
    });
    document.getElementById('cluster-content').innerHTML = html;
}

// ── Day equity curve ────────────────────────────────────────────────
var _dayEqChart = null;
var _dayEqChartFull = null;
var _dayEqClosed = [];
var _dayEqLabels = [], _dayEqVals = [], _dayEqColors = [];

function _buildDayEqConfig(closedTrades, labels, vals, colors) {
    return {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                data: vals,
                borderColor: '#58a6ff',
                borderWidth: 2,
                fill: false,
                tension: 0.3,
                pointRadius: 5,
                pointBackgroundColor: colors,
                pointBorderColor: colors,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: function(ctx) {
                            var t = closedTrades[ctx.dataIndex];
                            var sign = (t.pnl||0) >= 0 ? '+' : '';
                            return 'Cumul: ₹' + ctx.parsed.y.toLocaleString('en-IN') + '  (trade: ' + sign + Math.round(t.pnl||0) + ')';
                        }
                    }
                },
                zoom: {
                    zoom: { wheel: { enabled: true, speed: 0.1 }, pinch: { enabled: true }, mode: 'x' },
                    pan: { enabled: true, mode: 'x' },
                    limits: { x: { minRange: 2 } }
                }
            },
            scales: {
                x: { ticks: { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 12, maxRotation: 0 }, grid: { color: '#161b22' } },
                y: { ticks: { color: '#8b949e', font: { size: 10 }, callback: function(v){ return '₹' + v.toLocaleString('en-IN'); } }, grid: { color: '#21262d' } }
            },
            animation: { duration: 0 }
        }
    };
}

function renderDayEquityCurve(trades) {
    var section = document.getElementById('day-eq-section');
    _dayEqClosed = trades.filter(function(t){ return t.pnl != null && t.status !== 'OPEN'; });
    if (!_dayEqClosed.length) { section.style.display = 'none'; return; }
    section.style.display = 'block';

    _dayEqLabels = []; _dayEqVals = []; _dayEqColors = [];
    var cumul = 0;
    _dayEqClosed.forEach(function(t) {
        cumul += t.pnl || 0;
        var raw = t.exit_time || t.entry_time || '';
        _dayEqLabels.push(raw.length > 8 ? raw.substring(11,16) : raw.substring(0,5));
        _dayEqVals.push(Math.round(cumul));
        _dayEqColors.push((t.pnl || 0) >= 0 ? '#3fb950' : '#f85149');
    });

    if (typeof Chart === 'undefined') return;
    var ctx = document.getElementById('day-eq-chart');
    if (!ctx) return;

    if (_dayEqChart) { _dayEqChart.destroy(); _dayEqChart = null; }
    _dayEqChart = new Chart(ctx, _buildDayEqConfig(_dayEqClosed, _dayEqLabels, _dayEqVals, _dayEqColors));
    ctx.addEventListener('dblclick', function(){ _dayEqChart && _dayEqChart.resetZoom(); });
}

function openEqModal() {
    var modal = document.getElementById('eq-modal');
    var title = document.getElementById('day-detail-title');
    document.getElementById('eq-modal-title').textContent = '📈 Equity Curve — ' + (title ? title.textContent : '');
    modal.style.display = 'flex';
    if (_dayEqChartFull) { _dayEqChartFull.destroy(); _dayEqChartFull = null; }
    setTimeout(function() {
        var ctx = document.getElementById('day-eq-chart-full');
        if (!ctx || !_dayEqClosed.length) return;
        _dayEqChartFull = new Chart(ctx, _buildDayEqConfig(_dayEqClosed, _dayEqLabels, _dayEqVals, _dayEqColors));
        ctx.addEventListener('dblclick', function(){ _dayEqChartFull && _dayEqChartFull.resetZoom(); });
    }, 50);
}

function closeEqModal() {
    document.getElementById('eq-modal').style.display = 'none';
}
</script>
</body>
</html>'''


def _build_journal_page():
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Trade Journal</title>
<style>
*{box-sizing:border-box}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;margin:0;padding:20px}
.topbar{display:flex;align-items:center;gap:16px;margin-bottom:20px;border-bottom:1px solid #21262d;padding-bottom:14px}
.topbar h2{margin:0;font-size:16px;color:#e6edf3}
.filter-row{display:flex;gap:8px;margin-left:auto;align-items:center}
.filter-btn{background:#161b22;border:1px solid #30363d;color:#8b949e;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.filter-btn.active{background:#1f6feb;border-color:#388bfd;color:#e6edf3}
.stats-row{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.stat-card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:12px 18px;min-width:120px}
.stat-card .label{font-size:10px;color:#484f58;text-transform:uppercase;letter-spacing:.5px}
.stat-card .value{font-size:18px;font-weight:700;margin-top:4px}
.green{color:#3fb950}.red{color:#f85149}.gold{color:#d29922}.blue{color:#58a6ff}.muted{color:#8b949e}
.journal-grid{display:flex;flex-direction:column;gap:10px}
.trade-card{background:#161b22;border:1px solid #21262d;border-radius:8px;overflow:hidden}
.trade-card:hover{border-color:#30363d}
.trade-card.win{border-left:3px solid #3fb950}
.trade-card.loss{border-left:3px solid #f85149}
.trade-card.open{border-left:3px solid #d29922}
.card-header{display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;user-select:none;flex-wrap:wrap}
.card-header:hover{background:rgba(255,255,255,.02)}
.trade-badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;white-space:nowrap}
.badge-spread{background:#1a2d1a;color:#3fb950;border:1px solid #2ea04326}
.badge-naked{background:#2d1a1a;color:#f85149;border:1px solid #f8514926}
.trade-instrument{font-size:13px;font-weight:700;color:#e6edf3;min-width:200px}
.trade-time{font-size:11px;color:#484f58;white-space:nowrap}
.price-col{display:flex;flex-direction:column;align-items:flex-end;min-width:80px}
.price-col .main{font-size:13px;font-weight:700}
.price-col .sub{font-size:10px;color:#484f58}
.pnl-col{font-size:15px;font-weight:700;min-width:90px;text-align:right}
.pnl-col .lots{font-size:10px;color:#484f58;font-weight:400;display:block}
.expand-btn{margin-left:auto;color:#484f58;font-size:12px;transition:transform .2s}
.expanded .expand-btn{transform:rotate(180deg)}
.card-detail{display:none;border-top:1px solid #21262d;padding:14px}
.card-detail.show{display:block}
.detail-row{display:flex;gap:16px;flex-wrap:wrap}
.screenshots{display:flex;gap:10px;flex:1;min-width:280px}
.screenshot-box{flex:1}
.screenshot-box .ss-label{font-size:10px;color:#484f58;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.screenshot-box img{width:100%;border-radius:6px;border:1px solid #21262d;cursor:zoom-in;transition:border-color .15s;display:block}
.screenshot-box img:hover{border-color:#58a6ff}
.screenshot-box .ss-time{font-size:10px;color:#484f58;margin-top:3px;text-align:center}
.screenshot-placeholder{background:#0d1117;border:1px dashed #21262d;border-radius:6px;height:90px;display:flex;align-items:center;justify-content:center;color:#484f58;font-size:11px}
.trade-meta{flex:1;min-width:200px}
.meta-table{width:100%;font-size:11px;border-collapse:collapse}
.meta-table td{padding:3px 0;color:#8b949e}
.meta-table td:first-child{color:#484f58;width:110px}
.notes-box{margin-top:10px}
.notes-box textarea{width:100%;background:#0d1117;border:1px solid #21262d;border-radius:6px;color:#e6edf3;font-size:11px;padding:8px;resize:vertical;min-height:48px;font-family:inherit}
.notes-box textarea:focus{outline:none;border-color:#388bfd}
.notes-box textarea::placeholder{color:#484f58}
.save-note-btn{background:#1f6feb;border:none;color:#e6edf3;padding:4px 12px;border-radius:6px;font-size:11px;cursor:pointer;margin-top:4px}
#lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:1000;align-items:center;justify-content:center;cursor:zoom-out}
#lightbox.show{display:flex}
#lightbox img{max-width:90vw;max-height:90vh;border-radius:8px;border:1px solid #30363d}
.empty{text-align:center;padding:60px 20px;color:#484f58}
.refresh-btn{background:none;border:1px solid #30363d;color:#8b949e;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.refresh-btn:hover{border-color:#58a6ff;color:#58a6ff}
</style>
</head>
<body>
<div class="topbar">
  <h2>&#x1F4D3; Trade Journal</h2>
  <div style="font-size:12px;color:#484f58;" id="last-updated"></div>
  <div class="filter-row">
    <input type="date" id="journal-date" onchange="loadEntries()"
      style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;
             padding:4px 8px;font-size:12px;cursor:pointer;">
    <button onclick="changeDate(-1)" style="background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:4px 8px;cursor:pointer;font-size:12px;">&#8249;</button>
    <button onclick="changeDate(1)"  style="background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:4px 8px;cursor:pointer;font-size:12px;">&#8250;</button>
    <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
    <button class="filter-btn" onclick="setFilter('open',this)">Open</button>
    <button class="filter-btn" onclick="setFilter('win',this)">Winners</button>
    <button class="filter-btn" onclick="setFilter('loss',this)">Losers</button>
    <select id="sort-select" onchange="renderAll()" style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer;">
      <option value="newest">Newest first</option>
      <option value="oldest">Oldest first</option>
      <option value="pnl_desc">Best P&amp;L</option>
      <option value="pnl_asc">Worst P&amp;L</option>
    </select>
    <button class="refresh-btn" onclick="loadEntries()">&#x21bb; Refresh</button>
    <button onclick="clearToday()" style="background:#2d1117;color:#f85149;border:1px solid #f85149;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer;" title="Delete all entries for the currently viewed date">&#x1F5D1; Clear date</button>
    <button id="backfill-btn" onclick="runBackfill()" style="background:#1a3a2a;color:#3fb950;border:1px solid #3fb950;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer;">&#x2193; Import from Dhan</button>
    <label style="background:#1a2a3a;color:#58a6ff;border:1px solid #58a6ff;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer;" title="Export CSV from Dhan Order Book, then upload here">
      &#x2B06; Upload CSV
      <input type="file" id="csv-upload" accept=".csv" onchange="uploadCsv(this)" style="display:none;">
    </label>
    <span id="csv-status" style="font-size:11px;color:#8b949e;"></span>
  </div>
</div>
<div class="stats-row" id="stats-row"></div>
<div class="journal-grid" id="journal"></div>
<div id="lightbox" onclick="closeLightbox()"><img id="lb-img" src=""></div>
<script>
var _entries = [], _filter = 'all';

function runBackfill() {
  var btn = document.getElementById('backfill-btn');
  btn.textContent = '⏳ Importing...';
  btn.disabled = true;
  fetch('/api/journal/backfill', {method: 'POST'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (d.status === 'ok') {
        btn.textContent = '✓ ' + d.created + ' imported';
        btn.style.background = '#0d2117';
        loadEntries();
        setTimeout(function(){
          btn.textContent = '↓ Import from Dhan';
          btn.style.background = '#1a3a2a';
          btn.disabled = false;
        }, 3000);
      } else {
        btn.textContent = '✕ ' + (d.message || 'Error');
        btn.style.color = '#f85149';
        setTimeout(function(){
          btn.textContent = '↓ Import from Dhan';
          btn.style.color = '#3fb950';
          btn.disabled = false;
        }, 3000);
      }
    })
    .catch(function() {
      btn.textContent = '✕ Network error';
      btn.disabled = false;
    });
}

function clearToday() {
  var d = document.getElementById('date-input') ? document.getElementById('date-input').value : null;
  var label = d || 'today';
  if (!confirm('Delete ALL journal entries for ' + label + '? This cannot be undone.')) return;
  fetch('/api/journal/clear_date', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date: d})
  }).then(function(r){ return r.json(); })
    .then(function(d) {
      if (d.status === 'ok') { loadEntries(); }
      else { alert('Error: ' + (d.message || 'unknown')); }
    });
}

function uploadCsv(input) {
  var file = input.files[0];
  if (!file) return;
  var status = document.getElementById('csv-status');
  status.textContent = '⏳ Uploading...';
  var fd = new FormData();
  fd.append('file', file);
  fetch('/api/journal/upload_csv', {method: 'POST', body: fd})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (d.status === 'ok') {
        status.style.color = d.created > 0 ? '#3fb950' : '#f0883e';
        status.textContent = d.created > 0
          ? ('✓ ' + d.created + ' entries imported from CSV (' + (d.rows_scanned||0) + ' trades scanned)')
          : ('⚠ ' + (d.message || '0 entries imported'));
        if (d.created > 0) loadEntries();
      } else {
        status.style.color = '#f85149';
        status.textContent = '✕ ' + (d.message || 'Error');
      }
      input.value = '';
    })
    .catch(function() {
      status.style.color = '#f85149';
      status.textContent = '✕ Network error';
      input.value = '';
    });
}

function todayIst() {
  return new Date().toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'}); // "YYYY-MM-DD"
}
function changeDate(delta) {
  var d = new Date(document.getElementById('journal-date').value + 'T00:00:00');
  d.setDate(d.getDate() + delta);
  document.getElementById('journal-date').value = d.toISOString().slice(0,10);
  loadEntries();
}
function loadEntries() {
  var date = document.getElementById('journal-date').value || todayIst();
  fetch('/api/journal/entries?date=' + date)
    .then(function(r){return r.json();})
    .then(function(data) {
      _entries = data;
      document.getElementById('last-updated').textContent =
        'Updated ' + new Date().toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'});
      renderAll();
    });
}

function setFilter(f, btn) {
  _filter = f;
  document.querySelectorAll('.filter-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  renderAll();
}

function fmtIst(val) {
  if (!val) return '';
  // created_at is a Unix timestamp (number); entry_time/exit_time are strings
  var d = (typeof val === 'number') ? new Date(val * 1000)
        : new Date(String(val).replace(' ', 'T') + 'Z');
  if (isNaN(d)) return String(val);
  return d.toLocaleString('en-IN', {timeZone:'Asia/Kolkata',
    day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', second:'2-digit',
    hour12: false});
}

function renderAll() {
  var list = _entries.filter(function(t) {
    if (_filter === 'all')  return true;
    if (_filter === 'open') return t.status === 'open';
    if (_filter === 'win')  return t.status === 'closed' && (t.pnl||0) >= 0;
    if (_filter === 'loss') return t.status === 'closed' && (t.pnl||0) < 0;
    return true;
  });
  var sortBy = document.getElementById('sort-select').value;
  list.sort(function(a, b) {
    if (sortBy === 'oldest')   return (a.created_at||0) - (b.created_at||0);
    if (sortBy === 'pnl_desc') return (b.pnl||0) - (a.pnl||0);
    if (sortBy === 'pnl_asc')  return (a.pnl||0) - (b.pnl||0);
    return (b.created_at||0) - (a.created_at||0); // newest first default
  });
  renderStats(list);
  var el = document.getElementById('journal');
  el.innerHTML = '';
  if (!list.length) {
    el.innerHTML = '<div class="empty"><div style="font-size:40px;margin-bottom:10px">&#x1F4ED;</div>No trades found</div>';
    return;
  }
  list.forEach(function(t){ el.appendChild(buildCard(t)); });
}

function renderStats(list) {
  var closed = list.filter(function(t){return t.status==='closed';});
  var wins   = closed.filter(function(t){return (t.pnl||0)>=0;});
  var total  = closed.reduce(function(s,t){return s+(t.pnl||0);},0);
  var avgW   = wins.length ? wins.reduce(function(s,t){return s+(t.pnl||0);},0)/wins.length : 0;
  var losses = closed.filter(function(t){return (t.pnl||0)<0;});
  var avgL   = losses.length ? losses.reduce(function(s,t){return s+(t.pnl||0);},0)/losses.length : 0;
  var wr = closed.length ? Math.round(wins.length/closed.length*100) : 0;
  var pnlClass = total>=0?'green':'red';

  document.getElementById('stats-row').innerHTML =
    stat('Total', list.length, 'blue') +
    stat('Win Rate', closed.length ? wr+'%' : '—', wr>=50?'green':'red') +
    stat('Total P&amp;L', (total>=0?'&#8377;+':'&#8377;')+Math.round(total).toLocaleString('en-IN'), pnlClass) +
    stat('Avg Winner', wins.length?'&#8377;+'+Math.round(avgW):'—','green') +
    stat('Avg Loser',  losses.length?'&#8377;'+Math.round(avgL):'—','red');
}

function stat(label, val, cls) {
  return '<div class="stat-card"><div class="label">'+label+'</div>' +
         '<div class="value '+cls+'">'+val+'</div></div>';
}

function nc(t) { return (t.sell_entry_price||0) - (t.buy_entry_price||0); }
function qty(t) { return (t.lots||0) * (t.lot_size||25); }

function buildCard(t) {
  var pnl = t.pnl;
  var isOpen = t.status === 'open';
  var cardClass = isOpen ? 'open' : (pnl>=0?'win':'loss');

  var pnlHtml = isOpen
    ? '<span class="gold">OPEN</span>'
    : '<span class="'+(pnl>=0?'green':'red')+'">&#8377;'+(pnl>=0?'+':'')
      +Math.round(pnl).toLocaleString('en-IN')+'</span>';

  var exitHtml = t.sell_exit_price
    ? '<div class="main '+(cardClass==='win'?'green':'red')+'">&#8377;'+parseFloat(t.sell_exit_price).toFixed(2)+'</div><div class="sub">exit</div>'
    : '<div class="main muted">—</div><div class="sub">not exited</div>';

  var timeStr = fmtIst(t.created_at) + (t.exit_time ? ' &#x2192; ' + fmtIst(t.exit_time) : '');

  var metaRows = '';
  if (t.trade_type === 'spread') {
    metaRows += '<tr><td>Hedge leg</td><td><span style="color:#e6edf3">'+(t.hedge_instrument||'—')+'</span></td></tr>';
    var ncVal = nc(t);
    metaRows += '<tr><td>Net credit</td><td><span class="gold">&#8377;'+ncVal.toFixed(2)
      +' &times; '+qty(t)+' = &#8377;'+Math.round(ncVal*qty(t)).toLocaleString('en-IN')+'</span></td></tr>';
  } else {
    metaRows += '<tr><td>Type</td><td><span class="red">Naked short</span></td></tr>';
    var prem = (t.sell_entry_price||0)*qty(t);
    metaRows += '<tr><td>Premium rcvd</td><td><span class="gold">&#8377;'+parseFloat(t.sell_entry_price||0).toFixed(2)
      +' &times; '+qty(t)+' = &#8377;'+Math.round(prem).toLocaleString('en-IN')+'</span></td></tr>';
  }
  if (pnl !== null && pnl !== undefined)
    metaRows += '<tr><td>P&amp;L</td><td><span class="'+(pnl>=0?'green':'red')
      +'">&#8377;'+(pnl>=0?'+':'')+Math.round(pnl).toLocaleString('en-IN')+'</span></td></tr>';

  var entryImg = t.entry_screenshot
    ? '<img src="/api/journal/screenshots/'+t.entry_screenshot+'" onclick="openLightbox(this)">'
      +'<div class="ss-time">'+fmtIst(t.created_at)+'</div>'
    : '<div class="screenshot-placeholder">No screenshot</div>';

  var exitImg = t.exit_screenshot
    ? '<img src="/api/journal/screenshots/'+t.exit_screenshot+'" onclick="openLightbox(this)">'
      +'<div class="ss-time">'+fmtIst(t.exit_time)+'</div>'
    : '<div class="screenshot-placeholder">'+(isOpen?'Captured on exit':'No screenshot')+'</div>';

  var div = document.createElement('div');
  div.className = 'trade-card '+cardClass;
  div.id = 'card-'+t.id;
  div.innerHTML =
    '<div class="card-header" onclick="toggleCard(\\'' + t.id + '\\')">' +
      '<span class="trade-badge '+(t.trade_type==='spread'?'badge-spread':'badge-naked')+'">'
        +(t.trade_type==='spread'?'SPREAD':'NAKED')+'</span>' +
      '<div class="trade-instrument">'+t.instrument+'</div>' +
      '<div class="trade-time">'+timeStr+'</div>' +
      '<div class="price-col"><div class="main gold">&#8377;'+parseFloat(t.sell_entry_price||0).toFixed(2)+'</div>'
        +'<div class="sub">sell entry</div></div>' +
      '<div class="price-col">'+exitHtml+'</div>' +
      '<div class="pnl-col">'+pnlHtml+'<span class="lots">'+t.lots+' lot'+(t.lots!==1?'s':'')+' &middot; '+qty(t)+' qty</span></div>' +
      '<span class="expand-btn">&#9660;</span>' +
    '</div>' +
    '<div class="card-detail" id="detail-'+t.id+'">' +
      '<div class="detail-row">' +
        '<div class="screenshots">' +
          '<div class="screenshot-box"><div class="ss-label">Entry chart</div>'+entryImg+'</div>' +
          '<div class="screenshot-box"><div class="ss-label">Exit chart</div>'+exitImg+'</div>' +
        '</div>' +
        '<div class="trade-meta">' +
          '<table class="meta-table">' +
            '<tr><td>Instrument</td><td><span style="color:#e6edf3">'+t.instrument+'</span></td></tr>' +
            metaRows +
            '<tr><td>Lots / Qty</td><td><span style="color:#e6edf3">'+t.lots+' lots &middot; '+qty(t)+' qty</span></td></tr>' +
            '<tr><td>Entry time</td><td><span style="color:#e6edf3">'+fmtIst(t.created_at)+'</span></td></tr>' +
            '<tr><td>Exit time</td><td><span style="color:#e6edf3">'+(t.exit_time?fmtIst(t.exit_time):'—')+'</span></td></tr>' +
          '</table>' +
          '<div class="notes-box">' +
            '<div style="font-size:10px;color:#484f58;margin-bottom:3px;text-transform:uppercase;letter-spacing:.5px">Notes</div>' +
            '<textarea placeholder="Add trade notes…" id="notes-'+t.id+'">'+(t.notes||'')+'</textarea>' +
            '<div style="display:flex;gap:8px;margin-top:4px;">' +
              '<button class="save-note-btn" onclick="saveNote(\\''+t.id+'\\')">Save</button>' +
              '<button onclick="deleteEntry(\\''+t.id+'\\',\\''+t.instrument+'\\')" style="padding:4px 10px;font-size:11px;background:none;border:1px solid #f85149;color:#f85149;border-radius:4px;cursor:pointer;">Delete</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  return div;
}

function toggleCard(id) {
  var detail = document.getElementById('detail-'+id);
  var card   = document.getElementById('card-'+id);
  var isOpen = detail.classList.contains('show');
  detail.classList.toggle('show', !isOpen);
  card.classList.toggle('expanded', !isOpen);
}

function saveNote(id) {
  var notes = document.getElementById('notes-'+id).value;
  fetch('/api/journal/entry/'+id, {
    method:'PUT',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'notes', notes:notes})
  }).then(function() {
    var btn = document.querySelector('#card-'+id+' .save-note-btn');
    btn.textContent = '&#x2713; Saved'; btn.style.background='#2ea043';
    setTimeout(function(){btn.textContent='Save';btn.style.background='';},1500);
  });
}

function deleteEntry(id, instrument) {
  if (!confirm('Delete journal entry for ' + instrument + '?')) return;
  fetch('/api/journal/entry/' + id, {method: 'DELETE'})
    .then(function(r){return r.json();})
    .then(function(d) {
      if (d.status === 'ok') {
        _entries = _entries.filter(function(e){return e.id != id;});
        renderAll();
      }
    });
}

function openLightbox(img) {
  event.stopPropagation();
  document.getElementById('lb-img').src = img.src;
  document.getElementById('lightbox').classList.add('show');
}
function closeLightbox() { document.getElementById('lightbox').classList.remove('show'); }
document.addEventListener('keydown', function(e){ if(e.key==='Escape') closeLightbox(); });

document.getElementById('journal-date').value = todayIst();
loadEntries();
setInterval(loadEntries, 30000);
</script>
</body>
</html>"""


# ── Token Update ──────────────────────────────────────────────────

@app.route("/api/update_token", methods=["POST"])
def api_update_token():
    """Update Dhan access token in .env and reinitialize the API client."""
    import os as _os
    data = request.json
    new_token = (data.get("access_token") or "").strip()
    if not new_token:
        return jsonify({"status": "error", "message": "Token cannot be empty"}), 400

    env_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
        with open(env_path, "w") as f:
            for line in lines:
                if line.startswith("DHAN_ACCESS_TOKEN="):
                    f.write("DHAN_ACCESS_TOKEN=" + new_token + "\n")
                else:
                    f.write(line)

        # Reinitialize the Dhan API client in-memory (no restart needed)
        if _monitor:
            _monitor.api.reinitialize(Config.DHAN_CLIENT_ID, new_token)
            Config.DHAN_ACCESS_TOKEN = new_token

        logger.info("Access token updated successfully")
        return jsonify({"status": "ok", "message": "Token updated and API reinitialized"})
    except Exception as e:
        logger.error("Token update failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/token/status")
def api_token_status():
    """Check if the current API token is valid by making a lightweight API call."""
    if not _monitor:
        return jsonify({"valid": False, "error": "Monitor not initialized"}), 500
    try:
        resp = _monitor.api.get_fund_limits()
        if isinstance(resp, dict) and resp.get("status") == "success":
            return jsonify({"valid": True, "balance": resp.get("data", {}).get("availabelBalance")})
        else:
            msg = ""
            if isinstance(resp, dict):
                remarks = resp.get("remarks", {})
                if isinstance(remarks, dict):
                    msg = remarks.get("error_message", "")
                elif isinstance(remarks, str):
                    msg = remarks
            return jsonify({"valid": False, "error": msg or "Token validation failed", "raw": resp})
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500


@app.route("/api/token/refresh", methods=["POST"])
def api_token_refresh():
    """Trigger automatic token refresh via PIN + TOTP (no manual token needed)."""
    from token_manager import refresh_token, is_token_refresh_configured
    import os as _os

    if not is_token_refresh_configured():
        return jsonify({
            "status": "error",
            "message": "Auto-refresh not configured. Set DHAN_PIN and DHAN_TOTP_SECRET in .env"
        }), 400

    try:
        success = refresh_token(dhan_api=_monitor.api if _monitor else None)
        if success:
            # Reload config
            from dotenv import load_dotenv
            load_dotenv(override=True)
            Config.DHAN_ACCESS_TOKEN = _os.getenv("DHAN_ACCESS_TOKEN", "")
            return jsonify({"status": "ok", "message": "Token refreshed successfully"})
        else:
            return jsonify({"status": "error", "message": "Token refresh failed. Check server logs."}), 500
    except Exception as e:
        logger.error("Manual token refresh failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Pending Order Management ──────────────────────────────────────

@app.route("/api/order/modify", methods=["POST"])
def api_modify_order():
    """Modify a pending order on Dhan (price/trigger)."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json
    order_id = data.get("order_id", "")
    if not order_id:
        return jsonify({"status": "error", "message": "Missing order_id"}), 400
    try:
        result = _monitor.api.modify_order(
            order_id=order_id,
            order_type=data.get("order_type", "LIMIT"),
            quantity=int(data.get("quantity", 0)),
            price=float(data.get("price", 0)),
            trigger_price=float(data.get("trigger_price", 0)),
            validity=data.get("validity", "DAY"),
        )
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        logger.error("Order modify error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/order/cancel_pending", methods=["POST"])
def api_cancel_pending_order():
    """Cancel a single pending order on Dhan."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json
    order_id = data.get("order_id", "")
    if not order_id:
        return jsonify({"status": "error", "message": "Missing order_id"}), 400
    try:
        result = _monitor.api.cancel_order(order_id=order_id)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        logger.error("Order cancel error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── SocketIO Emitters ──────────────────────────────────────────────

def emit_status_update(status_data: dict):
    """Push status update to all connected dashboard clients."""
    socketio.emit("status_update", status_data)


def emit_sl_tp_trigger(trigger_data: dict):
    """Push SL/TP trigger notification to dashboard."""
    socketio.emit("sl_tp_triggered", trigger_data)


# Security IDs subscribed for real-time option chain LTP updates
_oc_ltp_subscribed: set = set()
# Full instrument tuples for all ever-subscribed OC strikes: sid -> (seg_int, sid, 15)
_oc_ltp_instruments: dict = {}
# Timestamp of last LTP feed start (used to detect stale feed after page refresh)
_oc_last_feed_start: float = 0.0
# Security IDs of BSE index futures — ticks update _bse_last_spot in real-time
_bse_futures_sids: set = set()


def emit_oc_ltp(security_id: str, ltp: float):
    """Push real-time LTP tick for option chain cell update."""
    socketio.emit("oc_ltp", {"sid": security_id, "ltp": ltp})


# ── Depth of Market ──────────────────────────────────────────────

def analyze_depth(bids: list, asks: list) -> dict:
    """Analyze 20-level depth and return key trading insights.

    Each bid/ask is: {price: float, quantity: int, orders: int}
    bids[0] = best bid (highest price), asks[0] = best ask (lowest price)
    """
    if not bids or not asks:
        return {}

    total_bid_qty = sum(b["quantity"] for b in bids)
    total_ask_qty = sum(a["quantity"] for a in asks)
    avg_bid_qty = total_bid_qty / len(bids) if bids else 1
    avg_ask_qty = total_ask_qty / len(asks) if asks else 1
    avg_bid_orders = sum(b["orders"] for b in bids) / len(bids) if bids else 1
    avg_ask_orders = sum(a["orders"] for a in asks) / len(asks) if asks else 1

    # Bid-Ask Spread
    spread = round(asks[0]["price"] - bids[0]["price"], 2)
    mid_price = (asks[0]["price"] + bids[0]["price"]) / 2
    spread_pct = round(spread / mid_price * 100, 3) if mid_price > 0 else 0

    # Buy/Sell Imbalance
    imbalance = round(total_bid_qty / total_ask_qty, 2) if total_ask_qty > 0 else 999

    # Max walls
    max_bid_wall = max(bids, key=lambda b: b["quantity"])
    max_ask_wall = max(asks, key=lambda a: a["quantity"])

    # Support & resistance (qty > 1.5x average)
    support_levels = [b for b in bids if b["quantity"] > avg_bid_qty * 1.5]
    resistance_levels = [a for a in asks if a["quantity"] > avg_ask_qty * 1.5]

    # Institutional activity (many orders at a level)
    institutional_bids = sum(1 for b in bids if b["orders"] > avg_bid_orders * 2)
    institutional_asks = sum(1 for a in asks if a["orders"] > avg_ask_orders * 2)

    # Sentiment
    sentiment = "NEUTRAL"
    if imbalance > 1.2:
        sentiment = "BULLISH"
    elif imbalance < 0.8:
        sentiment = "BEARISH"

    return {
        "bid_ask_spread": spread,
        "spread_pct": spread_pct,
        "mid_price": round(mid_price, 2),
        "total_bid_qty": total_bid_qty,
        "total_ask_qty": total_ask_qty,
        "imbalance_ratio": imbalance,
        "max_bid_wall": max_bid_wall,
        "max_ask_wall": max_ask_wall,
        "support_levels": support_levels,
        "resistance_levels": resistance_levels,
        "institutional_bids": institutional_bids,
        "institutional_asks": institutional_asks,
        "sentiment": sentiment,
    }


def _depth_emit_loop():
    """Background loop that reads depth data and emits to frontend every 500ms."""
    global _depth_timer_running
    # Prevent multiple emit loops running simultaneously
    if not _depth_timer_lock.acquire(blocking=False):
        logger.warning("Depth emit loop: another loop already running, exiting")
        return
    try:
        _depth_timer_running = True
        _depth_emit_loop_inner()
    finally:
        _depth_timer_running = False
        _depth_timer_lock.release()


def _depth_emit_loop_inner():
    """Inner emit loop (called under _depth_timer_lock)."""
    global _depth_timer_running
    no_data_notified = False
    connection_notified = False
    loop_count = 0
    last_gen = _depth_subscribe_gen
    while _depth_timer_running and _depth_ws:
        # Reset counters when a new subscription comes in
        if _depth_subscribe_gen != last_gen:
            last_gen = _depth_subscribe_gen
            no_data_notified = False
            connection_notified = False
            loop_count = 0
        try:
            depth = _depth_ws.get_depth()
            if depth["bids"] or depth["asks"]:
                no_data_notified = False
                connection_notified = False
                analysis = analyze_depth(depth["bids"], depth["asks"])
                socketio.emit("depth_update", {
                    "bids": depth["bids"],
                    "asks": depth["asks"],
                    "analysis": analysis,
                    "security_id": depth.get("security_id"),
                })
            elif not no_data_notified and loop_count >= 20:
                # ~10 seconds with no data
                connected = depth.get("connected", False)
                disconnect_reason = depth.get("disconnect_reason", "")
                attempts = depth.get("connect_attempts", 0)
                last_error = depth.get("last_error", "")
                ever_connected = depth.get("ever_connected", False)
                if disconnect_reason and not connection_notified:
                    # Server sent a disconnect error
                    socketio.emit("depth_status", {
                        "status": "connection_failed",
                        "reason": disconnect_reason,
                        "security_id": depth.get("security_id"),
                    })
                    connection_notified = True
                    logger.warning("Depth emit: server disconnected — %s", disconnect_reason)
                elif not connected and not connection_notified:
                    # WebSocket never connected or keeps disconnecting
                    reason = last_error if last_error else "WebSocket connection failed"
                    if attempts > 0:
                        reason += f" (attempts: {attempts})"
                    if ever_connected:
                        reason = f"Connection lost after connecting. {reason}"
                    socketio.emit("depth_status", {
                        "status": "connection_failed",
                        "reason": reason,
                        "security_id": depth.get("security_id"),
                    })
                    connection_notified = True
                    logger.warning("Depth emit: not connected after %d loops — "
                                   "attempts=%d ever_connected=%s last_error=%s",
                                   loop_count, attempts, ever_connected, last_error)
                elif connected:
                    # Connected but no data — market likely closed
                    socketio.emit("depth_status", {
                        "status": "no_data",
                        "security_id": depth.get("security_id"),
                    })
                    no_data_notified = True
                    logger.info("Depth emit: connected but no data after %d loops", loop_count)
        except Exception as e:
            logger.error("Depth emit error: %s", e)
        loop_count += 1
        time.sleep(0.5)


@socketio.on("subscribe_depth")
def handle_subscribe_depth(data):
    """Client requests depth for an instrument."""
    global _depth_ws, _depth_timer_running, _depth_cred_version, _depth_subscribe_gen
    security_id = data.get("security_id")
    exchange_segment = data.get("exchange_segment", "NSE_FNO")

    if not security_id:
        return

    if not _monitor:
        socketio.emit("depth_error", {"error": "Monitor not initialized"})
        return

    # Recreate DepthWebSocket if credentials were refreshed
    current_cred_ver = getattr(_monitor.api, '_credentials_version', 0)
    if _depth_ws is not None and _depth_cred_version != current_cred_ver:
        logger.info("Depth WS: credentials changed, recreating connection")
        _depth_ws.stop()
        _depth_ws = None

    # Create or reuse the DepthWebSocket instance
    if _depth_ws is None:
        _depth_ws = DepthWebSocket(
            access_token=_monitor.api.access_token,
            client_id=_monitor.api.client_id,
        )
        _depth_cred_version = current_cred_ver

    # Subscribe to new instrument (stops previous if any)
    _depth_ws.subscribe(security_id, exchange_segment)
    _depth_subscribe_gen += 1  # Reset no-data timeout in emit loop

    # Start emit loop if not already running
    if not _depth_timer_running:
        t = threading.Thread(target=_depth_emit_loop, daemon=True)
        t.start()

    logger.info("Depth subscribed: security_id=%s segment=%s timer_running=%s",
                security_id, exchange_segment, _depth_timer_running)


@socketio.on("unsubscribe_depth")
def handle_unsubscribe_depth(data=None):
    """Client requests to stop depth streaming."""
    global _depth_ws, _depth_timer_running
    _depth_timer_running = False
    if _depth_ws:
        _depth_ws.stop()
        _depth_ws = None  # Ensure fresh instance on next subscribe
    logger.info("Depth unsubscribed")


@app.route("/api/depth-diag")
def api_depth_diag():
    """Diagnostic endpoint: tests network connectivity and reports WebSocket state.

    Does NOT create a competing WebSocket connection (Dhan limits concurrent connections).
    Instead, tests TCP/SSL and reports the state of the existing DepthWebSocket.
    """
    import socket
    import ssl

    results = {"steps": [], "overall": "unknown"}

    def step(name, ok, detail=""):
        results["steps"].append({"name": name, "ok": ok, "detail": detail})
        return ok

    # Step 1: Check credentials
    if not _monitor:
        step("credentials", False, "Monitor not initialized — start the system first")
        results["overall"] = "fail"
        return jsonify(results)
    token = _monitor.api.access_token
    client_id = _monitor.api.client_id
    step("credentials", True, f"client_id={client_id}, token_len={len(token) if token else 0}")

    # Step 2: DNS resolution
    host = "depth-api-feed.dhan.co"
    try:
        ip = socket.gethostbyname(host)
        step("dns", True, f"{host} → {ip}")
    except Exception as e:
        step("dns", False, f"Cannot resolve {host}: {e}")
        results["overall"] = "fail"
        return jsonify(results)

    # Step 3: TCP connection
    try:
        s = socket.create_connection((host, 443), timeout=5)
        s.close()
        step("tcp", True, f"Connected to {host}:443")
    except Exception as e:
        step("tcp", False, f"TCP connection failed: {e}")
        results["overall"] = "fail"
        return jsonify(results)

    # Step 4: SSL handshake
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as ss:
                cert = ss.getpeercert()
                step("ssl", True, f"SSL OK, cert subject={cert.get('subject', 'N/A')}")
    except Exception as e:
        step("ssl", False, f"SSL handshake failed: {e}")
        results["overall"] = "fail"
        return jsonify(results)

    # Step 5: Report current DepthWebSocket state (no new connection created)
    if _depth_ws is not None:
        depth = _depth_ws.get_depth()
        ws_state = {
            "connected": depth.get("connected"),
            "ever_connected": depth.get("ever_connected"),
            "connect_attempts": depth.get("connect_attempts"),
            "last_error": depth.get("last_error"),
            "disconnect_reason": depth.get("disconnect_reason"),
            "security_id": depth.get("security_id"),
            "has_bids": len(depth.get("bids", [])) > 0,
            "has_asks": len(depth.get("asks", [])) > 0,
        }
        if depth.get("connected"):
            step("depth_ws", True, f"Connected and streaming. State: {ws_state}")
        elif depth.get("ever_connected"):
            step("depth_ws", False, f"Was connected but disconnected. State: {ws_state}")
        else:
            step("depth_ws", False, f"Never connected. State: {ws_state}")
    else:
        step("depth_ws", True, "No active subscription (click an option to start)")

    # Overall result
    all_ok = all(s["ok"] for s in results["steps"])
    results["overall"] = "pass" if all_ok else "fail"
    return jsonify(results)


def run_dashboard(monitor):
    """Start the dashboard web server."""
    set_monitor(monitor)
    socketio.run(
        app,
        host=Config.DASHBOARD_HOST,
        port=Config.DASHBOARD_PORT,
        allow_unsafe_werkzeug=True
    )
