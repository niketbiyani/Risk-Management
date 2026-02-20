"""
Web Dashboard for the Trade Management Platform.
Real-time monitoring via Flask + SocketIO with auto-refresh.
Shows P&L, risk status, positions, spreads, order placement, and trade management controls.
"""

import json
import logging
import threading
import time
from datetime import date

from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO

from config import Config

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = "risk-mgmt-dashboard"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Reference to monitor instance and instrument cache (set at startup)
_monitor = None
_instrument_cache = None


def set_monitor(monitor):
    global _monitor
    _monitor = monitor


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

    <!-- Option Chain -->
    <div style="padding:0 24px;margin-top:16px;">
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <h3 style="margin:0;">Option Chain</h3>
                <div style="display:flex;gap:10px;align-items:center;">
                    <select id="oc-underlying" class="form-input" style="width:140px;padding:4px 8px;font-size:12px;">
                        <option value="13">NIFTY</option>
                        <option value="25">BANKNIFTY</option>
                    </select>
                    <select id="oc-expiry" class="form-input" style="width:140px;padding:4px 8px;font-size:12px;" onchange="loadOptionChain()">
                        <option value="">Loading...</option>
                    </select>
                    <button onclick="loadOptionChain()" class="btn-neutral" style="padding:4px 12px;font-size:12px;">Refresh</button>
                </div>
            </div>
            <div id="oc-spot" style="font-size:13px;color:#8b949e;margin-bottom:8px;">Spot: --</div>
            <div id="oc-scroll-container">
                <table id="oc-table" style="font-size:12px;">
                    <thead style="position:sticky;top:0;background:#0d1117;z-index:1;">
                        <tr>
                            <th style="text-align:right;color:#3fb950;">CE LTP</th>
                            <th style="text-align:center;font-weight:700;">Strike</th>
                            <th style="text-align:left;color:#f85149;">PE LTP</th>
                        </tr>
                    </thead>
                    <tbody id="oc-body">
                        <tr><td colspan="3" style="text-align:center;color:#484f58;padding:20px;">Loading option chain...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Order Placement Panel -->
    <div style="padding:0 24px;">
        <div class="card">
            <!-- Tab Headers -->
            <div style="display:flex;gap:0;border-bottom:1px solid #21262d;margin-bottom:16px;">
                <div class="order-tab active" data-tab="naked" onclick="switchOrderTab('naked')" style="padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid #58a6ff;color:#58a6ff;">Naked Order</div>
                <div class="order-tab" data-tab="spread" onclick="switchOrderTab('spread')" style="padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;">Spread Entry</div>
            </div>

            <!-- ═══ NAKED ORDER TAB ═══ -->
            <div id="tab-naked">
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

            <!-- ═══ SPREAD ENTRY TAB ═══ -->
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
                var isTP = data.action === 'TAKE_PROFIT';
                playAlert(isTP ? 'tp_hit' : 'sl_hit');
                var msg = data.action + ' triggered for ' + data.security_id +
                          ' @ \\u20B9' + (data.trigger_price || 0).toFixed(2) +
                          ' (LTP: \\u20B9' + (data.ltp || 0).toFixed(2) + ')';
                showToast(msg, isTP ? 'success' : 'error');
                showBrowserNotif(data.action, msg);
            });
            if (typeof safeUpdate === 'function') {
                socket.on('status_update', safeUpdate);
            }
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
                html += '<td>';
                html += '<button class="btn-sl btn-sm" data-action="set-sl" data-idx="' + i + '">SL</button> ';
                html += '<button class="btn-tp btn-sm" data-action="set-tp" data-idx="' + i + '">TP</button> ';
                html += '<button class="btn-danger btn-sm" data-action="exit" data-idx="' + i + '">EXIT</button>';
                html += '</td>';
                html += '</tr>';

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
            } else if (action === 'exit') {
                exitPosition(sid, exSeg, prodType, qty, dir);
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
                            html += '<tr><td>' + ins.security_id + '</td><td class="' + iClass + '">' + fmt(ins.total_pnl) + '</td><td>' + ins.trade_count + '</td><td>' + ins.winners + '</td></tr>';
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
            if (confirm('Exit this position at market?')) {
                fetch('/api/exit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({security_id: sid, exchange_segment: exSeg, product_type: prodType, quantity: qty, direction: direction})
                }).then(function(r){ return r.json(); }).then(function(d) {
                    if (d.status === 'ok') {
                        playAlert('order');
                        showToast('Exit order placed', 'success');
                    } else {
                        showToast('Exit failed: ' + (d.message || ''), 'error');
                    }
                });
            }
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

        function loadOptionChain() {
            var sel = document.getElementById('oc-underlying');
            var underlying = sel.options[sel.selectedIndex].text;
            var expiry = document.getElementById('oc-expiry').value;
            if (!expiry) { initOptionChain(); return; }

            var body = document.getElementById('oc-body');
            body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#484f58;padding:20px;">Loading...</td></tr>';

            fetch('/api/option_chain/data?underlying=' + underlying + '&expiry=' + expiry)
                .then(function(r){ return r.json(); })
                .then(function(d) {
                    if (d.error) {
                        body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#f85149;padding:20px;">' + d.error + '</td></tr>';
                        return;
                    }
                    var spot = d.spot || 0;
                    _ocLotSize = d.lot_size || 75;
                    document.getElementById('oc-spot').textContent = 'Spot: \\u20B9' + (spot > 0 ? spot.toFixed(2) : '--') + '  |  Expiry: ' + expiry + '  |  Lot: ' + _ocLotSize;

                    var chain = d.chain || [];
                    if (chain.length === 0) {
                        body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#484f58;padding:20px;">No strikes found</td></tr>';
                        return;
                    }

                    // ATM is the middle row (backend already trimmed to +/- 6)
                    var atmIdx = Math.floor(chain.length / 2);

                    var html = '';
                    chain.forEach(function(row, i) {
                        var isATM = (i === atmIdx);
                        var rowStyle = isATM ? 'background:#1a2332;' : '';
                        var ceLtp = row.ce_ltp ? row.ce_ltp.toFixed(2) : '-';
                        var peLtp = row.pe_ltp ? row.pe_ltp.toFixed(2) : '-';

                        html += '<tr style="cursor:pointer;' + rowStyle + '">';
                        html += '<td style="text-align:right;color:#3fb950;font-weight:600;padding:8px 16px;" ';
                        html += 'onclick="ocSelect(\\'' + row.ce_security_id + '\\',\\'CE\\',\\'' + expiry + '\\',' + row.strike + ',' + (row.ce_ltp || 0) + ')">';
                        html += ceLtp + '</td>';
                        html += '<td style="text-align:center;font-weight:700;color:#e6edf3;background:#161b22;border-left:2px solid #30363d;border-right:2px solid #30363d;padding:8px 16px;">' + row.strike.toFixed(0) + '</td>';
                        html += '<td style="text-align:left;color:#f85149;font-weight:600;padding:8px 16px;" ';
                        html += 'onclick="ocSelect(\\'' + row.pe_security_id + '\\',\\'PE\\',\\'' + expiry + '\\',' + row.strike + ',' + (row.pe_ltp || 0) + ')">';
                        html += peLtp + '</td>';
                        html += '</tr>';
                    });
                    body.innerHTML = html;
                })
                .catch(function(e) {
                    body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#f85149;padding:20px;">Error: ' + e + '</td></tr>';
                });
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

            // Scroll to order panel
            document.querySelector('.order-tab').scrollIntoView({behavior: 'smooth', block: 'start'});
            showToast('Selected: ' + symbol + ' @ \\u20B9' + (ltp ? ltp.toFixed(2) : '--'), 'success');
        }

        document.getElementById('oc-underlying').addEventListener('change', function() {
            initOptionChain();
        });

        initOptionChain();
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
    return jsonify({"status": "ok", "instruments_loaded": count})


# ── Option Chain ───────────────────────────────────────────────────

@app.route("/api/option_chain/expiries")
def api_option_chain_expiries():
    """Get expiry dates for an underlying from instrument cache."""
    if not _instrument_cache:
        return jsonify({"error": "Instrument cache not loaded"}), 500
    underlying = request.args.get("underlying", "NIFTY").upper()
    try:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        expiries = set()
        for inst in _instrument_cache._instruments:
            if inst.instrument_type != "OPTIDX":
                continue
            if not inst.trading_symbol.upper().startswith(underlying + "-"):
                continue
            if inst.expiry_date:
                exp_date = inst.expiry_date[:10]  # "2026-03-30 14:30:00" -> "2026-03-30"
                if exp_date >= today:
                    expiries.add(exp_date)
        sorted_expiries = sorted(expiries)[:2]
        return jsonify({"expiries": sorted_expiries})
    except Exception as e:
        logger.error("Failed to get expiries: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/option_chain/data")
def api_option_chain_data():
    """Build option chain from instrument cache, optionally fetch LTPs."""
    if not _instrument_cache:
        return jsonify({"error": "Instrument cache not loaded"}), 500
    underlying = request.args.get("underlying", "NIFTY").upper()
    expiry = request.args.get("expiry", "")
    if not expiry:
        return jsonify({"error": "expiry parameter required"}), 400
    try:
        # Build chain from instrument cache
        strikes = {}
        lot_size = 75
        for inst in _instrument_cache._instruments:
            if inst.instrument_type != "OPTIDX":
                continue
            if not inst.trading_symbol.upper().startswith(underlying + "-"):
                continue
            if not inst.expiry_date or inst.expiry_date[:10] != expiry:
                continue
            if inst.exchange != "NSE":
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

        # Use Dhan's option_chain API to get spot + LTPs in one call
        spot = 0
        if _monitor:
            try:
                underlying_id = 13 if underlying == "NIFTY" else 25
                oc_result = _monitor.api.get_option_chain(underlying_id, expiry)
                if isinstance(oc_result, dict) and oc_result.get("status") == "success":
                    oc_data = oc_result.get("data", {})
                    spot = oc_data.get("last_price", 0) or 0
                    oc_strikes = oc_data.get("oc", {})
                    # Map LTPs from option chain into our chain
                    ltp_by_strike = {}
                    for strike_str, sides in oc_strikes.items():
                        try:
                            s = float(strike_str)
                        except (ValueError, TypeError):
                            continue
                        ce = sides.get("ce", {}) or {}
                        pe = sides.get("pe", {}) or {}
                        ltp_by_strike[s] = {
                            "ce_ltp": ce.get("ltp", 0) or 0,
                            "pe_ltp": pe.get("ltp", 0) or 0,
                        }
                    for row in chain:
                        strike_data = ltp_by_strike.get(row["strike"])
                        if strike_data:
                            row["ce_ltp"] = strike_data["ce_ltp"]
                            row["pe_ltp"] = strike_data["pe_ltp"]
            except Exception as e:
                logger.debug("Option chain API failed: %s", e)

        # Find ATM index
        atm_idx = len(chain) // 2
        if spot > 0:
            min_dist = float("inf")
            for i, row in enumerate(chain):
                d = abs(row["strike"] - spot)
                if d < min_dist:
                    min_dist = d
                    atm_idx = i

        # Trim to ATM +/- 6 strikes (13 total)
        start = max(0, atm_idx - 6)
        end = min(len(chain), atm_idx + 7)
        chain = chain[start:end]

        return jsonify({"spot": spot, "chain": chain, "expiry": expiry, "lot_size": lot_size})
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
            product_type=data.get("product_type", "INTRADAY"),
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
            product_type=data.get("product_type", "INTRADAY"),
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

@app.route("/api/order/place_spread", methods=["POST"])
def api_place_spread():
    """Queue a pending spread order (hedge buy at market + sell at limit)."""
    if not _monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    data = request.json

    required = ["sell_security_id", "buy_security_id", "sell_price",
                 "sell_trigger_price", "quantity"]
    for field in required:
        if not data.get(field):
            return jsonify({"status": "error", "message": f"Missing {field}"}), 400

    try:
        spread_id = _monitor.trade_mgr.add_pending_spread(data)
        return jsonify({"status": "ok", "spread_id": spread_id})
    except Exception as e:
        logger.error("Failed to create spread order: %s", e)
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


def run_dashboard(monitor):
    """Start the dashboard web server."""
    set_monitor(monitor)
    socketio.run(app, host=Config.DASHBOARD_HOST, port=Config.DASHBOARD_PORT,
                 debug=False, allow_unsafe_werkzeug=True)
