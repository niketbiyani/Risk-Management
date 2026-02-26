"""
DOM & Bookmap Analyzer — Standalone Flask App.

Provides 20-level Depth of Market (DOM) and Bookmap liquidity heatmap
for Nifty futures via Dhan's WebSocket feed.

Runs independently on its own port (default 5556).
Shares Dhan credentials from the parent Risk-Management/.env.
"""

import logging
import os
import sys

from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO

from config import Config
from depth_engine import DepthEngine

# ── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Flask App ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = "dom-bookmap-analyzer"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_engine: DepthEngine | None = None


# ══════════════════════════════════════════════════════════════════
# HTML Template — DOM + Bookmap (single-page, two tabs)
# ══════════════════════════════════════════════════════════════════

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <title>DOM & Bookmap Analyzer</title>
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
            padding: 12px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { font-size: 18px; font-weight: 600; color: #f0f6fc; }
        .header .conn-badge {
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .conn-on { background: #0d4429; color: #3fb950; border: 1px solid #238636; }
        .conn-off { background: #4a1d1d; color: #f85149; border: 1px solid #da3633; }

        .tabs {
            display: flex;
            gap: 0;
            margin-left: 20px;
        }
        .tab {
            padding: 6px 18px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            border-bottom: 2px solid transparent;
            color: #8b949e;
            transition: all 0.2s;
        }
        .tab.active {
            border-bottom-color: #58a6ff;
            color: #58a6ff;
        }
        .tab:hover:not(.active) { color: #c9d1d9; }

        .page { display: none; }
        .page.active { display: block; }

        .grid { display: grid; gap: 16px; padding: 20px 24px; }
        .grid-4 { grid-template-columns: repeat(4, 1fr); }
        .grid-2 { grid-template-columns: repeat(2, 1fr); }

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
        .value { font-size: 24px; font-weight: 700; }
        .sub { font-size: 12px; color: #8b949e; margin-top: 2px; }

        .footer {
            text-align: center;
            padding: 12px;
            font-size: 11px;
            color: #484f58;
            border-top: 1px solid #21262d;
        }
    </style>
    <script>
        function switchTab(tab) {
            document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
            document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
            var el = document.getElementById('page-' + tab);
            if (el) el.classList.add('active');
            var tabEl = document.querySelector('.tab[data-tab="' + tab + '"]');
            if (tabEl) tabEl.classList.add('active');
        }
    </script>
</head>
<body>
    <div class="header">
        <div style="display:flex;align-items:center;">
            <h1>DOM & Bookmap</h1>
            <div class="tabs">
                <div class="tab active" data-tab="dom" onclick="switchTab('dom')">Market Depth</div>
                <div class="tab" data-tab="bookmap" onclick="switchTab('bookmap')">Bookmap</div>
            </div>
        </div>
        <div style="display:flex;align-items:center;gap:12px;">
            <span id="hdr-symbol" style="font-size:14px;font-weight:700;color:#58a6ff;">--</span>
            <span id="hdr-ltp" style="font-size:14px;font-weight:600;">--</span>
            <div id="conn-badge" class="conn-badge conn-off">Disconnected</div>
        </div>
    </div>

    <!-- ═══ PAGE: DOM (20-Level Depth) ═══ -->
    <div id="page-dom" class="page active">
        <div style="padding:20px 24px;">

            <!-- Top Stats Row -->
            <div class="grid grid-4" style="margin-bottom:16px;">
                <div class="card">
                    <h3>Symbol</h3>
                    <div class="value" id="depth-symbol" style="font-size:20px;color:#58a6ff;">--</div>
                    <div class="sub" id="depth-expiry">Expiry: --</div>
                </div>
                <div class="card">
                    <h3>Last Price</h3>
                    <div class="value" id="depth-ltp" style="font-size:22px;">--</div>
                    <div class="sub" id="depth-spread">Spread: --</div>
                </div>
                <div class="card">
                    <h3>Total Bid Qty</h3>
                    <div class="value" id="depth-total-bid" style="font-size:22px;color:#3fb950;">--</div>
                    <div class="sub">Buy side (all 20 levels)</div>
                </div>
                <div class="card">
                    <h3>Total Ask Qty</h3>
                    <div class="value" id="depth-total-ask" style="font-size:22px;color:#f85149;">--</div>
                    <div class="sub">Sell side (all 20 levels)</div>
                </div>
            </div>

            <!-- Imbalance Meters -->
            <div class="card" style="margin-bottom:16px;padding:16px 20px;">
                <h3 style="margin-bottom:12px;">Order Book Imbalance</h3>
                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">
                    <div>
                        <div style="font-size:11px;color:#8b949e;margin-bottom:6px;">5-Level Depth</div>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <div style="flex:1;height:10px;background:#21262d;border-radius:5px;overflow:hidden;position:relative;">
                                <div id="imb5-bid-bar" style="position:absolute;right:50%;height:100%;background:#3fb950;border-radius:5px 0 0 5px;transition:width 0.3s;width:0;"></div>
                                <div id="imb5-ask-bar" style="position:absolute;left:50%;height:100%;background:#f85149;border-radius:0 5px 5px 0;transition:width 0.3s;width:0;"></div>
                                <div style="position:absolute;left:50%;top:0;width:1px;height:100%;background:#484f58;"></div>
                            </div>
                            <span id="imb5-pct" style="font-size:13px;font-weight:600;min-width:70px;text-align:right;">--</span>
                        </div>
                        <div id="imb5-text" style="font-size:11px;color:#8b949e;margin-top:2px;">--</div>
                    </div>
                    <div>
                        <div style="font-size:11px;color:#8b949e;margin-bottom:6px;">10-Level Depth</div>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <div style="flex:1;height:10px;background:#21262d;border-radius:5px;overflow:hidden;position:relative;">
                                <div id="imb10-bid-bar" style="position:absolute;right:50%;height:100%;background:#3fb950;border-radius:5px 0 0 5px;transition:width 0.3s;width:0;"></div>
                                <div id="imb10-ask-bar" style="position:absolute;left:50%;height:100%;background:#f85149;border-radius:0 5px 5px 0;transition:width 0.3s;width:0;"></div>
                                <div style="position:absolute;left:50%;top:0;width:1px;height:100%;background:#484f58;"></div>
                            </div>
                            <span id="imb10-pct" style="font-size:13px;font-weight:600;min-width:70px;text-align:right;">--</span>
                        </div>
                        <div id="imb10-text" style="font-size:11px;color:#8b949e;margin-top:2px;">--</div>
                    </div>
                    <div>
                        <div style="font-size:11px;color:#8b949e;margin-bottom:6px;">20-Level Depth (Full)</div>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <div style="flex:1;height:10px;background:#21262d;border-radius:5px;overflow:hidden;position:relative;">
                                <div id="imb20-bid-bar" style="position:absolute;right:50%;height:100%;background:#3fb950;border-radius:5px 0 0 5px;transition:width 0.3s;width:0;"></div>
                                <div id="imb20-ask-bar" style="position:absolute;left:50%;height:100%;background:#f85149;border-radius:0 5px 5px 0;transition:width 0.3s;width:0;"></div>
                                <div style="position:absolute;left:50%;top:0;width:1px;height:100%;background:#484f58;"></div>
                            </div>
                            <span id="imb20-pct" style="font-size:13px;font-weight:600;min-width:70px;text-align:right;">--</span>
                        </div>
                        <div id="imb20-text" style="font-size:11px;color:#8b949e;margin-top:2px;">--</div>
                    </div>
                </div>
            </div>

            <!-- Order Book (Bid / Ask side by side) -->
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0;margin-bottom:16px;">
                <!-- Bid Side -->
                <div class="card" style="border-radius:12px 0 0 12px;border-right:none;padding:0;overflow:hidden;">
                    <div style="padding:10px 16px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between;align-items:center;">
                        <h3 style="color:#3fb950;font-size:13px;">BID (Buyers)</h3>
                        <span style="font-size:11px;color:#8b949e;">Orders | Qty | Price</span>
                    </div>
                    <div style="max-height:520px;overflow-y:auto;">
                        <table style="width:100%;border-collapse:collapse;">
                            <tbody id="depth-bid-body"></tbody>
                        </table>
                    </div>
                </div>
                <!-- Ask Side -->
                <div class="card" style="border-radius:0 12px 12px 0;padding:0;overflow:hidden;">
                    <div style="padding:10px 16px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between;align-items:center;">
                        <h3 style="color:#f85149;font-size:13px;">ASK (Sellers)</h3>
                        <span style="font-size:11px;color:#8b949e;">Price | Qty | Orders</span>
                    </div>
                    <div style="max-height:520px;overflow-y:auto;">
                        <table style="width:100%;border-collapse:collapse;">
                            <tbody id="depth-ask-body"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Analytics Row: Delta + Alerts -->
            <div class="grid grid-2">
                <!-- Cumulative Delta -->
                <div class="card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <h3>Cumulative Delta</h3>
                        <div id="depth-delta-value" style="font-size:18px;font-weight:700;">--</div>
                    </div>
                    <div style="font-size:11px;color:#8b949e;margin-bottom:8px;">
                        Rising = buyers aggressive | Falling = sellers aggressive
                    </div>
                    <canvas id="depth-delta-canvas" style="width:100%;height:100px;"></canvas>
                </div>
                <!-- Wall Alerts -->
                <div class="card">
                    <h3 style="margin-bottom:8px;">Wall Alerts</h3>
                    <div style="font-size:11px;color:#8b949e;margin-bottom:8px;">
                        Absorption = wall being eaten | Pull = wall removed before hit
                    </div>
                    <div id="depth-alerts-feed" style="max-height:180px;overflow-y:auto;font-size:12px;font-family:monospace;">
                        <div style="color:#484f58;padding:4px 0;">Waiting for data...</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- ═══ PAGE: BOOKMAP (Liquidity Heatmap + Trade Bubbles) ═══ -->
    <div id="page-bookmap" class="page">
        <div style="padding:20px 24px;">

            <!-- Controls Row -->
            <div class="card" style="margin-bottom:12px;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;">
                <div style="display:flex;align-items:center;gap:16px;">
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span style="font-size:12px;color:#8b949e;">Window:</span>
                        <button class="bm-time-btn active" data-window="120" onclick="bmSetWindow(120)" style="padding:3px 10px;font-size:11px;background:#21262d;border:1px solid #30363d;color:#e0e6ed;border-radius:4px;cursor:pointer;">2m</button>
                        <button class="bm-time-btn" data-window="300" onclick="bmSetWindow(300)" style="padding:3px 10px;font-size:11px;background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:4px;cursor:pointer;">5m</button>
                        <button class="bm-time-btn" data-window="600" onclick="bmSetWindow(600)" style="padding:3px 10px;font-size:11px;background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:4px;cursor:pointer;">10m</button>
                    </div>
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span style="font-size:12px;color:#8b949e;">Zoom:</span>
                        <button onclick="bmZoom(1)" style="padding:3px 8px;font-size:11px;background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:4px;cursor:pointer;">+</button>
                        <button onclick="bmZoom(-1)" style="padding:3px 8px;font-size:11px;background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:4px;cursor:pointer;">-</button>
                        <button onclick="bmReset()" style="padding:3px 8px;font-size:11px;background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:4px;cursor:pointer;">Reset</button>
                    </div>
                </div>
                <div style="display:flex;align-items:center;gap:16px;">
                    <div style="display:flex;align-items:center;gap:4px;">
                        <span id="bm-symbol" style="font-size:14px;font-weight:700;color:#58a6ff;">--</span>
                        <span id="bm-ltp" style="font-size:14px;font-weight:600;color:#e0e6ed;">--</span>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px;font-size:11px;">
                        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;"></span><span style="color:#8b949e;">Buy</span>
                        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#f85149;"></span><span style="color:#8b949e;">Sell</span>
                        <span style="display:inline-block;width:8px;height:3px;background:#58a6ff;"></span><span style="color:#8b949e;">LTP</span>
                    </div>
                </div>
            </div>

            <!-- Bookmap Canvas + Volume Profile -->
            <div style="display:flex;gap:0;margin-bottom:16px;">
                <div class="card" style="flex:1;padding:0;border-radius:12px 0 0 12px;border-right:none;overflow:hidden;position:relative;">
                    <canvas id="bm-canvas" style="width:100%;height:480px;cursor:crosshair;display:block;"></canvas>
                    <div id="bm-tooltip" style="display:none;position:absolute;background:rgba(13,17,23,0.92);border:1px solid #30363d;border-radius:6px;padding:6px 10px;font-size:11px;font-family:monospace;color:#e0e6ed;pointer-events:none;z-index:10;white-space:nowrap;"></div>
                </div>
                <div class="card" style="width:140px;padding:0;border-radius:0 12px 12px 0;overflow:hidden;position:relative;">
                    <div style="padding:6px 10px;border-bottom:1px solid #21262d;text-align:center;">
                        <span style="font-size:10px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:1px;">Vol Profile</span>
                    </div>
                    <canvas id="bm-volprofile" style="width:100%;height:456px;display:block;"></canvas>
                </div>
            </div>

            <!-- Stats Row: Delta + Tick Summary -->
            <div class="grid grid-2">
                <div class="card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <h3>Cumulative Delta</h3>
                        <div id="bm-delta-value" style="font-size:18px;font-weight:700;">--</div>
                    </div>
                    <div style="font-size:11px;color:#8b949e;margin-bottom:8px;">
                        Rising = buyers aggressive | Falling = sellers aggressive
                    </div>
                    <canvas id="bm-delta-canvas" style="width:100%;height:80px;"></canvas>
                </div>
                <div class="card">
                    <h3 style="margin-bottom:8px;">Trade Flow</h3>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
                        <div style="text-align:center;">
                            <div style="font-size:10px;color:#8b949e;margin-bottom:2px;">Buy Volume</div>
                            <div id="bm-buy-vol" style="font-size:20px;font-weight:700;color:#3fb950;">0</div>
                        </div>
                        <div style="text-align:center;">
                            <div style="font-size:10px;color:#8b949e;margin-bottom:2px;">Sell Volume</div>
                            <div id="bm-sell-vol" style="font-size:20px;font-weight:700;color:#f85149;">0</div>
                        </div>
                    </div>
                    <div style="height:8px;background:#21262d;border-radius:4px;overflow:hidden;display:flex;">
                        <div id="bm-buy-bar" style="background:#3fb950;height:100%;transition:width 0.3s;width:50%;"></div>
                        <div id="bm-sell-bar" style="background:#f85149;height:100%;transition:width 0.3s;width:50%;"></div>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin-top:4px;">
                        <span id="bm-buy-pct" style="font-size:11px;color:#3fb950;">50%</span>
                        <span id="bm-sell-pct" style="font-size:11px;color:#f85149;">50%</span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="footer">
        DOM & Bookmap Analyzer | Dhan 20-Level Depth Feed | Standalone
    </div>

    <script>
        // ── CDN Script Loader ──
        function loadScript(url, timeout, onReady) {
            var done = false;
            var s = document.createElement('script');
            s.src = url;
            s.async = true;
            s.onload = function() { if (!done) { done = true; onReady(true); } };
            s.onerror = function() { if (!done) { done = true; onReady(false); } };
            setTimeout(function() { if (!done) { done = true; onReady(false); } }, timeout);
            document.head.appendChild(s);
        }

        var socket = null;
        loadScript('https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js', 8000, function(ok) {
            if (ok && typeof io !== 'undefined') {
                socket = io();
                socket.on('depth_update', function(data) {
                    handleDepthUpdate(data);
                    handleBookmapIngest(data);
                });
            } else {
                // Fallback: poll snapshot
                setInterval(function() {
                    fetch('/api/depth/snapshot').then(function(r) { return r.json(); }).then(function(d) {
                        if (d && d.symbol) { handleDepthUpdate(d); handleBookmapIngest(d); }
                    }).catch(function() {});
                }, 1000);
            }
        });

        // ── Helpers ──
        function fmtQty(n) { return n ? n.toLocaleString('en-IN') : '0'; }
        function fmtPrice(p) { return p ? p.toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '--'; }
        function setT(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; }

        // ══════════════════════════════════════════════════════════
        // DOM (Market Depth) Tab
        // ══════════════════════════════════════════════════════════

        var deltaCanvas = document.getElementById('depth-delta-canvas');

        function updateImbalance(prefix, data) {
            var pctEl = document.getElementById(prefix + '-pct');
            var textEl = document.getElementById(prefix + '-text');
            var bidBar = document.getElementById(prefix + '-bid-bar');
            var askBar = document.getElementById(prefix + '-ask-bar');
            if (!pctEl || !data) return;

            var pct = data.pct || 0;
            var color = pct > 5 ? '#3fb950' : pct < -5 ? '#f85149' : '#8b949e';
            pctEl.textContent = (pct > 0 ? '+' : '') + pct.toFixed(1) + '%';
            pctEl.style.color = color;
            textEl.textContent = data.text || '';

            var absPct = Math.min(Math.abs(pct), 100);
            var barWidth = (absPct / 100) * 50;
            if (pct >= 0) {
                bidBar.style.width = barWidth + '%';
                askBar.style.width = '0%';
            } else {
                bidBar.style.width = '0%';
                askBar.style.width = barWidth + '%';
            }
        }

        function renderBookSide(bodyId, levels, side, walls) {
            var body = document.getElementById(bodyId);
            if (!body || !levels) return;

            var maxQty = 0;
            levels.forEach(function(l) { if (l.qty > maxQty) maxQty = l.qty; });

            var wallPrices = {};
            if (walls) {
                walls.forEach(function(w) { wallPrices[w.price] = w.strength; });
            }

            var html = '';
            levels.forEach(function(l) {
                if (l.price <= 0) return;
                var intensity = maxQty > 0 ? (l.qty / maxQty) : 0;
                var barPct = (intensity * 100).toFixed(1);
                var isWall = wallPrices[l.price];

                var barColor;
                if (side === 'bid') {
                    barColor = isWall
                        ? 'rgba(63,185,80,' + (0.25 + intensity * 0.45) + ')'
                        : 'rgba(63,185,80,' + (0.08 + intensity * 0.30) + ')';
                } else {
                    barColor = isWall
                        ? 'rgba(248,81,73,' + (0.25 + intensity * 0.45) + ')'
                        : 'rgba(248,81,73,' + (0.08 + intensity * 0.30) + ')';
                }

                var barGradient = side === 'bid'
                    ? 'linear-gradient(to left, ' + barColor + ' ' + barPct + '%, transparent ' + barPct + '%)'
                    : 'linear-gradient(to right, ' + barColor + ' ' + barPct + '%, transparent ' + barPct + '%)';

                var wallBorder = isWall ? 'border-' + (side === 'bid' ? 'right' : 'left') + ':3px solid ' + (side === 'bid' ? '#3fb950' : '#f85149') + ';' : '';
                var tdStyle = 'padding:3px 8px;font-family:monospace;font-size:12px;white-space:nowrap;position:relative;z-index:1;';
                var wallBadge = isWall ? ' <span style="font-size:9px;background:' + (side === 'bid' ? 'rgba(63,185,80,0.2)' : 'rgba(248,81,73,0.2)') + ';color:' + (side === 'bid' ? '#3fb950' : '#f85149') + ';padding:1px 4px;border-radius:3px;font-weight:700;">' + isWall.toFixed(1) + 'x</span>' : '';

                var rowStyle = 'background:' + barGradient + ';' + wallBorder + (isWall ? 'font-weight:700;' : '');

                if (side === 'bid') {
                    html += '<tr style="' + rowStyle + '">'
                        + '<td style="' + tdStyle + 'text-align:right;color:#8b949e;width:22%;">' + fmtQty(l.orders) + '</td>'
                        + '<td style="' + tdStyle + 'text-align:right;color:#3fb950;width:40%;">' + fmtQty(l.qty) + wallBadge + '</td>'
                        + '<td style="' + tdStyle + 'text-align:right;color:#e0e6ed;width:38%;">' + fmtPrice(l.price) + '</td>'
                        + '</tr>';
                } else {
                    html += '<tr style="' + rowStyle + '">'
                        + '<td style="' + tdStyle + 'color:#e0e6ed;width:38%;">' + fmtPrice(l.price) + '</td>'
                        + '<td style="' + tdStyle + 'color:#f85149;width:40%;">' + fmtQty(l.qty) + wallBadge + '</td>'
                        + '<td style="' + tdStyle + 'text-align:right;color:#8b949e;width:22%;">' + fmtQty(l.orders) + '</td>'
                        + '</tr>';
                }
            });

            body.innerHTML = html;
        }

        function drawDomDeltaChart(history) {
            if (!deltaCanvas) return;
            var ctx = deltaCanvas.getContext('2d');
            var w = deltaCanvas.width = deltaCanvas.offsetWidth;
            var h = deltaCanvas.height = 100;
            ctx.clearRect(0, 0, w, h);

            if (!history || history.length < 2) {
                ctx.fillStyle = '#484f58'; ctx.font = '12px monospace';
                ctx.fillText('Collecting data...', 10, h / 2);
                return;
            }

            var values = history.map(function(d) { return d.d; });
            var min = Math.min.apply(null, values);
            var max = Math.max.apply(null, values);
            var range = max - min; if (range === 0) range = 1;

            var zeroY = h - ((0 - min) / range) * (h - 10) - 5;
            ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
            ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(w, zeroY); ctx.stroke();
            ctx.setLineDash([]);

            var lastVal = values[values.length - 1];
            ctx.fillStyle = lastVal >= 0 ? 'rgba(63,185,80,0.1)' : 'rgba(248,81,73,0.1)';
            ctx.beginPath(); ctx.moveTo(0, zeroY);
            for (var i = 0; i < values.length; i++) {
                ctx.lineTo((i / (values.length - 1)) * w, h - ((values[i] - min) / range) * (h - 10) - 5);
            }
            ctx.lineTo(w, zeroY); ctx.closePath(); ctx.fill();

            ctx.strokeStyle = lastVal >= 0 ? '#3fb950' : '#f85149'; ctx.lineWidth = 1.5;
            ctx.beginPath();
            for (var j = 0; j < values.length; j++) {
                var lx = (j / (values.length - 1)) * w;
                var ly = h - ((values[j] - min) / range) * (h - 10) - 5;
                if (j === 0) ctx.moveTo(lx, ly); else ctx.lineTo(lx, ly);
            }
            ctx.stroke();
        }

        function renderAlerts(alerts) {
            var feed = document.getElementById('depth-alerts-feed');
            if (!feed || !alerts || alerts.length === 0) return;

            var html = '';
            var now = Date.now() / 1000;
            alerts.forEach(function(a) {
                var ago = Math.round(now - a.time);
                var agoStr = ago < 60 ? ago + 's ago' : Math.round(ago / 60) + 'm ago';
                var icon, msg, color;

                if (a.type === 'absorption') {
                    icon = a.side === 'bid' ? '&#9660;' : '&#9650;';
                    color = a.side === 'bid' ? '#f85149' : '#3fb950';
                    msg = (a.side === 'bid' ? 'Bid' : 'Ask') + ' wall at '
                        + fmtPrice(a.price) + ' absorbed ' + a.pct + '% ('
                        + fmtQty(a.prev_qty) + '&rarr;' + fmtQty(a.curr_qty) + ')';
                } else if (a.type === 'stacking') {
                    icon = '&#9650;&#9650;';
                    color = a.side === 'bid' ? '#3fb950' : '#f85149';
                    msg = (a.side === 'bid' ? 'Bid' : 'Ask') + ' stacking at '
                        + fmtPrice(a.price) + ' (' + fmtQty(a.qty) + ', '
                        + (a.strength || 0) + 'x avg)';
                } else {
                    icon = '&#10005;';
                    color = '#d29922';
                    msg = (a.side === 'bid' ? 'Bid' : 'Ask') + ' wall at '
                        + fmtPrice(a.price) + ' pulled (' + fmtQty(a.qty) + ' removed)';
                }

                html += '<div style="padding:4px 0;border-bottom:1px solid #21262d;display:flex;justify-content:space-between;">'
                    + '<span><span style="color:' + color + ';">' + icon + '</span> ' + msg + '</span>'
                    + '<span style="color:#484f58;min-width:50px;text-align:right;">' + agoStr + '</span>'
                    + '</div>';
            });
            feed.innerHTML = html;
        }

        function handleDepthUpdate(data) {
            if (!data) return;

            // Header
            setT('hdr-symbol', data.symbol || '--');
            setT('hdr-ltp', data.ltp ? fmtPrice(data.ltp) : '--');
            var badge = document.getElementById('conn-badge');
            if (badge) {
                badge.textContent = data.connected ? 'Connected' : 'Disconnected';
                badge.className = 'conn-badge ' + (data.connected ? 'conn-on' : 'conn-off');
            }

            // Top stats
            setT('depth-symbol', data.symbol || '--');
            var expEl = document.getElementById('depth-expiry');
            if (expEl) expEl.textContent = 'Expiry: ' + (data.expiry || '--') + '  |  Lot: ' + (data.lot_size || '--');
            setT('depth-ltp', data.ltp ? fmtPrice(data.ltp) : '--');
            var spreadEl = document.getElementById('depth-spread');
            if (spreadEl) spreadEl.textContent = 'Spread: ' + (data.spread != null ? fmtPrice(data.spread) : '--');
            setT('depth-total-bid', fmtQty(data.total_bid_qty));
            setT('depth-total-ask', fmtQty(data.total_ask_qty));

            // Imbalance
            updateImbalance('imb5', data.imbalance_5);
            updateImbalance('imb10', data.imbalance_10);
            updateImbalance('imb20', data.imbalance_20);

            // Order book
            var bidWalls = data.walls ? data.walls.bids : [];
            var askWalls = data.walls ? data.walls.asks : [];
            renderBookSide('depth-bid-body', data.bids, 'bid', bidWalls);
            renderBookSide('depth-ask-body', data.asks, 'ask', askWalls);

            // Delta
            var deltaValEl = document.getElementById('depth-delta-value');
            if (deltaValEl) {
                var d = data.cumulative_delta || 0;
                deltaValEl.textContent = (d >= 0 ? '+' : '') + fmtQty(Math.round(d));
                deltaValEl.style.color = d >= 0 ? '#3fb950' : '#f85149';
            }
            drawDomDeltaChart(data.delta_history);

            // Alerts
            renderAlerts(data.alerts);
        }

        // ══════════════════════════════════════════════════════════
        // BOOKMAP Tab
        // ══════════════════════════════════════════════════════════

        var bmCanvas = document.getElementById('bm-canvas');
        var vpCanvas = document.getElementById('bm-volprofile');
        var dCanvas = document.getElementById('bm-delta-canvas');
        var tipEl = document.getElementById('bm-tooltip');

        var windowSec = 120;
        var priceZoom = 0;
        var snapshots = [];
        var allTicks = [];
        var volProfile = {};
        var bmDeltaHist = [];
        var lastSnapT = 0;
        var needsDraw = true;
        var MAX_SNAPS = 1200;
        var MAX_TICKS = 5000;

        window.bmSetWindow = function(s) {
            windowSec = s; needsDraw = true;
            document.querySelectorAll('.bm-time-btn').forEach(function(b) {
                var on = parseInt(b.getAttribute('data-window')) === s;
                b.style.color = on ? '#e0e6ed' : '#8b949e';
                b.style.borderColor = on ? '#58a6ff' : '#30363d';
            });
        };
        window.bmZoom = function(d) { priceZoom = Math.max(-5, Math.min(10, priceZoom + d)); needsDraw = true; };
        window.bmReset = function() { priceZoom = 0; needsDraw = true; };

        function handleBookmapIngest(data) {
            if (!data || !data.bids || !data.asks) return;
            var now = Date.now() / 1000;

            if (now - lastSnapT >= 0.9) {
                lastSnapT = now;
                var snap = {t: now, ltp: data.ltp || 0, b: [], a: []};
                data.bids.forEach(function(b) { if (b.price > 0 && b.qty > 0) snap.b.push([b.price, b.qty]); });
                data.asks.forEach(function(a) { if (a.price > 0 && a.qty > 0) snap.a.push([a.price, a.qty]); });
                snapshots.push(snap);
                if (snapshots.length > MAX_SNAPS) snapshots.splice(0, snapshots.length - MAX_SNAPS);
            }

            if (data.tick_history && data.tick_history.length > 0) {
                var lastTk = allTicks.length > 0 ? allTicks[allTicks.length - 1].t : 0;
                data.tick_history.forEach(function(tk) {
                    if (tk.t > lastTk || allTicks.length === 0) allTicks.push(tk);
                });
                if (allTicks.length > MAX_TICKS) allTicks.splice(0, allTicks.length - MAX_TICKS);
            }

            if (data.volume_profile) {
                volProfile = {};
                data.volume_profile.forEach(function(vp) { volProfile[vp.price] = {b: vp.buy, s: vp.sell}; });
            }

            if (data.delta_history) bmDeltaHist = data.delta_history;

            // Stats
            setT('bm-symbol', data.symbol || '--');
            setT('bm-ltp', data.ltp ? fmtPrice(data.ltp) : '--');

            var tBuy = 0, tSell = 0;
            for (var p in volProfile) { tBuy += volProfile[p].b; tSell += volProfile[p].s; }
            setT('bm-buy-vol', fmtQty(tBuy));
            setT('bm-sell-vol', fmtQty(tSell));
            var tot = tBuy + tSell;
            if (tot > 0) {
                var bp = Math.round(tBuy / tot * 100);
                var bbEl = document.getElementById('bm-buy-bar');
                var sbEl = document.getElementById('bm-sell-bar');
                if (bbEl) bbEl.style.width = bp + '%';
                if (sbEl) sbEl.style.width = (100 - bp) + '%';
                setT('bm-buy-pct', bp + '%');
                setT('bm-sell-pct', (100 - bp) + '%');
            }

            var d = data.cumulative_delta || 0;
            var dEl = document.getElementById('bm-delta-value');
            if (dEl) {
                dEl.textContent = (d >= 0 ? '+' : '') + fmtQty(Math.round(d));
                dEl.style.color = d >= 0 ? '#3fb950' : '#f85149';
            }

            needsDraw = true;
        }

        // ── Render Loop ──
        function renderLoop() {
            requestAnimationFrame(renderLoop);
            if (!needsDraw) return;
            needsDraw = false;
            drawMain();
            drawVP();
            drawBmDelta();
        }

        function drawMain() {
            if (!bmCanvas) return;
            var dpr = window.devicePixelRatio || 1;
            var w = bmCanvas.offsetWidth, h = bmCanvas.offsetHeight;
            bmCanvas.width = w * dpr; bmCanvas.height = h * dpr;
            var ctx = bmCanvas.getContext('2d');
            ctx.scale(dpr, dpr);
            ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, w, h);

            if (snapshots.length < 2) {
                ctx.fillStyle = '#484f58'; ctx.font = '14px monospace';
                ctx.fillText('Collecting depth data... Bookmap builds as updates arrive.', 20, h / 2);
                return;
            }

            var now = Date.now() / 1000;
            var tS = now - windowSec, tE = now;

            var vis = snapshots.filter(function(s) { return s.t >= tS - 2 && s.t <= tE + 2; });
            if (vis.length < 1) {
                ctx.fillStyle = '#484f58'; ctx.font = '13px monospace';
                ctx.fillText('No data in current window. Waiting...', 20, h / 2);
                return;
            }

            var pMin = Infinity, pMax = -Infinity;
            vis.forEach(function(s) {
                s.b.forEach(function(l) { if (l[0] < pMin) pMin = l[0]; if (l[0] > pMax) pMax = l[0]; });
                s.a.forEach(function(l) { if (l[0] < pMin) pMin = l[0]; if (l[0] > pMax) pMax = l[0]; });
            });
            if (pMin === Infinity) return;

            var mid = (pMin + pMax) / 2, rng = pMax - pMin;
            var zf = Math.pow(0.85, priceZoom);
            pMin = mid - rng * zf / 2; pMax = mid + rng * zf / 2;

            var pw = w - 58, ph = h - 24;

            function tX(t) { return ((t - tS) / (tE - tS)) * pw; }
            function pY(p) { return ph - ((p - pMin) / (pMax - pMin)) * ph; }

            var mq = 0;
            vis.forEach(function(s) {
                s.b.forEach(function(l) { if (l[1] > mq) mq = l[1]; });
                s.a.forEach(function(l) { if (l[1] > mq) mq = l[1]; });
            });
            if (mq === 0) mq = 1;

            var cellH = 4;
            if (vis.length > 0 && vis[vis.length - 1].b.length > 1) {
                var pp = vis[vis.length - 1].b.map(function(l) { return l[0]; }).sort(function(a, b) { return a - b; });
                if (pp.length > 1) {
                    var minStep = Infinity;
                    for (var si = 1; si < pp.length; si++) {
                        var diff = pp[si] - pp[si - 1];
                        if (diff > 0 && diff < minStep) minStep = diff;
                    }
                    if (minStep < Infinity) cellH = Math.max(2, Math.min(14, (minStep / (pMax - pMin)) * ph));
                }
            }

            var cw = Math.max(1.5, pw / windowSec);

            // Heatmap
            vis.forEach(function(s) {
                var x = tX(s.t);
                s.b.forEach(function(l) {
                    var y = pY(l[0]);
                    var a = 0.06 + Math.sqrt(Math.min(l[1] / mq, 1)) * 0.64;
                    ctx.fillStyle = 'rgba(63,185,80,' + a.toFixed(2) + ')';
                    ctx.fillRect(x - cw / 2, y - cellH / 2, cw, cellH);
                });
                s.a.forEach(function(l) {
                    var y = pY(l[0]);
                    var a = 0.06 + Math.sqrt(Math.min(l[1] / mq, 1)) * 0.64;
                    ctx.fillStyle = 'rgba(248,81,73,' + a.toFixed(2) + ')';
                    ctx.fillRect(x - cw / 2, y - cellH / 2, cw, cellH);
                });
            });

            // Trade Bubbles
            var vtk = allTicks.filter(function(t) { return t.t >= tS && t.t <= tE; });
            var mv = 0;
            vtk.forEach(function(t) { if (t.v > mv) mv = t.v; });
            if (mv === 0) mv = 1;

            vtk.forEach(function(tk) {
                var x = tX(tk.t), y = pY(tk.p);
                if (y < 0 || y > ph) return;
                var r = 2 + Math.sqrt(tk.v / mv) * 8;
                ctx.beginPath();
                ctx.arc(x, y, r, 0, Math.PI * 2);
                ctx.fillStyle = tk.s === 'buy' ? 'rgba(63,185,80,0.65)' : 'rgba(248,81,73,0.65)';
                ctx.fill();
                ctx.strokeStyle = tk.s === 'buy' ? '#3fb950' : '#f85149';
                ctx.lineWidth = 0.8;
                ctx.stroke();
            });

            // LTP Line
            ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
            ctx.beginPath();
            var first = true;
            vis.forEach(function(s) {
                if (!s.ltp) return;
                var x = tX(s.t), y = pY(s.ltp);
                if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
            });
            if (!first) ctx.stroke();

            // LTP label
            if (vis.length > 0 && vis[vis.length - 1].ltp) {
                var lastLtp = vis[vis.length - 1].ltp;
                var ly = pY(lastLtp);
                ctx.fillStyle = '#1a2332'; ctx.fillRect(pw - 70, ly - 9, 68, 18);
                ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1;
                ctx.strokeRect(pw - 70, ly - 9, 68, 18);
                ctx.fillStyle = '#58a6ff'; ctx.font = 'bold 10px monospace'; ctx.textAlign = 'right';
                ctx.fillText(lastLtp.toFixed(2), pw - 6, ly + 4);
            }

            // Price Axis
            ctx.fillStyle = '#161b22'; ctx.fillRect(pw, 0, 58, h);
            ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(pw, 0); ctx.lineTo(pw, h); ctx.stroke();

            var pr = pMax - pMin;
            var stp = pr > 500 ? 50 : pr > 200 ? 20 : pr > 100 ? 10 : pr > 50 ? 5 : pr > 20 ? 2 : 1;
            var gridP = Math.ceil(pMin / stp) * stp;
            ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'left';
            while (gridP <= pMax) {
                var gy = pY(gridP);
                if (gy >= 5 && gy <= ph - 5) {
                    ctx.fillText(gridP.toFixed(stp < 1 ? 2 : 0), pw + 4, gy + 3);
                    ctx.strokeStyle = 'rgba(33,38,45,0.5)'; ctx.lineWidth = 0.5;
                    ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(pw, gy); ctx.stroke();
                }
                gridP += stp;
            }

            // Time Axis
            ctx.fillStyle = '#161b22'; ctx.fillRect(0, ph, pw, 24);
            ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(0, ph); ctx.lineTo(pw, ph); ctx.stroke();

            var tstp = windowSec <= 120 ? 15 : windowSec <= 300 ? 30 : 60;
            var tl = Math.ceil(tS / tstp) * tstp;
            ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
            while (tl <= tE) {
                var tx = tX(tl);
                if (tx >= 20 && tx <= pw - 20) {
                    var dd = new Date(tl * 1000);
                    ctx.fillText(('0' + dd.getHours()).slice(-2) + ':' + ('0' + dd.getMinutes()).slice(-2) + ':' + ('0' + dd.getSeconds()).slice(-2), tx, ph + 14);
                }
                tl += tstp;
            }

            bmCanvas._bm = {pw: pw, ph: ph, tS: tS, tE: tE, pMin: pMin, pMax: pMax};
        }

        function drawVP() {
            if (!vpCanvas) return;
            var dpr = window.devicePixelRatio || 1;
            var w = vpCanvas.offsetWidth, h = vpCanvas.offsetHeight;
            vpCanvas.width = w * dpr; vpCanvas.height = h * dpr;
            var ctx2 = vpCanvas.getContext('2d');
            ctx2.scale(dpr, dpr);
            ctx2.fillStyle = '#0d1117'; ctx2.fillRect(0, 0, w, h);

            var L = bmCanvas ? bmCanvas._bm : null;
            if (!L) return;

            var prices = Object.keys(volProfile).map(Number).filter(function(p) {
                return p >= L.pMin && p <= L.pMax;
            }).sort(function(a, b) { return a - b; });
            if (prices.length === 0) return;

            var maxV = 0;
            prices.forEach(function(p) {
                var t = (volProfile[p].b || 0) + (volProfile[p].s || 0);
                if (t > maxV) maxV = t;
            });
            if (maxV === 0) return;

            var bH = Math.max(2, Math.min(8, L.ph / Math.max(prices.length, 1) * 0.7));

            prices.forEach(function(p) {
                var y = L.ph - ((p - L.pMin) / (L.pMax - L.pMin)) * L.ph;
                if (y < 0 || y > L.ph) return;
                var bv = volProfile[p].b || 0, sv = volProfile[p].s || 0;
                var total = bv + sv;
                var totW = (total / maxV) * (w - 6);
                var bw = total > 0 ? (bv / total) * totW : 0;
                ctx2.fillStyle = 'rgba(63,185,80,0.55)';
                ctx2.fillRect(3, y - bH / 2, bw, bH);
                ctx2.fillStyle = 'rgba(248,81,73,0.55)';
                ctx2.fillRect(3 + bw, y - bH / 2, totW - bw, bH);
            });
        }

        function drawBmDelta() {
            if (!dCanvas) return;
            var dpr = window.devicePixelRatio || 1;
            var w = dCanvas.offsetWidth, h = dCanvas.offsetHeight;
            dCanvas.width = w * dpr; dCanvas.height = h * dpr;
            var dc = dCanvas.getContext('2d');
            dc.scale(dpr, dpr);
            dc.fillStyle = '#0d1117'; dc.fillRect(0, 0, w, h);

            if (!bmDeltaHist || bmDeltaHist.length < 2) {
                dc.fillStyle = '#484f58'; dc.font = '12px monospace';
                dc.fillText('Collecting data...', 10, h / 2);
                return;
            }

            var vals = bmDeltaHist.map(function(d) { return d.d; });
            var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
            var rg = mx - mn; if (rg === 0) rg = 1;

            var zy = h - ((0 - mn) / rg) * (h - 10) - 5;
            dc.strokeStyle = '#30363d'; dc.lineWidth = 1; dc.setLineDash([4, 4]);
            dc.beginPath(); dc.moveTo(0, zy); dc.lineTo(w, zy); dc.stroke(); dc.setLineDash([]);

            var lv = vals[vals.length - 1];
            dc.fillStyle = lv >= 0 ? 'rgba(63,185,80,0.1)' : 'rgba(248,81,73,0.1)';
            dc.beginPath(); dc.moveTo(0, zy);
            for (var i = 0; i < vals.length; i++) {
                dc.lineTo((i / (vals.length - 1)) * w, h - ((vals[i] - mn) / rg) * (h - 10) - 5);
            }
            dc.lineTo(w, zy); dc.closePath(); dc.fill();

            dc.strokeStyle = lv >= 0 ? '#3fb950' : '#f85149'; dc.lineWidth = 1.5;
            dc.beginPath();
            for (var j = 0; j < vals.length; j++) {
                var x = (j / (vals.length - 1)) * w, y = h - ((vals[j] - mn) / rg) * (h - 10) - 5;
                if (j === 0) dc.moveTo(x, y); else dc.lineTo(x, y);
            }
            dc.stroke();
        }

        // Tooltip
        if (bmCanvas) {
            bmCanvas.addEventListener('mousemove', function(e) {
                var L = bmCanvas._bm;
                if (!L || !tipEl) return;
                var r = bmCanvas.getBoundingClientRect();
                var mx = e.clientX - r.left, my = e.clientY - r.top;
                if (mx > L.pw || my > L.ph) { tipEl.style.display = 'none'; return; }

                var t = L.tS + (mx / L.pw) * (L.tE - L.tS);
                var p = L.pMax - (my / L.ph) * (L.pMax - L.pMin);
                var dd = new Date(t * 1000);
                var ts = ('0' + dd.getHours()).slice(-2) + ':' + ('0' + dd.getMinutes()).slice(-2) + ':' + ('0' + dd.getSeconds()).slice(-2);

                var info = '';
                var near = null, best = Infinity;
                for (var i = 0; i < snapshots.length; i++) {
                    var dist = Math.abs(snapshots[i].t - t);
                    if (dist < best) { best = dist; near = snapshots[i]; }
                }
                if (near) {
                    var nq = 0, np = 0, ns = '', bd = Infinity;
                    near.b.forEach(function(l) { var d = Math.abs(l[0] - p); if (d < bd) { bd = d; np = l[0]; nq = l[1]; ns = 'Bid'; } });
                    near.a.forEach(function(l) { var d = Math.abs(l[0] - p); if (d < bd) { bd = d; np = l[0]; nq = l[1]; ns = 'Ask'; } });
                    if (np) info = '<br>' + ns + ': ' + fmtQty(nq) + ' @ ' + fmtPrice(np);
                }

                tipEl.innerHTML = ts + ' | ' + fmtPrice(p) + info;
                tipEl.style.display = 'block';
                var tx = mx + 15, ty = my - 10;
                if (tx + 200 > bmCanvas.offsetWidth) tx = mx - 210;
                if (ty < 0) ty = my + 15;
                tipEl.style.left = tx + 'px'; tipEl.style.top = ty + 'px';
            });
            bmCanvas.addEventListener('mouseleave', function() { if (tipEl) tipEl.style.display = 'none'; });
        }

        // Fetch initial snapshot
        fetch('/api/depth/snapshot').then(function(r) { return r.json(); }).then(function(d) {
            if (d && d.symbol) { handleDepthUpdate(d); handleBookmapIngest(d); }
        }).catch(function() {});

        // Start render loop
        renderLoop();
    </script>
</body>
</html>
"""


# ── Flask Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    from flask import make_response
    resp = make_response(render_template_string(TEMPLATE))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/depth/config")
def depth_config():
    if _engine:
        return jsonify(_engine.get_config())
    return jsonify({"running": False, "connected": False})


@app.route("/api/depth/snapshot")
def depth_snapshot():
    if _engine:
        return jsonify(_engine.get_snapshot())
    return jsonify({"symbol": "", "connected": False, "bids": [], "asks": []})


@app.route("/api/depth/reconnect", methods=["POST"])
def depth_reconnect():
    if _engine:
        _engine.reconnect()
        return jsonify({"status": "ok", "message": "Reconnect requested"})
    return jsonify({"status": "error", "message": "Engine not running"}), 400


# ── Entrypoint ────────────────────────────────────────────────────

def main():
    global _engine

    errors = Config.validate()
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        logger.error("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env")
        sys.exit(1)

    logger.info("Starting DOM & Bookmap Analyzer on %s:%d", Config.HOST, Config.PORT)

    _engine = DepthEngine(socketio)
    _engine.start()

    socketio.run(app, host=Config.HOST, port=Config.PORT,
                 debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
