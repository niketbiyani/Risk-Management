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
from dhan_api import DepthWebSocket

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = "risk-mgmt-dashboard"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

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
        from dhan_api import DepthWebSocket
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
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Trade Risk Management</title>
    <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
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
        function switchJournalTab(tab) {
            ['today', 'history', 'analytics'].forEach(function(t) {
                var el = document.getElementById('jtab-' + t);
                if (el) el.style.display = t === tab ? '' : 'none';
            });
            document.querySelectorAll('.journal-tab').forEach(function(el) {
                var isActive = el.getAttribute('data-jtab') === tab;
                el.style.borderBottomColor = isActive ? '#58a6ff' : 'transparent';
                el.style.color = isActive ? '#58a6ff' : '#8b949e';
            });
            if (typeof loadJournalHistory === 'function' && tab === 'history') loadJournalHistory();
            if (typeof loadJournalAnalytics === 'function' && tab === 'analytics') loadJournalAnalytics();
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
            <div class="sub" id="trade-stats">0 trades</div>
        </div>
    </div>

    <!-- Risk Meters -->
    <div class="grid grid-detail">
        <div class="card">
            <h3>Trailing Drawdown</h3>
            <div style="display:flex;justify-content:space-between;align-items:baseline;">
                <div>
                    <div class="value" id="hwm-value" style="font-size:22px;">&#8377;0</div>
                    <div class="sub">High Water Mark</div>
                </div>
                <div style="text-align:right;">
                    <div id="drawdown-value" style="font-size:22px;font-weight:700;">&#8377;0</div>
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

    <!-- Option Chain + Depth of Market (side by side) -->
    <div style="padding:0 24px;margin-top:16px;">
        <div style="display:flex;gap:16px;">
            <!-- Left: Option Chain -->
            <div style="flex:0 0 38%;min-width:0;">
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
                            <button onclick="loadOptionChain()" class="btn-neutral" style="padding:4px 10px;font-size:11px;">Refresh</button>
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
                            <h3 style="margin:0;">Depth of Market</h3>
                            <div style="display:flex;gap:2px;">
                                <button id="dom-tab-depth" onclick="switchDomTab('depth')" style="background:none;border:none;color:#3fb950;cursor:pointer;font-size:11px;font-weight:700;padding:2px 8px;border-bottom:2px solid #3fb950;">Depth</button>
                                <button id="dom-tab-chart" onclick="switchDomTab('chart')" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:11px;font-weight:400;padding:2px 8px;border-bottom:2px solid transparent;">Chart</button>
                            </div>
                        </div>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <span id="dom-instrument" style="font-size:12px;color:#8b949e;">Select an option from the chain</span>
                            <span id="dom-status" style="display:none;font-size:10px;padding:2px 6px;border-radius:8px;background:#0d4429;color:#3fb950;font-weight:600;">LIVE</span>
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
                    <div id="dom-chart-canvas" style="display:none;height:480px;width:100%;"></div>

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

    <!-- Recent Orders (Rejected / Executed / Cancelled) -->
    <div style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <h3 style="margin:0;">Recent Orders</h3>
                <span id="recent-orders-count" style="color:#8b949e;font-size:12px;"></span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Instrument</th>
                        <th>Side</th>
                        <th>Qty</th>
                        <th>Type</th>
                        <th>Price</th>
                        <th>Status</th>
                        <th>Reason / Info</th>
                    </tr>
                </thead>
                <tbody id="recent-orders-body">
                    <tr><td colspan="7" style="text-align:center;color:#484f58;">No recent orders</td></tr>
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

    <!-- Trade Journal -->
    <div style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <div style="display:flex;gap:0;border-bottom:1px solid #21262d;margin-bottom:16px;">
                <div class="journal-tab active" data-jtab="today" onclick="switchJournalTab('today')" style="padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid #58a6ff;color:#58a6ff;">Today</div>
                <div class="journal-tab" data-jtab="history" onclick="switchJournalTab('history')" style="padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;">History</div>
                <div class="journal-tab" data-jtab="analytics" onclick="switchJournalTab('analytics')" style="padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;">Analytics</div>
            </div>
            <div id="jtab-today">
                <table>
                    <thead><tr><th>Time</th><th>Instrument</th><th>Type</th><th>Qty</th><th>P&L</th></tr></thead>
                    <tbody id="journal-today-body">
                        <tr><td colspan="5" style="text-align:center;color:#484f58;">No trades today</td></tr>
                    </tbody>
                </table>
            </div>
            <div id="jtab-history" style="display:none;">
                <div id="journal-daily-summary">
                    <div style="color:#484f58;text-align:center;padding:20px;">Loading...</div>
                </div>
            </div>
            <div id="jtab-analytics" style="display:none;">
                <div id="journal-analytics">
                    <div style="color:#484f58;text-align:center;padding:20px;">Loading...</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Intraday P&L Chart -->
    <div style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <h3>Intraday P&L</h3>
            <div style="position:relative;height:250px;width:100%;">
                <canvas id="pnl-chart"></canvas>
            </div>
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

        // Chart.js - loaded async, chart section hidden if unavailable
        loadScript('https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js', 8000, function(ok) {
            if (ok && typeof Chart !== 'undefined') {
                try { initPnlChart(); console.log('Chart.js initialized'); } catch(e) { console.warn('Chart init error:', e); }
            } else {
                console.warn('Chart.js not available - chart disabled');
            }
        });

        const QUICK_SL_OFFSETS = {{ quick_sl_offsets }};
        const QUICK_TP_OFFSETS = {{ quick_tp_offsets }};

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
                        var nowSec = Math.floor(Date.now() / 1000);
                        var barSec = nowSec - (nowSec % 60);
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

            // P&L cards
            var totalPnl = data.pnl.total;
            var totalEl = document.getElementById('total-pnl');
            totalEl.textContent = fmt(totalPnl);
            totalEl.className = 'value ' + (totalPnl >= 0 ? 'positive' : 'negative');
            document.getElementById('pnl-breakdown').textContent =
                'R: ' + fmt(data.pnl.realized) + ' | U: ' + fmt(data.pnl.unrealized);

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
            document.getElementById('trade-stats').textContent =
                data.trades.total + ' trades (W:' + data.trades.winners + ' L:' + data.trades.losers + ')';

            // Trailing drawdown
            if (data.trailing_drawdown.enabled) {
                document.getElementById('hwm-value').textContent = fmt(data.trailing_drawdown.high_water_mark);
                document.getElementById('drawdown-value').textContent = fmt(data.trailing_drawdown.current_drawdown);
                var ddLimit = data.trailing_drawdown.drawdown_limit;
                var ddCurrent = data.trailing_drawdown.current_drawdown;
                var ddPct = ddLimit > 0 ? Math.min(100, (ddCurrent / ddLimit) * 100) : 0;
                var ddBar = document.getElementById('drawdown-bar');
                ddBar.style.width = ddPct + '%';
                ddBar.style.background = ddPct > 70 ? '#f85149' : ddPct > 40 ? '#d29922' : '#3fb950';
                document.getElementById('drawdown-info').textContent =
                    'Limit: ' + fmt(ddLimit) + ' | Buffer: ' + fmt(data.trailing_drawdown.buffer);
            }

            // Positions table
            updatePositions(data.positions || [], data.sl_tp_orders || {});

            // Pending orders
            updatePendingOrders(data.pending_orders || []);

            // Recent orders (rejected/executed/cancelled)
            updateRecentOrders(data.recent_orders || []);

            // Spreads
            updateSpreads(data.spreads || []);

            // P&L chart
            updatePnlChart(data.pnl_chart || []);

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

        function updateSpreads(spreads) {
            var container = document.getElementById('spreads-container');
            if (spreads.length === 0) {
                container.innerHTML = '<div style="color:#484f58;text-align:center;padding:12px;">No spreads detected</div>';
                return;
            }
            var html = '';
            for (var i = 0; i < spreads.length; i++) {
                var s = spreads[i];
                var pnlClass = s.current_pnl >= 0 ? 'positive' : 'negative';
                html += '<div class="spread-card">';
                html += '<div class="spread-type">' + s.type.replace(/_/g, ' ') + '</div>';
                html += '<div style="display:flex;gap:24px;margin-bottom:8px;">';
                html += '<div><span style="color:#8b949e;">P&L:</span> <span class="' + pnlClass + '">' + fmt(s.current_pnl) + '</span></div>';
                html += '<div><span style="color:#8b949e;">Max Profit:</span> <span class="positive">' + fmt(s.max_profit) + '</span></div>';
                html += '<div><span style="color:#8b949e;">Max Loss:</span> <span class="negative">' + fmt(s.max_loss) + '</span></div>';
                html += '<div><span style="color:#8b949e;">Premium:</span> ' + fmt(s.net_premium) + '</div>';
                if (s.breakevens && s.breakevens.length > 0) {
                    html += '<div><span style="color:#8b949e;">BE:</span> ' + s.breakevens.map(function(b){return b.toFixed(0);}).join(', ') + '</div>';
                }
                html += '</div>';
                html += '<table><thead><tr><th>Type</th><th>Strike</th><th>Qty</th><th>Entry</th><th>LTP</th><th>P&L</th></tr></thead><tbody>';
                for (var j = 0; j < s.legs.length; j++) {
                    var leg = s.legs[j];
                    var legClass = leg.pnl >= 0 ? 'positive' : 'negative';
                    html += '<tr><td>' + leg.option_type + '</td><td>' + leg.strike + '</td><td>' + leg.qty + '</td>';
                    html += '<td>\\u20B9' + leg.entry.toFixed(2) + '</td><td>\\u20B9' + leg.ltp.toFixed(2) + '</td>';
                    html += '<td class="' + legClass + '">' + fmt(leg.pnl) + '</td></tr>';
                }
                html += '</tbody></table></div>';
            }
            container.innerHTML = html;
        }

        // ── P&L Chart ────────────────────────────────────────────────
        var pnlChart = null;
        function initPnlChart() {
            var ctx = document.getElementById('pnl-chart');
            if (!ctx || typeof Chart === 'undefined') return;
            pnlChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        { label: 'Total', data: [], borderColor: '#58a6ff', borderWidth: 2, fill: false, tension: 0.1, pointRadius: 0 },
                        { label: 'Realized', data: [], borderColor: '#3fb950', borderWidth: 1, fill: false, tension: 0.1, pointRadius: 0, borderDash: [5,3] },
                        { label: 'Unrealized', data: [], borderColor: '#d29922', borderWidth: 1, fill: false, tension: 0.1, pointRadius: 0, borderDash: [3,3] }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { labels: { color: '#8b949e', font: { size: 11 } } } },
                    scales: {
                        x: { ticks: { color: '#484f58', maxTicksLimit: 12, font: { size: 10 } }, grid: { color: '#161b22' } },
                        y: { ticks: { color: '#8b949e', callback: function(v){ return '\\u20B9' + v.toLocaleString('en-IN'); } }, grid: { color: '#21262d' } }
                    },
                    animation: { duration: 0 }
                }
            });
        }
        // Chart.js is now initialized via async CDN loader callback above

        function updatePnlChart(chartData) {
            if (!pnlChart || !chartData || chartData.length === 0) return;
            var data = chartData;
            if (data.length > 500) {
                var step = Math.ceil(data.length / 500);
                data = data.filter(function(_, i) { return i % step === 0 || i === data.length - 1; });
            }
            pnlChart.data.labels = data.map(function(p) {
                var d = new Date(p.timestamp * 1000);
                return d.getHours() + ':' + String(d.getMinutes()).padStart(2,'0');
            });
            pnlChart.data.datasets[0].data = data.map(function(p){ return p.total; });
            pnlChart.data.datasets[1].data = data.map(function(p){ return p.realized; });
            pnlChart.data.datasets[2].data = data.map(function(p){ return p.unrealized; });
            pnlChart.update('none');
        }

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

        function loadJournalHistory() {
            fetch('/api/journal/daily_summaries?days=30')
                .then(function(r){ return r.json(); })
                .then(function(data) {
                    var el = document.getElementById('journal-daily-summary');
                    if (!data || data.length === 0) {
                        el.innerHTML = '<div style="color:#484f58;text-align:center;padding:20px;">No historical data yet</div>';
                        return;
                    }
                    var html = '<table><thead><tr><th>Date</th><th>Trades</th><th>Win Rate</th><th>P&L</th><th>Gross Profit</th><th>Gross Loss</th><th>Avg Win</th><th>Avg Loss</th></tr></thead><tbody>';
                    for (var i = 0; i < data.length; i++) {
                        var d = data[i];
                        var pnlClass = d.total_pnl >= 0 ? 'positive' : 'negative';
                        html += '<tr>';
                        html += '<td>' + d.date + '</td>';
                        html += '<td>' + d.total_trades + ' <span style="color:#8b949e;font-size:11px;">(W:' + d.winners + ' L:' + d.losers + ')</span></td>';
                        html += '<td>' + d.win_rate.toFixed(1) + '%</td>';
                        html += '<td class="' + pnlClass + '">' + fmt(d.total_pnl) + '</td>';
                        html += '<td class="positive">' + fmt(d.gross_profit) + '</td>';
                        html += '<td class="negative">' + fmt(d.gross_loss) + '</td>';
                        html += '<td>' + fmt(d.avg_win) + '</td>';
                        html += '<td>' + fmt(d.avg_loss) + '</td>';
                        html += '</tr>';
                    }
                    html += '</tbody></table>';
                    el.innerHTML = html;
                })
                .catch(function(e) {
                    document.getElementById('journal-daily-summary').innerHTML =
                        '<div style="color:#f85149;text-align:center;padding:20px;">Failed to load history</div>';
                });
        }

        function loadJournalAnalytics() {
            fetch('/api/journal/analytics?days=30')
                .then(function(r){ return r.json(); })
                .then(function(data) {
                    var el = document.getElementById('journal-analytics');
                    if (!data || data.total_days === 0) {
                        el.innerHTML = '<div style="color:#484f58;text-align:center;padding:20px;">No data for analytics yet</div>';
                        return;
                    }
                    var html = '<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px;">';
                    var cards = [
                        { label: 'Total Days', val: data.total_days },
                        { label: 'Total Trades', val: data.total_trades },
                        { label: 'Overall P&L', val: fmt(data.overall_pnl), cls: data.overall_pnl >= 0 ? 'positive' : 'negative' },
                        { label: 'Win Rate', val: data.win_rate + '%' },
                        { label: 'Avg Win', val: fmt(data.avg_win), cls: 'positive' },
                        { label: 'Avg Loss', val: fmt(data.avg_loss), cls: 'negative' },
                        { label: 'Profitable Days', val: data.profitable_days, cls: 'positive' },
                        { label: 'Losing Days', val: data.losing_days, cls: 'negative' },
                        { label: 'Avg Daily P&L', val: fmt(data.avg_daily_pnl), cls: data.avg_daily_pnl >= 0 ? 'positive' : 'negative' },
                        { label: 'Best Day', val: data.best_day ? fmt(data.best_day.pnl) + ' (' + data.best_day.date + ')' : '-', cls: 'positive' },
                        { label: 'Worst Day', val: data.worst_day ? fmt(data.worst_day.pnl) + ' (' + data.worst_day.date + ')' : '-', cls: 'negative' },
                    ];
                    for (var i = 0; i < cards.length; i++) {
                        var c = cards[i];
                        html += '<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;text-align:center;">';
                        html += '<div style="font-size:11px;color:#8b949e;text-transform:uppercase;">' + c.label + '</div>';
                        html += '<div style="font-size:18px;font-weight:700;margin-top:4px;" class="' + (c.cls || '') + '">' + c.val + '</div>';
                        html += '</div>';
                    }
                    html += '</div>';

                    // P&L by hour
                    if (data.pnl_by_hour && data.pnl_by_hour.length > 0) {
                        html += '<h4 style="color:#c9d1d9;margin:16px 0 8px;">P&L by Hour</h4>';
                        html += '<table><thead><tr><th>Hour</th><th>P&L</th><th>Trades</th></tr></thead><tbody>';
                        for (var h = 0; h < data.pnl_by_hour.length; h++) {
                            var hr = data.pnl_by_hour[h];
                            var hClass = hr.total_pnl >= 0 ? 'positive' : 'negative';
                            html += '<tr><td>' + hr.hour + ':00</td><td class="' + hClass + '">' + fmt(hr.total_pnl) + '</td><td>' + hr.trade_count + '</td></tr>';
                        }
                        html += '</tbody></table>';
                    }

                    // P&L by instrument
                    if (data.pnl_by_instrument && data.pnl_by_instrument.length > 0) {
                        html += '<h4 style="color:#c9d1d9;margin:16px 0 8px;">P&L by Instrument</h4>';
                        html += '<table><thead><tr><th>Security ID</th><th>P&L</th><th>Trades</th><th>Winners</th></tr></thead><tbody>';
                        for (var inst = 0; inst < data.pnl_by_instrument.length; inst++) {
                            var ins = data.pnl_by_instrument[inst];
                            var iClass = ins.total_pnl >= 0 ? 'positive' : 'negative';
                            html += '<tr><td>' + (ins.instrument || ins.security_id) + '</td><td class="' + iClass + '">' + fmt(ins.total_pnl) + '</td><td>' + ins.trade_count + '</td><td>' + ins.winners + '</td></tr>';
                        }
                        html += '</tbody></table>';
                    }
                    el.innerHTML = html;
                })
                .catch(function(e) {
                    document.getElementById('journal-analytics').innerHTML =
                        '<div style="color:#f85149;text-align:center;padding:20px;">Failed to load analytics</div>';
                });
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
                                // Push live LTP into the current 1-min bar with proper OHLC
                                var nowSec = Math.floor(Date.now() / 1000);
                                var barSec = nowSec - (nowSec % 60);
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

            // Auto-qty from SL risk: lots = floor(maxLoss / ((sl - sellPrice) * lotSize))
            var qtyEl  = document.getElementById('sqb-qty');
            var lotsEl = document.getElementById('sqb-lots');
            var autoQty = 0;
            if (sl > sellPrice && sellPrice > 0 && maxLoss > 0) {
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
            _lwChart = LightweightCharts.createChart(container, {
                width:  container.clientWidth || 600,
                height: 480,
                layout: { background: {color:'#0d1117'}, textColor:'#8b949e' },
                grid:   { vertLines:{color:'#21262d'}, horzLines:{color:'#21262d'} },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                rightPriceScale: { borderColor:'#30363d' },
                timeScale: { borderColor:'#30363d', timeVisible:true, secondsVisible:false },
            });
            _lwSeries = _lwChart.addCandlestickSeries({
                upColor:'#3fb950', downColor:'#f85149',
                borderUpColor:'#3fb950', borderDownColor:'#f85149',
                wickUpColor:'#3fb950', wickDownColor:'#f85149',
            });
        }

        var _lwRefreshTimer = null;
        var _lwExchangeSegment = 'NSE_FNO';

        function loadChart(securityId, exchangeSegment) {
            _lwCurrentSecurity = securityId;
            _lwExchangeSegment = exchangeSegment || 'NSE_FNO';
            _liveBarTime = 0; _liveBarOpen = 0; _liveBarHigh = 0; _liveBarLow = Infinity;
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
                        _lwChart.timeScale().scrollToRealTime();
                    }
                })
                .catch(function(e) { console.error('Chart refresh error:', e); });
        }

        function switchDomTab(tab) {
            var depthDiv = document.getElementById('dom-chart');
            var chartDiv = document.getElementById('dom-chart-canvas');
            var depthBtn = document.getElementById('dom-tab-depth');
            var chartBtn = document.getElementById('dom-tab-chart');
            if (tab === 'chart') {
                depthDiv.style.display = 'none';
                chartDiv.style.display = 'block';
                depthBtn.style.fontWeight = '400'; depthBtn.style.borderBottomColor = 'transparent'; depthBtn.style.color = '#8b949e';
                chartBtn.style.fontWeight = '700'; chartBtn.style.borderBottomColor = '#3fb950';    chartBtn.style.color = '#3fb950';
                if (_lwChart) _lwChart.applyOptions({width: chartDiv.clientWidth || 600, height: 480});
            } else {
                depthDiv.style.display = 'block';
                chartDiv.style.display = 'none';
                depthBtn.style.fontWeight = '700'; depthBtn.style.borderBottomColor = '#3fb950';    depthBtn.style.color = '#3fb950';
                chartBtn.style.fontWeight = '400'; chartBtn.style.borderBottomColor = 'transparent'; chartBtn.style.color = '#8b949e';
            }
        }

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
    </script>
</body>
</html>
"""


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
        return jsonify({"status": "ok", "message": "State reset. Platform unlocked for today."})
    except Exception as e:
        logger.error("Admin unlock failed: %s", e)
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
                                for seg in outer.values():
                                    if isinstance(seg, dict):
                                        for sid, info in seg.items():
                                            result[str(sid)] = (info.get("last_price", 0) if isinstance(info, dict) else (info or 0)) or 0
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


# ── Order Placement ────────────────────────────────────────────────

@app.route("/api/order/place", methods=["POST"])
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
        # Fallback to API
        try:
            ltp_data = _monitor.api.get_ltp({exchange_segment: [security_id]})
            if isinstance(ltp_data, dict) and "data" in ltp_data:
                for key, val in ltp_data["data"].items():
                    if isinstance(val, dict):
                        ltp = val.get("last_price", 0)
                    else:
                        ltp = val
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
                # Parse IST string as-if-UTC so chart displays correct IST times
                try:
                    unix_ts = int(_dt.strptime(str(ts), "%Y-%m-%d %H:%M:%S").timestamp())
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


@app.route("/api/journal/daily_summaries")
def api_journal_daily_summaries():
    """Get daily P&L summaries for the last N days."""
    if not _monitor:
        return jsonify([])
    days = int(request.args.get("days", 30))
    return jsonify(_monitor.state.journal.get_daily_summaries(days=days))


@app.route("/api/journal/analytics")
def api_journal_analytics():
    """Get aggregated analytics."""
    if not _monitor:
        return jsonify({})
    days = int(request.args.get("days", 30))
    return jsonify(_monitor.state.journal.get_analytics(days=days))


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


@app.route("/journal")
def journal_page():
    # Redirect to trade-analyser running on port 5556
    host = request.host.split(":")[0]
    return redirect(f"http://{host}:5556", code=302)


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
    socketio.run(app, host=Config.DASHBOARD_HOST, port=Config.DASHBOARD_PORT,
                 debug=False, allow_unsafe_werkzeug=True)
