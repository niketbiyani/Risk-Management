#!/usr/bin/env python3
"""
Fyers DOM Analyzer Patcher
==========================
Applies the Fyers integration changes to your existing codebase.
Run this AFTER checking out the new Fyers files from the branch.

Usage:
    python3 apply_fyers_patch.py

This script modifies: dashboard.py, config.py, main.py, requirements.txt, .env.example
It will NOT overwrite if Fyers changes are already present.
"""

import os
import sys
import re

WORK_DIR = os.path.dirname(os.path.abspath(__file__))


def read_file(name):
    path = os.path.join(WORK_DIR, name)
    with open(path, "r") as f:
        return f.read()


def write_file(name, content):
    path = os.path.join(WORK_DIR, name)
    with open(path, "w") as f:
        f.write(content)
    print(f"  [OK] {name}")


def patch_requirements():
    """Add Fyers dependencies to requirements.txt."""
    content = read_file("requirements.txt")
    new_pkgs = [
        "websockets>=11.0.3",
        "protobuf>=5.29.0",
        "argon2-cffi>=25.1.0",
        "sqlalchemy>=2.0.0",
    ]
    added = []
    for pkg in new_pkgs:
        pkg_name = pkg.split(">=")[0].split("==")[0].strip()
        if pkg_name not in content:
            content = content.rstrip("\n") + "\n" + pkg + "\n"
            added.append(pkg_name)
    if added:
        write_file("requirements.txt", content)
        print(f"       Added: {', '.join(added)}")
    else:
        print("  [SKIP] requirements.txt - Fyers packages already present")


def patch_config():
    """Add Fyers config section to config.py."""
    content = read_file("config.py")
    if "FYERS_API_KEY" in content:
        print("  [SKIP] config.py - Fyers config already present")
        return

    fyers_block = '''
    # Fyers DOM Analyzer
    FYERS_API_KEY: str = os.getenv("FYERS_API_KEY", "")
    FYERS_API_SECRET: str = os.getenv("FYERS_API_SECRET", "")
    FYERS_REDIRECT_URL: str = os.getenv("FYERS_REDIRECT_URL", "http://127.0.0.1:5555/fyers/callback")
    FYERS_WEBSOCKET_URL: str = os.getenv("FYERS_WEBSOCKET_URL", "wss://rtsocket-api.fyers.in/versova")
    FYERS_SYMBOL: str = os.getenv("FYERS_SYMBOL", "NSE:NIFTY25JULFUT")
    FYERS_LOT_SIZE: int = int(os.getenv("FYERS_LOT_SIZE", "50"))
    FYERS_ENABLED: bool = os.getenv("FYERS_ENABLED", "false").lower() == "true"

'''
    # Insert before @classmethod validate
    anchor = "    @classmethod\n    def validate(cls)"
    if anchor in content:
        content = content.replace(anchor, fyers_block + anchor)
        write_file("config.py", content)
    else:
        print("  [ERROR] config.py - Could not find validate() anchor")


def patch_main():
    """Add Fyers midnight cleanup to main.py."""
    content = read_file("main.py")
    if "fyers_midnight_cleanup" in content:
        print("  [SKIP] main.py - Fyers scheduler already present")
        return

    fyers_block = '''
    # Initialize Fyers DOM Analyzer midnight cleanup if enabled
    if Config.FYERS_ENABLED:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from fyers_database import revoke_all_fyers_tokens
            from fyers_websocket import disable_websocket as fyers_disable_ws

            def fyers_midnight_cleanup():
                logger.info("[SEBI COMPLIANCE] Running Fyers midnight token cleanup")
                count = revoke_all_fyers_tokens()
                fyers_disable_ws()
                logger.info("[SEBI COMPLIANCE] Revoked %d Fyers token(s)", count)

            fyers_scheduler = BackgroundScheduler(daemon=True)
            fyers_scheduler.add_job(
                func=fyers_midnight_cleanup,
                trigger=CronTrigger(hour=3, minute=0),
                id='fyers_midnight_cleanup',
                name='SEBI Compliance - Fyers Midnight Token Cleanup',
                replace_existing=True
            )
            fyers_scheduler.start()
            logger.info("Fyers midnight token cleanup scheduled for 3:00 AM daily")
        except Exception as e:
            logger.warning("Failed to set up Fyers scheduler: %s", e)

'''
    # Insert after pusher_thread.start() in run_full() - unique anchor
    anchor = "    pusher_thread.start()\n"
    if anchor in content:
        content = content.replace(anchor, anchor + fyers_block, 1)
        write_file("main.py", content)
    else:
        print("  [ERROR] main.py - Could not find dashboard starting anchor")


def patch_env_example():
    """Add Fyers section to .env.example."""
    content = read_file(".env.example")
    if "FYERS_ENABLED" in content:
        print("  [SKIP] .env.example - Fyers section already present")
        return

    fyers_section = """
# ─── Fyers DOM Analyzer (optional) ───────────────────────────
# Set FYERS_ENABLED=true to enable the DOM Analyzer tab
FYERS_ENABLED=false
FYERS_API_KEY=your_fyers_app_id
FYERS_API_SECRET=your_fyers_secret_key
FYERS_REDIRECT_URL=http://127.0.0.1:5555/fyers/callback
FYERS_WEBSOCKET_URL=wss://rtsocket-api.fyers.in/versova
FYERS_SYMBOL=NSE:NIFTY25JULFUT
FYERS_LOT_SIZE=50

# Fyers Security (change in production)
FYERS_DATABASE_URL=sqlite:///fyers_auth.db
FYERS_API_KEY_PEPPER=your_secure_pepper_key_change_in_production
"""
    content = content.rstrip("\n") + "\n" + fyers_section
    write_file(".env.example", content)


def patch_dashboard():
    """Apply all Fyers changes to dashboard.py."""
    content = read_file("dashboard.py")

    if "FYERS_IMPORTS_OK" in content:
        print("  [SKIP] dashboard.py - Fyers changes already present")
        return

    errors = []

    # ── Change 1: Add redirect, session to flask imports ──
    old_import = "from flask import Flask, render_template_string, jsonify, request"
    new_import = "from flask import Flask, render_template_string, jsonify, request, redirect, session"
    if old_import in content and "redirect, session" not in content:
        content = content.replace(old_import, new_import, 1)
    elif "redirect, session" in content:
        pass  # already present
    else:
        errors.append("Could not find flask import line")

    # ── Change 2: Add Fyers conditional imports ──
    fyers_imports = """
# Fyers DOM Analyzer imports (conditional)
try:
    from fyers_database import init_fyers_db, upsert_fyers_auth, get_fyers_auth_token, is_fyers_token_valid_for_today
    from fyers_auth import authenticate_fyers_broker, handle_fyers_auth_success, mask_api_credential
    from fyers_websocket import set_socketio as set_fyers_socketio, enable_websocket as fyers_enable_ws, disable_websocket as fyers_disable_ws
    FYERS_IMPORTS_OK = True
except ImportError as e:
    FYERS_IMPORTS_OK = False
    logging.getLogger(__name__).warning("Fyers modules not available: %s", e)

"""
    anchor2 = "from config import Config\n"
    if anchor2 in content:
        content = content.replace(anchor2, anchor2 + fyers_imports, 1)
    else:
        errors.append("Could not find 'from config import Config' anchor")

    # ── Change 3: Add page tab navigation to header ──
    old_header = '<h1>Risk Management Dashboard</h1>'
    new_header = '''<div style="display:flex;align-items:center;gap:24px;">
            <h1>Risk Management Dashboard</h1>
            <!-- Main Page Tabs -->
            {% if fyers_enabled %}
            <div style="display:flex;gap:0;margin-left:16px;">
                <div class="page-tab active" data-page="risk" onclick="switchPage('risk')" style="padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid #58a6ff;color:#58a6ff;transition:all 0.2s;">Risk Dashboard</div>
                <div class="page-tab" data-page="dom" onclick="switchPage('dom')" style="padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;transition:all 0.2s;">DOM Analyzer</div>
            </div>
            {% endif %}
        </div>'''
    if old_header in content:
        content = content.replace(old_header, new_header, 1)
    else:
        errors.append("Could not find <h1>Risk Management Dashboard</h1> anchor")

    # ── Change 3b: Add switchPage to early <script> block (global scope) ──
    early_script_anchor = "    </script>\n</head>"
    switchpage_fn = """        function switchPage(page) {
            var risk = document.getElementById('page-risk');
            var dom = document.getElementById('page-dom');
            if (risk) risk.style.display = page === 'risk' ? '' : 'none';
            if (dom) dom.style.display = page === 'dom' ? '' : 'none';
            document.querySelectorAll('.page-tab').forEach(function(t) {
                var isActive = t.getAttribute('data-page') === page;
                t.style.borderBottomColor = isActive ? '#58a6ff' : 'transparent';
                t.style.color = isActive ? '#58a6ff' : '#8b949e';
            });
        }
"""
    if early_script_anchor in content and "function switchPage" not in content:
        content = content.replace(early_script_anchor, switchpage_fn + early_script_anchor, 1)
    elif "function switchPage" in content:
        pass
    else:
        errors.append("Could not find early </script></head> anchor for switchPage")

    # ── Change 4: Wrap existing content in page-risk div ──
    lockout_anchor = '    <div id="lockout-banner" class="lockout-banner">'
    page_risk_open = '    <!-- ═══ PAGE: RISK DASHBOARD ═══ -->\n    <div id="page-risk">\n'
    if lockout_anchor in content and 'id="page-risk"' not in content:
        content = content.replace(lockout_anchor, page_risk_open + lockout_anchor, 1)
    else:
        errors.append("Could not find lockout-banner anchor or page-risk already exists")

    # ── Change 5: Close page-risk + add DOM Analyzer HTML before footer ──
    dom_html = _get_dom_analyzer_html()
    footer_anchor = '    <div class="footer">'
    if footer_anchor in content and '<div id="page-dom"' not in content:
        content = content.replace(footer_anchor, dom_html + "\n" + footer_anchor, 1)
    else:
        errors.append("Could not find footer anchor or DOM page already exists")

    # ── Change 6: Add DOM analyzer JS before the final </script> ──
    dom_js = _get_dom_analyzer_js()
    # Find the last </script> before the closing """ of DASHBOARD_HTML
    final_script_close = '    </script>\n</body>\n</html>\n"""'
    if final_script_close in content and "setupDomSocket" not in content:
        content = content.replace(final_script_close, dom_js + "\n    </script>\n</body>\n</html>\n" + '"""', 1)
    elif "setupDomSocket" in content:
        pass  # already present
    else:
        errors.append("Could not find final </script></body></html> anchor for DOM JS")

    # ── Change 7: Add Fyers state to index() function ──
    fyers_state = '''
    # Fyers DOM Analyzer state
    fyers_enabled = Config.FYERS_ENABLED and FYERS_IMPORTS_OK
    fyers_logged_in = False
    if fyers_enabled and session.get('fyers_logged_in'):
        username = session.get('fyers_user', 'fyers_user')
        if is_fyers_token_valid_for_today(username):
            fyers_logged_in = True
        else:
            session.pop('fyers_logged_in', None)
            session.pop('fyers_user', None)
            fyers_disable_ws()

'''
    make_response_anchor = "    from flask import make_response"
    if make_response_anchor in content and "fyers_enabled = Config.FYERS_ENABLED" not in content:
        content = content.replace(make_response_anchor, fyers_state + make_response_anchor, 1)
    elif "fyers_enabled = Config.FYERS_ENABLED" in content:
        pass
    else:
        errors.append("Could not find 'from flask import make_response' anchor in index()")

    # ── Change 8: Add Fyers template vars to render_template_string ──
    fyers_vars = """        fyers_enabled=fyers_enabled,
        fyers_logged_in=fyers_logged_in,
        fyers_symbol=Config.FYERS_SYMBOL,
        fyers_lot_size=Config.FYERS_LOT_SIZE,
"""
    template_anchor = "        profit_lock_distance_fmt=fmt_inr(Config.PROFIT_LOCK_THRESHOLD),"
    if template_anchor in content and "fyers_enabled=fyers_enabled" not in content:
        content = content.replace(template_anchor, template_anchor + "\n" + fyers_vars, 1)
    elif "fyers_enabled=fyers_enabled" in content:
        pass
    else:
        errors.append("Could not find profit_lock_distance_fmt template anchor")

    # ── Change 9: Add Fyers routes before SocketIO Emitters ──
    fyers_routes = _get_fyers_routes()
    emitters_anchor = "# ── SocketIO Emitters"
    if emitters_anchor in content and "fyers/connect" not in content:
        content = content.replace(emitters_anchor, fyers_routes + "\n" + emitters_anchor, 1)
    elif "fyers/connect" in content:
        pass
    else:
        errors.append("Could not find SocketIO Emitters anchor")

    # ── Change 10: Add Fyers init to run_dashboard() ──
    fyers_init = """
    # Initialize Fyers DOM Analyzer if enabled
    if Config.FYERS_ENABLED and FYERS_IMPORTS_OK:
        try:
            init_fyers_db()
            set_fyers_socketio(socketio)
            from fyers_websocket import start_fyers_websocket_thread
            start_fyers_websocket_thread()
            logger.info("Fyers DOM Analyzer initialized")
        except Exception as e:
            logger.error("Failed to initialize Fyers DOM Analyzer: %s", e)

"""
    socketio_run_anchor = "    socketio.run(app,"
    if socketio_run_anchor in content and "init_fyers_db" not in content:
        content = content.replace(socketio_run_anchor, fyers_init + socketio_run_anchor, 1)
    elif "init_fyers_db" in content:
        pass
    else:
        errors.append("Could not find socketio.run anchor in run_dashboard()")

    if errors:
        print(f"  [WARN] dashboard.py - Applied with {len(errors)} warnings:")
        for e in errors:
            print(f"         - {e}")
    write_file("dashboard.py", content)


def _get_dom_analyzer_html():
    """Return the DOM Analyzer HTML block."""
    return r"""    </div><!-- end page-risk -->

    <!-- ═══ PAGE: DOM ANALYZER ═══ -->
    {% if fyers_enabled %}
    <div id="page-dom" style="display:none;">
        {% if fyers_logged_in %}
        <!-- DOM Analyzer Dashboard Content -->
        <div style="padding:20px 24px;">
            <!-- Top Stats Bar -->
            <div class="grid grid-main" style="margin-bottom:16px;">
                <div class="card">
                    <h3>Symbol</h3>
                    <div class="value" style="font-size:22px;color:#58a6ff;" id="dom-symbol">{{ fyers_symbol }}</div>
                    <div class="sub">Lot Size: {{ fyers_lot_size }}</div>
                </div>
                <div class="card">
                    <h3>Last Price</h3>
                    <div class="value" id="dom-last-price" style="font-size:22px;">--</div>
                    <div class="sub" id="dom-price-change">--</div>
                </div>
                <div class="card">
                    <h3>Bid-Ask Spread</h3>
                    <div class="value" id="dom-spread" style="font-size:22px;">--</div>
                    <div class="sub" id="dom-spread-pct">--</div>
                </div>
                <div class="card">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <h3 style="margin:0;">Connection</h3>
                        <span id="dom-ws-status" style="font-size:11px;padding:2px 8px;border-radius:10px;background:#0d4429;color:#3fb950;border:1px solid #238636;">LIVE</span>
                    </div>
                    <div class="sub" style="margin-top:8px;">Updated: <span id="dom-last-update">--</span></div>
                    <div style="margin-top:8px;display:flex;gap:6px;align-items:center;">
                        <label style="font-size:11px;color:#8b949e;">Lot Size</label>
                        <input type="checkbox" id="dom-lot-toggle" style="cursor:pointer;">
                    </div>
                </div>
            </div>

            <!-- Market Overview Row -->
            <div class="grid" style="grid-template-columns:1fr 1fr 1fr 1fr;margin-bottom:16px;">
                <div class="card">
                    <h3>Total Bid Qty</h3>
                    <div class="value positive" id="dom-total-bid" style="font-size:22px;">0</div>
                    <div class="sub">Buy Side</div>
                </div>
                <div class="card">
                    <h3>Total Ask Qty</h3>
                    <div class="value negative" id="dom-total-ask" style="font-size:22px;">0</div>
                    <div class="sub">Sell Side</div>
                </div>
                <div class="card">
                    <h3>Bid-Ask Ratio</h3>
                    <div id="dom-ratio" style="font-size:18px;font-weight:700;margin-top:4px;">50% : 50%</div>
                    <div style="height:6px;background:#21262d;border-radius:3px;margin-top:8px;overflow:hidden;display:flex;">
                        <div id="dom-bid-bar" style="background:#3fb950;height:100%;width:50%;transition:width 0.5s;"></div>
                        <div id="dom-ask-bar" style="background:#f85149;height:100%;width:50%;transition:width 0.5s;"></div>
                    </div>
                    <div class="sub" id="dom-ratio-summary" style="margin-top:4px;">Balanced</div>
                </div>
                <div class="card">
                    <h3>Market Sentiment</h3>
                    <div id="dom-sentiment-badge" style="display:inline-block;padding:4px 12px;border-radius:12px;font-size:13px;font-weight:600;background:#0c2d6b;color:#58a6ff;border:1px solid #1f6feb;margin-top:4px;">Neutral</div>
                    <div class="sub" style="margin-top:8px;">Score: <span id="dom-sentiment-score">0.00</span></div>
                </div>
            </div>

            <!-- Order Book Imbalance Row -->
            <div class="card" style="margin-bottom:16px;">
                <h3>Order Book Imbalance Analysis</h3>
                <div class="grid" style="grid-template-columns:1fr 1fr 1fr;margin-top:12px;">
                    <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:16px;">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;">10 Level Imbalance</div>
                        <div id="dom-imb10-val" style="font-size:24px;font-weight:700;margin-top:4px;">--</div>
                        <div id="dom-imb10-interp" style="font-size:12px;color:#8b949e;margin-top:2px;">--</div>
                        <div style="height:4px;background:#21262d;border-radius:2px;margin-top:8px;overflow:hidden;">
                            <div style="height:100%;background:linear-gradient(to right,#3fb950,#f85149);position:relative;">
                                <div id="dom-imb10-ind" style="position:absolute;height:100%;width:2px;background:white;left:50%;"></div>
                            </div>
                        </div>
                        <div style="display:flex;justify-content:space-between;font-size:10px;color:#484f58;margin-top:4px;">
                            <span>Buy</span><span>Neutral</span><span>Sell</span>
                        </div>
                        <div style="font-size:11px;color:#484f58;margin-top:4px;">Bid: <span id="dom-imb10-bid">--</span> | Ask: <span id="dom-imb10-ask">--</span></div>
                    </div>
                    <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:16px;">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;">20 Level Imbalance</div>
                        <div id="dom-imb20-val" style="font-size:24px;font-weight:700;margin-top:4px;">--</div>
                        <div id="dom-imb20-interp" style="font-size:12px;color:#8b949e;margin-top:2px;">--</div>
                        <div style="height:4px;background:#21262d;border-radius:2px;margin-top:8px;overflow:hidden;">
                            <div style="height:100%;background:linear-gradient(to right,#3fb950,#f85149);position:relative;">
                                <div id="dom-imb20-ind" style="position:absolute;height:100%;width:2px;background:white;left:50%;"></div>
                            </div>
                        </div>
                        <div style="display:flex;justify-content:space-between;font-size:10px;color:#484f58;margin-top:4px;">
                            <span>Buy</span><span>Neutral</span><span>Sell</span>
                        </div>
                        <div style="font-size:11px;color:#484f58;margin-top:4px;">Bid: <span id="dom-imb20-bid">--</span> | Ask: <span id="dom-imb20-ask">--</span></div>
                    </div>
                    <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:16px;">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;">50 Level Imbalance</div>
                        <div id="dom-imb50-val" style="font-size:24px;font-weight:700;margin-top:4px;">--</div>
                        <div id="dom-imb50-interp" style="font-size:12px;color:#8b949e;margin-top:2px;">--</div>
                        <div style="height:4px;background:#21262d;border-radius:2px;margin-top:8px;overflow:hidden;">
                            <div style="height:100%;background:linear-gradient(to right,#3fb950,#f85149);position:relative;">
                                <div id="dom-imb50-ind" style="position:absolute;height:100%;width:2px;background:white;left:50%;"></div>
                            </div>
                        </div>
                        <div style="display:flex;justify-content:space-between;font-size:10px;color:#484f58;margin-top:4px;">
                            <span>Buy</span><span>Neutral</span><span>Sell</span>
                        </div>
                        <div style="font-size:11px;color:#484f58;margin-top:4px;">Bid: <span id="dom-imb50-bid">--</span> | Ask: <span id="dom-imb50-ask">--</span></div>
                    </div>
                </div>
            </div>

            <!-- DOM Display Controls -->
            <div class="card" style="margin-bottom:16px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                    <h3 style="margin:0;">Depth of Market (DOM) &mdash; <span id="dom-levels-badge" style="color:#58a6ff;">50 Levels</span> / <span id="dom-active-badge" style="color:#3fb950;">0 Active</span></h3>
                    <div style="display:flex;gap:12px;align-items:center;">
                        <select id="dom-depth-select" style="background:#0d1117;border:1px solid #30363d;color:#e0e6ed;padding:4px 8px;border-radius:6px;font-size:12px;">
                            <option value="10">10 Levels</option>
                            <option value="20">20 Levels</option>
                            <option value="50" selected>50 Levels</option>
                        </select>
                        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#8b949e;cursor:pointer;">
                            <input type="checkbox" id="dom-depth-bars" checked> Depth Bars
                        </label>
                        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#8b949e;cursor:pointer;">
                            <input type="checkbox" id="dom-hide-zero"> Hide Zero Qty
                        </label>
                    </div>
                </div>

                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
                    <!-- Bid Orders (Buy) -->
                    <div>
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                            <span style="color:#3fb950;font-weight:600;font-size:14px;">Bid Orders (Buy)</span>
                            <span style="font-size:12px;color:#8b949e;">Visible: <strong class="positive" id="dom-vis-bid">0</strong></span>
                        </div>
                        <div style="max-height:600px;overflow-y:auto;">
                            <table>
                                <thead><tr><th>Lvl</th><th>Price</th><th style="text-align:right;">Qty</th><th style="text-align:center;">Orders</th><th>Depth</th></tr></thead>
                                <tbody id="dom-bids" style="color:#3fb950;"></tbody>
                            </table>
                        </div>
                    </div>
                    <!-- Ask Orders (Sell) -->
                    <div>
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                            <span style="color:#f85149;font-weight:600;font-size:14px;">Ask Orders (Sell)</span>
                            <span style="font-size:12px;color:#8b949e;">Visible: <strong class="negative" id="dom-vis-ask">0</strong></span>
                        </div>
                        <div style="max-height:600px;overflow-y:auto;">
                            <table>
                                <thead><tr><th>Lvl</th><th>Price</th><th style="text-align:right;">Qty</th><th style="text-align:center;">Orders</th><th>Depth</th></tr></thead>
                                <tbody id="dom-asks" style="color:#f85149;"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Bottom Analysis Row -->
            <div class="grid" style="grid-template-columns:1fr 1fr 1fr;margin-bottom:16px;">
                <!-- Large Order Watch -->
                <div class="card">
                    <h3>Large Order Watch</h3>
                    <div style="max-height:200px;overflow-y:auto;">
                        <table style="font-size:12px;">
                            <thead><tr><th>Type</th><th>Price</th><th style="text-align:right;">Qty</th><th style="text-align:right;">% Total</th></tr></thead>
                            <tbody id="dom-large-orders"><tr><td colspan="4" style="text-align:center;color:#484f58;">No data</td></tr></tbody>
                        </table>
                    </div>
                </div>
                <!-- Depth Distribution -->
                <div class="card">
                    <h3>Depth Distribution</h3>
                    <div style="margin-top:8px;">
                        <div style="margin-bottom:8px;">
                            <div style="font-size:11px;color:#8b949e;margin-bottom:4px;">Top 5 Levels</div>
                            <div style="display:flex;align-items:center;gap:8px;">
                                <span id="dom-top5-bid-pct" style="font-size:11px;color:#3fb950;width:40px;">--</span>
                                <div style="flex:1;height:6px;background:#21262d;border-radius:3px;overflow:hidden;display:flex;">
                                    <div id="dom-top5-bid-bar" style="background:#3fb950;width:50%;height:100%;transition:width 0.5s;"></div>
                                    <div id="dom-top5-ask-bar" style="background:#f85149;width:50%;height:100%;transition:width 0.5s;"></div>
                                </div>
                                <span id="dom-top5-ask-pct" style="font-size:11px;color:#f85149;width:40px;text-align:right;">--</span>
                            </div>
                        </div>
                        <div style="margin-bottom:8px;">
                            <div style="font-size:11px;color:#8b949e;margin-bottom:4px;">Next 10 Levels</div>
                            <div style="display:flex;align-items:center;gap:8px;">
                                <span id="dom-n10-bid-pct" style="font-size:11px;color:#3fb950;width:40px;">--</span>
                                <div style="flex:1;height:6px;background:#21262d;border-radius:3px;overflow:hidden;display:flex;">
                                    <div id="dom-n10-bid-bar" style="background:#3fb950;width:50%;height:100%;transition:width 0.5s;"></div>
                                    <div id="dom-n10-ask-bar" style="background:#f85149;width:50%;height:100%;transition:width 0.5s;"></div>
                                </div>
                                <span id="dom-n10-ask-pct" style="font-size:11px;color:#f85149;width:40px;text-align:right;">--</span>
                            </div>
                        </div>
                        <div>
                            <div style="font-size:11px;color:#8b949e;margin-bottom:4px;">Remaining Levels</div>
                            <div style="display:flex;align-items:center;gap:8px;">
                                <span id="dom-rest-bid-pct" style="font-size:11px;color:#3fb950;width:40px;">--</span>
                                <div style="flex:1;height:6px;background:#21262d;border-radius:3px;overflow:hidden;display:flex;">
                                    <div id="dom-rest-bid-bar" style="background:#3fb950;width:50%;height:100%;transition:width 0.5s;"></div>
                                    <div id="dom-rest-ask-bar" style="background:#f85149;width:50%;height:100%;transition:width 0.5s;"></div>
                                </div>
                                <span id="dom-rest-ask-pct" style="font-size:11px;color:#f85149;width:40px;text-align:right;">--</span>
                            </div>
                        </div>
                    </div>
                </div>
                <!-- Price Level Clusters -->
                <div class="card">
                    <h3>Price Level Activity</h3>
                    <div style="max-height:200px;overflow-y:auto;">
                        <table style="font-size:12px;">
                            <thead><tr><th>Range</th><th style="text-align:right;color:#3fb950;">Bid</th><th style="text-align:right;color:#f85149;">Ask</th><th style="text-align:right;">Net</th></tr></thead>
                            <tbody id="dom-clusters"><tr><td colspan="4" style="text-align:center;color:#484f58;">No data</td></tr></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Logout -->
            <div style="text-align:center;padding:8px;">
                <a href="/fyers/logout" style="color:#8b949e;font-size:12px;text-decoration:none;">Disconnect Fyers</a>
            </div>
        </div>
        {% else %}
        <!-- Fyers Login Screen -->
        <div style="display:flex;justify-content:center;align-items:center;min-height:60vh;">
            <div style="max-width:400px;text-align:center;">
                <div style="margin-bottom:24px;">
                    <h2 style="font-size:28px;font-weight:700;color:#f0f6fc;margin-bottom:8px;"><span style="color:#58a6ff;">FYERS</span> DOM ANALYZER</h2>
                    <p style="color:#8b949e;font-size:14px;">50-level depth of market analysis with real-time order flow</p>
                </div>
                <div class="card" style="padding:32px;">
                    <p style="color:#8b949e;font-size:13px;margin-bottom:20px;">Connect your Fyers account via OAuth to start streaming live market depth data.</p>
                    <a href="/fyers/connect" style="display:inline-block;background:#238636;color:white;padding:12px 32px;border-radius:8px;font-size:15px;font-weight:700;text-decoration:none;transition:background 0.2s;">Connect to Fyers</a>
                    <div style="margin-top:16px;display:flex;justify-content:center;gap:24px;font-size:11px;color:#484f58;">
                        <span>SSL Encrypted</span>
                        <span>OAuth 2.0</span>
                        <span>SEBI Compliant</span>
                    </div>
                </div>
            </div>
        </div>
        {% endif %}
    </div>
    {% endif %}
"""


def _get_dom_analyzer_js():
    """Return the DOM Analyzer JavaScript block."""
    return r"""
        // ── DOM Analyzer Frontend Logic ──────────────────────────────
        (function() {
            var domLastPrice = 0;
            var domPriceChange = 0;
            var domMaxQty = { bid: 1, ask: 1 };
            var domLotSize = {{ fyers_lot_size }};
            var domShowLots = false;
            var domPrevValid = { bids: [], asks: [] };

            function fmtP(price) {
                return price.toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2});
            }
            function fmtQ(qty) {
                if (domShowLots && domLotSize > 0) {
                    var lots = Math.floor(qty / domLotSize);
                    var rem = qty % domLotSize;
                    if (rem === 0) return lots + ' lots';
                    if (lots > 0) return lots + ' lots + ' + rem;
                    return '' + qty;
                }
                return qty.toLocaleString('en-IN');
            }
            function fmtPct(p) { return p.toFixed(1) + '%'; }
            function fmtTime(ts) {
                var d = new Date(ts);
                return d.toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
            }

            var lotToggle = document.getElementById('dom-lot-toggle');
            if (lotToggle) {
                lotToggle.addEventListener('change', function() { domShowLots = this.checked; });
            }

            // Listen for fyers_market_depth via SocketIO
            function setupDomSocket() {
                if (!socket) { setTimeout(setupDomSocket, 2000); return; }
                socket.on('fyers_market_depth', function(data) {
                    Object.entries(data).forEach(function(entry) {
                        var sym = entry[0], d = entry[1];
                        updateDom(d);
                    });
                });
            }
            setupDomSocket();

            function updateDom(d) {
                var bids = d.bids || [];
                var asks = d.asks || [];
                var totalBid = d.total_bid_qty || 0;
                var totalAsk = d.total_sell_qty || 0;

                // Timestamp
                var el = document.getElementById('dom-last-update');
                if (el) el.textContent = fmtTime(d.timestamp);

                // Totals
                el = document.getElementById('dom-total-bid');
                if (el) { el.textContent = fmtQ(totalBid); el.dataset.qty = totalBid; }
                el = document.getElementById('dom-total-ask');
                if (el) { el.textContent = fmtQ(totalAsk); el.dataset.qty = totalAsk; }

                // Ratio
                var total = totalBid + totalAsk;
                var bidR = total > 0 ? (totalBid / total * 100) : 50;
                var askR = total > 0 ? (totalAsk / total * 100) : 50;
                el = document.getElementById('dom-ratio');
                if (el) el.textContent = bidR.toFixed(1) + '% : ' + askR.toFixed(1) + '%';
                el = document.getElementById('dom-bid-bar');
                if (el) el.style.width = bidR + '%';
                el = document.getElementById('dom-ask-bar');
                if (el) el.style.width = askR + '%';

                // Filter valid
                var vBids = bids.filter(function(b){ return b.price > 0; });
                var vAsks = asks.filter(function(a){ return a.price > 0; });
                if (vBids.length === 0 && domPrevValid.bids.length > 0) vBids = domPrevValid.bids;
                if (vAsks.length === 0 && domPrevValid.asks.length > 0) vAsks = domPrevValid.asks;
                if (vBids.length > 0) domPrevValid.bids = vBids.slice();
                if (vAsks.length > 0) domPrevValid.asks = vAsks.slice();

                // Sentiment
                if (vBids.length > 0 && vAsks.length > 0) {
                    var sent = total > 0 ? (totalBid - totalAsk) / total : 0;
                    el = document.getElementById('dom-sentiment-score');
                    if (el) el.textContent = sent.toFixed(2);
                    var badge = document.getElementById('dom-sentiment-badge');
                    if (badge) {
                        var txt = 'Neutral', bg = '#0c2d6b', clr = '#58a6ff', brd = '#1f6feb';
                        if (sent > 0.3) { txt='Bullish'; bg='#0d4429'; clr='#3fb950'; brd='#238636'; }
                        else if (sent > 0.1) { txt='Mildly Bullish'; bg='#0d4429'; clr='#3fb950'; brd='#238636'; }
                        else if (sent < -0.3) { txt='Bearish'; bg='#4a1d1d'; clr='#f85149'; brd='#da3633'; }
                        else if (sent < -0.1) { txt='Mildly Bearish'; bg='#4a1d1d'; clr='#f85149'; brd='#da3633'; }
                        badge.textContent = txt;
                        badge.style.background = bg;
                        badge.style.color = clr;
                        badge.style.borderColor = brd;
                    }
                }

                // Max quantities
                domMaxQty.bid = Math.max.apply(null, vBids.map(function(b){ return b.quantity; }).concat([1]));
                domMaxQty.ask = Math.max.apply(null, vAsks.map(function(a){ return a.quantity; }).concat([1]));

                // Spread and price
                if (vBids.length > 0 && vAsks.length > 0) {
                    var bestBid = vBids[0].price, bestAsk = vAsks[0].price;
                    if (bestAsk > bestBid) {
                        var spread = bestAsk - bestBid;
                        el = document.getElementById('dom-spread');
                        if (el) el.textContent = fmtP(spread);
                        el = document.getElementById('dom-spread-pct');
                        if (el) el.textContent = (spread / bestBid * 100).toFixed(3) + '%';
                        var mid = (bestBid + bestAsk) / 2;
                        if (mid > 0) {
                            if (domLastPrice > 0) domPriceChange = mid - domLastPrice;
                            domLastPrice = mid;
                            el = document.getElementById('dom-last-price');
                            if (el) { el.textContent = fmtP(mid); el.className = 'value' + (domPriceChange >= 0 ? ' positive' : ' negative'); el.style.fontSize = '22px'; }
                            el = document.getElementById('dom-price-change');
                            if (el) { el.textContent = (domPriceChange >= 0 ? '+' : '') + fmtP(domPriceChange); el.className = 'sub ' + (domPriceChange >= 0 ? 'positive' : 'negative'); }
                        }
                    }
                }

                // Imbalance
                updateDomImbalance(d);

                // DOM table
                var maxLevels = parseInt(document.getElementById('dom-depth-select').value) || 50;
                var showBars = document.getElementById('dom-depth-bars').checked;
                var hideZero = document.getElementById('dom-hide-zero').checked;

                var fBids = hideZero ? vBids.filter(function(b){ return b.quantity > 0; }) : vBids;
                var fAsks = hideZero ? vAsks.filter(function(a){ return a.quantity > 0; }) : vAsks;

                el = document.getElementById('dom-levels-badge');
                if (el) el.textContent = maxLevels + ' Levels';
                el = document.getElementById('dom-active-badge');
                if (el) el.textContent = (fBids.length + fAsks.length) + ' Active';

                fBids = fBids.slice(0, maxLevels);
                fAsks = fAsks.slice(0, maxLevels);

                var visBidQty = 0, visAskQty = 0;

                // Render bids
                var bidsBody = document.getElementById('dom-bids');
                if (bidsBody) {
                    bidsBody.innerHTML = '';
                    fBids.forEach(function(b, i) {
                        visBidQty += b.quantity;
                        var pct = (b.quantity / domMaxQty.bid * 100).toFixed(0);
                        var row = document.createElement('tr');
                        row.innerHTML = '<td style="text-align:center;font-size:11px;opacity:0.7;">' + (i+1) + '</td>'
                            + '<td style="font-family:monospace;' + (i===0?'font-weight:bold;':'') + '">' + fmtP(b.price) + '</td>'
                            + '<td style="text-align:right;font-family:monospace;" data-qty="'+b.quantity+'">' + fmtQ(b.quantity) + '</td>'
                            + '<td style="text-align:center;">' + b.orders + '</td>'
                            + '<td style="width:25%;position:relative;padding-right:8px;">'
                            + (showBars ? '<div style="position:absolute;top:50%;right:0;transform:translateY(-50%);height:4px;border-radius:2px;background:#3fb950;width:'+pct+'%;transition:width 0.5s;"></div>' : '')
                            + '<span style="position:relative;z-index:1;font-size:11px;opacity:0.8;">'+pct+'%</span></td>';
                        bidsBody.appendChild(row);
                    });
                }

                // Render asks
                var asksBody = document.getElementById('dom-asks');
                if (asksBody) {
                    asksBody.innerHTML = '';
                    fAsks.forEach(function(a, i) {
                        visAskQty += a.quantity;
                        var pct = (a.quantity / domMaxQty.ask * 100).toFixed(0);
                        var row = document.createElement('tr');
                        row.innerHTML = '<td style="text-align:center;font-size:11px;opacity:0.7;">' + (i+1) + '</td>'
                            + '<td style="font-family:monospace;' + (i===0?'font-weight:bold;':'') + '">' + fmtP(a.price) + '</td>'
                            + '<td style="text-align:right;font-family:monospace;" data-qty="'+a.quantity+'">' + fmtQ(a.quantity) + '</td>'
                            + '<td style="text-align:center;">' + a.orders + '</td>'
                            + '<td style="width:25%;position:relative;padding-right:8px;">'
                            + (showBars ? '<div style="position:absolute;top:50%;right:0;transform:translateY(-50%);height:4px;border-radius:2px;background:#f85149;width:'+pct+'%;transition:width 0.5s;"></div>' : '')
                            + '<span style="position:relative;z-index:1;font-size:11px;opacity:0.8;">'+pct+'%</span></td>';
                        asksBody.appendChild(row);
                    });
                }

                el = document.getElementById('dom-vis-bid');
                if (el) el.textContent = fmtQ(visBidQty);
                el = document.getElementById('dom-vis-ask');
                if (el) el.textContent = fmtQ(visAskQty);

                // Large orders
                updateDomLargeOrders(vBids, vAsks, totalBid, totalAsk);
                // Depth distribution
                updateDomDepthDist(vBids, vAsks, totalBid, totalAsk);
                // Price clusters
                updateDomClusters(vBids, vAsks);
            }

            function updateDomImbalance(d) {
                [['10','dom-imb10'],['20','dom-imb20'],['50','dom-imb50']].forEach(function(pair) {
                    var key = 'imbalance_' + pair[0], pfx = pair[1];
                    var imb = d[key];
                    if (!imb) return;
                    var valEl = document.getElementById(pfx + '-val');
                    if (valEl) {
                        valEl.textContent = imb.imbalance_pct.toFixed(1) + '%';
                        valEl.style.color = imb.imbalance_pct > 5 ? '#3fb950' : imb.imbalance_pct < -5 ? '#f85149' : '#d29922';
                    }
                    var interpEl = document.getElementById(pfx + '-interp');
                    if (interpEl) interpEl.textContent = imb.interpretation;
                    var indEl = document.getElementById(pfx + '-ind');
                    if (indEl) indEl.style.left = Math.max(0, Math.min(100, 50 + imb.imbalance_pct / 2)) + '%';
                    var bidEl = document.getElementById(pfx + '-bid');
                    if (bidEl) bidEl.textContent = fmtQ(imb.bid_qty);
                    var askEl = document.getElementById(pfx + '-ask');
                    if (askEl) askEl.textContent = fmtQ(imb.ask_qty);
                });
            }

            function updateDomLargeOrders(bids, asks, totalBid, totalAsk) {
                var orders = [];
                bids.forEach(function(b) {
                    var pct = totalBid > 0 ? (b.quantity / totalBid * 100) : 0;
                    if (pct > 3) orders.push({type:'Bid', price:b.price, qty:b.quantity, pct:pct});
                });
                asks.forEach(function(a) {
                    var pct = totalAsk > 0 ? (a.quantity / totalAsk * 100) : 0;
                    if (pct > 3) orders.push({type:'Ask', price:a.price, qty:a.quantity, pct:pct});
                });
                orders.sort(function(a,b){ return b.pct - a.pct; });
                orders = orders.slice(0, 10);
                var body = document.getElementById('dom-large-orders');
                if (!body) return;
                body.innerHTML = '';
                if (orders.length === 0) {
                    body.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#484f58;">None</td></tr>';
                    return;
                }
                orders.forEach(function(o) {
                    var row = document.createElement('tr');
                    row.innerHTML = '<td style="color:' + (o.type==='Bid'?'#3fb950':'#f85149') + ';">' + o.type + '</td>'
                        + '<td style="font-family:monospace;">' + fmtP(o.price) + '</td>'
                        + '<td style="text-align:right;font-family:monospace;">' + fmtQ(o.qty) + '</td>'
                        + '<td style="text-align:right;">' + fmtPct(o.pct) + '</td>';
                    body.appendChild(row);
                });
            }

            function updateDomDepthDist(bids, asks, totalBid, totalAsk) {
                var t5b=0,t5a=0,n10b=0,n10a=0,rb=0,ra=0;
                bids.forEach(function(b,i) { if(i<5) t5b+=b.quantity; else if(i<15) n10b+=b.quantity; else rb+=b.quantity; });
                asks.forEach(function(a,i) { if(i<5) t5a+=a.quantity; else if(i<15) n10a+=a.quantity; else ra+=a.quantity; });
                var t5bp = totalBid>0?(t5b/totalBid*100):0, t5ap = totalAsk>0?(t5a/totalAsk*100):0;
                var n10bp = totalBid>0?(n10b/totalBid*100):0, n10ap = totalAsk>0?(n10a/totalAsk*100):0;
                var rbp = totalBid>0?(rb/totalBid*100):0, rap = totalAsk>0?(ra/totalAsk*100):0;

                function s(id,v){var e=document.getElementById(id);if(e)e.textContent=fmtPct(v);}
                function w(id,v){var e=document.getElementById(id);if(e)e.style.width=v+'%';}
                s('dom-top5-bid-pct',t5bp); s('dom-top5-ask-pct',t5ap);
                w('dom-top5-bid-bar',t5bp); w('dom-top5-ask-bar',t5ap);
                s('dom-n10-bid-pct',n10bp); s('dom-n10-ask-pct',n10ap);
                w('dom-n10-bid-bar',n10bp); w('dom-n10-ask-bar',n10ap);
                s('dom-rest-bid-pct',rbp); s('dom-rest-ask-pct',rap);
                w('dom-rest-bid-bar',rbp); w('dom-rest-ask-bar',rap);
            }

            function updateDomClusters(bids, asks) {
                var allP = bids.map(function(b){return b.price;}).concat(asks.map(function(a){return a.price;}));
                if (allP.length === 0) return;
                var minP = Math.min.apply(null, allP), maxP = Math.max.apply(null, allP);
                var range = maxP - minP;
                if (range <= 0) return;
                var cs = range / 5, clusters = [];
                for (var i=0;i<5;i++) {
                    var lo = minP + i*cs, hi = minP + (i+1)*cs;
                    var bq=0, aq=0;
                    bids.forEach(function(b){ if(b.price>=lo && b.price<hi) bq+=b.quantity; });
                    asks.forEach(function(a){ if(a.price>=lo && a.price<hi) aq+=a.quantity; });
                    clusters.push({range:fmtP(lo)+'-'+fmtP(hi), bq:bq, aq:aq, net:bq-aq});
                }
                var body = document.getElementById('dom-clusters');
                if (!body) return;
                body.innerHTML = '';
                clusters.forEach(function(c) {
                    var row = document.createElement('tr');
                    var nc = c.net >= 0 ? '#3fb950' : '#f85149';
                    row.innerHTML = '<td style="font-size:11px;">' + c.range + '</td>'
                        + '<td style="text-align:right;color:#3fb950;font-family:monospace;">' + fmtQ(c.bq) + '</td>'
                        + '<td style="text-align:right;color:#f85149;font-family:monospace;">' + fmtQ(c.aq) + '</td>'
                        + '<td style="text-align:right;color:'+nc+';font-family:monospace;">' + (c.net>=0?'+':'') + fmtQ(c.net) + '</td>';
                    body.appendChild(row);
                });
            }
        })();"""


def _get_fyers_routes():
    """Return the Fyers Flask routes block."""
    return '''# ── Fyers DOM Analyzer Routes ─────────────────────────────────────

@app.route("/fyers/connect")
def fyers_connect():
    """Redirect to Fyers OAuth login."""
    if not Config.FYERS_ENABLED or not FYERS_IMPORTS_OK:
        return redirect("/")
    api_key = Config.FYERS_API_KEY
    redirect_url = Config.FYERS_REDIRECT_URL
    import urllib.parse
    encoded_redirect = urllib.parse.quote(redirect_url, safe='')
    state = 'fyers_auth_' + str(int(time.time()))
    fyers_url = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={api_key}&redirect_uri={encoded_redirect}"
        f"&response_type=code&state={state}"
    )
    return redirect(fyers_url)


@app.route("/fyers/callback")
def fyers_callback():
    """Handle Fyers OAuth callback."""
    if not Config.FYERS_ENABLED or not FYERS_IMPORTS_OK:
        return redirect("/")

    auth_code = request.args.get('auth_code') or request.args.get('code')
    error = request.args.get('error')

    if error:
        logger.error("Fyers OAuth error: %s", error)
        return redirect("/")
    if not auth_code:
        logger.error("Fyers OAuth: no auth code received")
        return redirect("/")

    auth_token, error_message = authenticate_fyers_broker(auth_code)

    if auth_token:
        username = 'fyers_user'
        success = handle_fyers_auth_success(auth_token, username, 'fyers')
        if success:
            session['fyers_logged_in'] = True
            session['fyers_user'] = username
            fyers_enable_ws()
            logger.info("Fyers authenticated for user %s", username)
        else:
            logger.error("Failed to store Fyers auth token")
    else:
        logger.error("Fyers authentication failed: %s", error_message)

    return redirect("/")


@app.route("/fyers/logout")
def fyers_logout():
    """Logout from Fyers and disable WebSocket."""
    if FYERS_IMPORTS_OK:
        username = session.get('fyers_user')
        if username:
            upsert_fyers_auth(username, "", "fyers", revoke=True)
        fyers_disable_ws()
    session.pop('fyers_logged_in', None)
    session.pop('fyers_user', None)
    return redirect("/")


@app.route("/api/fyers/config")
def fyers_config_api():
    """Get Fyers DOM configuration."""
    if not Config.FYERS_ENABLED or not FYERS_IMPORTS_OK:
        return jsonify({"enabled": False})
    from fyers_websocket import get_fyers_config
    return jsonify(get_fyers_config())


'''


def main():
    print("=" * 50)
    print("  Fyers DOM Analyzer - Patch Installer")
    print("=" * 50)
    print()

    # Verify we're in the right directory
    if not os.path.exists(os.path.join(WORK_DIR, "dashboard.py")):
        print("ERROR: dashboard.py not found. Run this from the Risk-Management directory.")
        sys.exit(1)

    # Check that new Fyers files exist
    fyers_files = ["fyers_auth.py", "fyers_database.py", "fyers_websocket.py", "msg.proto", "msg_pb2.py"]
    missing = [f for f in fyers_files if not os.path.exists(os.path.join(WORK_DIR, f))]
    if missing:
        print(f"ERROR: Missing Fyers files: {', '.join(missing)}")
        print("Run this first:")
        print("  git checkout origin/claude/integrate-fyers-websockets-wDrFY -- " + " ".join(fyers_files))
        sys.exit(1)

    print("Patching files...")
    print()

    patch_requirements()
    patch_config()
    patch_main()
    patch_env_example()
    patch_dashboard()

    print()
    print("=" * 50)
    print("  Patch Complete!")
    print("=" * 50)
    print()
    print("Next steps:")
    print("  1. Install new dependencies:")
    print("     source venv/bin/activate && pip install -r requirements.txt")
    print("  2. Add to your .env file:")
    print("     FYERS_ENABLED=true")
    print("     FYERS_API_KEY=your_fyers_app_id")
    print("     FYERS_API_SECRET=your_fyers_secret_key")
    print("  3. Restart the platform:")
    print("     bash stop.sh && bash start.sh")
    print()


if __name__ == "__main__":
    main()
