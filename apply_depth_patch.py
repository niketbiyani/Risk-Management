#!/usr/bin/env python3
"""
Dhan Market Depth Patcher
=========================
Applies Dhan 20-level depth integration to your existing codebase.
Run this AFTER checking out dhan_depth.py from the branch.

Usage:
    python3 apply_depth_patch.py

This script modifies: config.py, dashboard.py
It will NOT overwrite if depth changes are already present.

Prerequisites:
    git fetch origin claude/integrate-fyers-websockets-wDrFY
    git checkout origin/claude/integrate-fyers-websockets-wDrFY -- dhan_depth.py apply_depth_patch.py
"""

import os
import sys

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


def cleanup_fyers():
    """Remove all Fyers DOM Analyzer code if present.

    The Fyers integration has been replaced by Dhan Market Depth.
    This runs before depth patching to strip old Fyers code from
    files that were previously patched with apply_fyers_patch.py.
    """
    import re

    cleaned = []

    # ── Clean dashboard.py ──
    content = read_file("dashboard.py")
    original = content

    # 1. Remove Fyers imports block
    fyers_import_block = re.compile(
        r'\n?# Fyers DOM Analyzer imports \(conditional\)\n'
        r'try:\n'
        r'    from fyers_database import.*?\n'
        r'    from fyers_auth import.*?\n'
        r'    from fyers_websocket import.*?\n'
        r'    FYERS_IMPORTS_OK = True\n'
        r'except ImportError as e:\n'
        r'    FYERS_IMPORTS_OK = False\n'
        r'    logging\.getLogger\(__name__\)\.warning\("Fyers modules not available: %s", e\)\n',
        re.DOTALL
    )
    content = fyers_import_block.sub('\n', content)

    # 2. Remove ', redirect, session' from Flask import (if present)
    content = content.replace(
        'from flask import Flask, render_template_string, jsonify, request, redirect, session',
        'from flask import Flask, render_template_string, jsonify, request'
    )

    # 3. Remove DOM Analyzer tab button from nav
    dom_tab_patterns = [
        # With newlines around it
        '\n                {% if fyers_enabled %}\n                <div class="page-tab" data-page="dom" onclick="switchPage(\'dom\')" style="padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;transition:all 0.2s;">DOM Analyzer</div>\n                {% endif %}',
    ]
    for pat in dom_tab_patterns:
        content = content.replace(pat, '')

    # 4. Remove {% if fyers_enabled %} wrapper around ALL tabs (old patcher bug)
    #    This replaces the wrapped block with an unwrapped version
    fyers_wrapped_tabs = re.compile(
        r'(            <!-- Main Page Tabs -->\n)'
        r'            \{% if fyers_enabled %\}\n'
        r'(            <div style="display:flex;gap:0;.*?>\n)'
        r'(                <div class="page-tab active" data-page="risk".*?Risk Dashboard</div>\n)'
        r'                <div class="page-tab" data-page="dom".*?DOM Analyzer</div>\n'
        r'(            </div>\n)'
        r'            \{% endif %\}',
        re.DOTALL
    )
    match = fyers_wrapped_tabs.search(content)
    if match:
        content = content[:match.start()] + match.group(1) + match.group(2) + match.group(3) + match.group(4) + content[match.end():]

    # 5. Remove 'page-dom' from switchPage function
    content = content.replace("            var dom = document.getElementById('page-dom');\n", '')
    content = content.replace("            if (dom) dom.style.display = page === 'dom' ? '' : 'none';\n", '')

    # 6. Remove entire DOM Analyzer HTML section
    dom_html_pattern = re.compile(
        r'\n    <!-- ═══ PAGE: DOM ANALYZER ═══ -->.*?'
        r'(?=\n    <div class="footer">|\n    <!-- ═══ PAGE: MARKET DEPTH)',
        re.DOTALL
    )
    content = dom_html_pattern.sub('', content)

    # 7. Remove DOM Analyzer JavaScript IIFE
    #    It starts with the comment and ends with })();
    dom_js_start = content.find('        // ── DOM Analyzer Frontend Logic')
    if dom_js_start < 0:
        # Try alternate: the IIFE with domLastPrice
        dom_js_start = content.find('        (function() {\n            var domLastPrice = 0;')
    if dom_js_start >= 0:
        # Find the matching })(); ending
        depth_marker = content.find('        // ═══ DHAN MARKET DEPTH', dom_js_start)
        end_script = content.find('    </script>\n</body>', dom_js_start)
        if depth_marker > dom_js_start:
            # Remove up to the depth marker
            content = content[:dom_js_start] + content[depth_marker:]
        elif end_script > dom_js_start:
            # Find the last })(); before </script>
            search_area = content[dom_js_start:end_script]
            last_iife_end = search_area.rfind('        })();')
            if last_iife_end >= 0:
                content = content[:dom_js_start] + content[dom_js_start + last_iife_end + len('        })();\n'):]

    # 8. Remove Fyers Flask routes
    fyers_routes_pattern = re.compile(
        r'\n*# ── Fyers DOM Analyzer Routes[^\n]*\n'
        r'.*?'
        r'(?=\n# ── |ndef run_dashboard|ndef [a-z]|\nif __name__)',
        re.DOTALL
    )
    content = fyers_routes_pattern.sub('\n', content)

    # Also remove individual routes if the block pattern didn't match
    for route in ['/fyers/connect', '/fyers/callback', '/fyers/logout', '/api/fyers/config']:
        route_pattern = re.compile(
            r'\n@app\.route\("' + re.escape(route) + r'".*?\n'
            r'def \w+\(.*?\):\n'
            r'    """.*?"""\n'
            r'.*?(?=\n@app\.route|\ndef |\n# ──)',
            re.DOTALL
        )
        content = route_pattern.sub('', content)

    # 9. Remove Fyers state initialization in index()
    fyers_state_pattern = re.compile(
        r'\n    # Fyers DOM Analyzer state\n'
        r'    fyers_enabled = Config\.FYERS_ENABLED.*?\n'
        r'    fyers_logged_in = False\n'
        r'    if fyers_enabled.*?fyers_disable_ws\(\)\n',
        re.DOTALL
    )
    content = fyers_state_pattern.sub('\n', content)

    # 10. Remove Fyers template variables from render_template_string
    for var in ['fyers_enabled=', 'fyers_logged_in=', 'fyers_symbol=', 'fyers_lot_size=']:
        var_pattern = re.compile(r'\n\s+' + re.escape(var) + r'[^\n]*,?')
        content = var_pattern.sub('', content)

    # 11. Remove Fyers init block in run_dashboard()
    fyers_init_pattern = re.compile(
        r'\n    # Initialize Fyers DOM Analyzer if enabled\n'
        r'    if Config\.FYERS_ENABLED.*?\n'
        r'        (?:try|except).*?'
        r'(?=\n    socketio\.run|\n    # )',
        re.DOTALL
    )
    content = fyers_init_pattern.sub('\n', content)

    if content != original:
        write_file("dashboard.py", content)
        cleaned.append("dashboard.py — removed Fyers DOM Analyzer code")

    # ── Clean config.py ──
    content = read_file("config.py")
    original = content
    # Remove Fyers config block
    fyers_config_pattern = re.compile(
        r'\n    # Fyers DOM Analyzer\n'
        r'(?:    FYERS_\w+[^\n]*\n)+',
        re.DOTALL
    )
    content = fyers_config_pattern.sub('\n', content)
    # Also remove individual FYERS_ lines that might not be under the comment
    content = re.sub(r'    FYERS_\w+: \w+ = os\.getenv\([^\n]+\n', '', content)
    if content != original:
        write_file("config.py", content)
        cleaned.append("config.py — removed FYERS_* config variables")

    # ── Clean main.py ──
    content = read_file("main.py")
    original = content
    fyers_scheduler_pattern = re.compile(
        r'\n    # Initialize Fyers DOM Analyzer midnight cleanup.*?\n'
        r'    if Config\.FYERS_ENABLED:\n'
        r'        try:\n'
        r'.*?'
        r'        except.*?:.*?\n'
        r'            logger\.warning\("Failed to set up Fyers scheduler.*?\n',
        re.DOTALL
    )
    content = fyers_scheduler_pattern.sub('\n', content)
    if content != original:
        write_file("main.py", content)
        cleaned.append("main.py — removed Fyers midnight scheduler")

    if cleaned:
        print("  [CLEANUP] Removed old Fyers DOM Analyzer:")
        for c in cleaned:
            print(f"         - {c}")
    else:
        print("  [SKIP] No Fyers code found to clean up")


def patch_config():
    """Add DEPTH_ENABLED to config.py."""
    content = read_file("config.py")
    if "DEPTH_ENABLED" in content:
        print("  [SKIP] config.py - DEPTH_ENABLED already present")
        return

    depth_line = '    DEPTH_ENABLED: bool = os.getenv("DEPTH_ENABLED", "true").lower() == "true"\n'

    # Insert before @classmethod validate
    anchor = "    @classmethod\n    def validate(cls)"
    if anchor in content:
        content = content.replace(anchor, "    # Dhan Market Depth\n" + depth_line + "\n" + anchor)
        write_file("config.py", content)
    else:
        # Last resort: insert before the last line of the Config class
        # Find DASHBOARD_HOST line as anchor (always present)
        if "DASHBOARD_HOST" in content:
            lines = content.split('\n')
            new_lines = []
            inserted = False
            for line in lines:
                if 'DASHBOARD_HOST' in line and not inserted:
                    new_lines.append("    # Dhan Market Depth")
                    new_lines.append(depth_line.rstrip())
                    new_lines.append("")
                    inserted = True
                new_lines.append(line)
            content = '\n'.join(new_lines)
            write_file("config.py", content)
        else:
            print("  [ERROR] config.py - Could not find suitable anchor for DEPTH_ENABLED")


def patch_dashboard():
    """Apply Dhan depth changes to dashboard.py."""
    content = read_file("dashboard.py")
    errors = []

    # ── Change 1: Add _depth_analyzer global ──
    if "_depth_analyzer" not in content:
        anchor = "_instrument_cache = None"
        if anchor in content:
            content = content.replace(anchor, anchor + "\n_depth_analyzer = None", 1)
        else:
            errors.append("Could not find _instrument_cache global for _depth_analyzer")

    # ── Change 2: Add switchPage to early <script> block ──
    if "function switchPage" not in content:
        early_anchor = "    </script>\n</head>"
        switchpage_fn = """        function switchPage(page) {
            var risk = document.getElementById('page-risk');
            var depth = document.getElementById('page-depth');
            if (risk) risk.style.display = page === 'risk' ? '' : 'none';
            if (depth) depth.style.display = page === 'depth' ? '' : 'none';
            document.querySelectorAll('.page-tab').forEach(function(t) {
                var isActive = t.getAttribute('data-page') === page;
                t.style.borderBottomColor = isActive ? '#58a6ff' : 'transparent';
                t.style.color = isActive ? '#58a6ff' : '#8b949e';
            });
        }
"""
        if early_anchor in content:
            content = content.replace(early_anchor, switchpage_fn + early_anchor, 1)
        else:
            errors.append("Could not find early </script></head> anchor for switchPage")
    elif "page-depth" not in content.split("function switchPage")[1].split("}")[0]:
        # switchPage exists but doesn't handle 'depth' — add depth var/toggle
        errors.append("switchPage exists but does not handle page-depth (update manually)")

    # ── Change 3: Add Market Depth tab button in header ──
    if 'data-page="depth"' not in content:
        if "<!-- Main Page Tabs -->" in content:
            # Tabs already exist — add depth tab after Risk Dashboard
            old_risk_tab = '<div class="page-tab active" data-page="risk" onclick="switchPage(\'risk\')" style="padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid #58a6ff;color:#58a6ff;transition:all 0.2s;">Risk Dashboard</div>'
            depth_tab = '\n                {% if depth_enabled %}\n                <div class="page-tab" data-page="depth" onclick="switchPage(\'depth\')" style="padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;transition:all 0.2s;">Market Depth</div>\n                {% endif %}'
            if old_risk_tab in content:
                content = content.replace(old_risk_tab, old_risk_tab + depth_tab, 1)
            else:
                errors.append("Could not find Risk Dashboard tab to insert Market Depth tab after it")
        else:
            # No tabs at all — need to add the full tab system
            old_header = '<h1>Risk Management Dashboard</h1>'
            new_header = '''<div style="display:flex;align-items:center;gap:24px;">
            <h1>Risk Management Dashboard</h1>
            <!-- Main Page Tabs -->
            <div style="display:flex;gap:0;margin-left:16px;">
                <div class="page-tab active" data-page="risk" onclick="switchPage('risk')" style="padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid #58a6ff;color:#58a6ff;transition:all 0.2s;">Risk Dashboard</div>
                {% if depth_enabled %}
                <div class="page-tab" data-page="depth" onclick="switchPage('depth')" style="padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;color:#8b949e;transition:all 0.2s;">Market Depth</div>
                {% endif %}
            </div>
        </div>'''
            if old_header in content:
                content = content.replace(old_header, new_header, 1)
            else:
                errors.append("Could not find <h1>Risk Management Dashboard</h1> for tab insertion")

    # ── Change 4: Wrap existing content in page-risk div (if not already done) ──
    if 'id="page-risk"' not in content:
        lockout_anchor = '    <div id="lockout-banner"'
        page_risk_open = '    <!-- PAGE: RISK DASHBOARD -->\n    <div id="page-risk">\n'
        if lockout_anchor in content:
            content = content.replace(lockout_anchor, page_risk_open + lockout_anchor, 1)
        else:
            errors.append("Could not find lockout-banner anchor for page-risk wrapper")

    # ── Change 5: Close page-risk and add depth HTML before footer ──
    depth_html = _get_depth_html()
    if '<div id="page-depth"' not in content:
        footer_anchor = '    <div class="footer">'

        if 'id="page-risk"' in content and '</div><!-- end page-risk -->' not in content:
            # page-risk was opened but not closed — close it before footer
            if footer_anchor in content:
                content = content.replace(footer_anchor, "    </div><!-- end page-risk -->\n\n" + depth_html + "\n\n" + footer_anchor, 1)
            else:
                errors.append("Could not find footer anchor for depth HTML")
        elif '</div><!-- end page-risk -->' in content:
            # page-risk already closed — insert depth after close
            close_anchor = '    </div><!-- end page-risk -->'
            content = content.replace(close_anchor, close_anchor + "\n\n" + depth_html, 1)
        elif footer_anchor in content:
            content = content.replace(footer_anchor, depth_html + "\n\n" + footer_anchor, 1)
        else:
            errors.append("Could not find suitable anchor for depth HTML")

    # ── Change 6: Add depth JavaScript before final </script> ──
    depth_js = _get_depth_js()
    if "depth_update" not in content or "handleDepthUpdate" not in content:
        final_script = '    </script>\n</body>\n</html>'
        if final_script in content:
            content = content.replace(final_script, "\n" + depth_js + "\n" + final_script, 1)
        else:
            final_script2 = '</script>\n</body>\n</html>'
            if final_script2 in content:
                content = content.replace(final_script2, "\n" + depth_js + "\n" + final_script2, 1)
            else:
                errors.append("Could not find final </script></body></html> anchor for depth JS")

    # ── Change 7: Add depth_enabled to template variables ──
    if "depth_enabled=" not in content:
        if "profit_lock_distance_fmt=" in content:
            anchor = "        profit_lock_distance_fmt=fmt_inr(Config.PROFIT_LOCK_THRESHOLD),"
            if anchor in content:
                content = content.replace(anchor, anchor + "\n        depth_enabled=Config.DEPTH_ENABLED,", 1)
            else:
                errors.append("Could not find template variable anchor for depth_enabled")
        else:
            errors.append("Could not find profit_lock_distance_fmt to insert depth_enabled")

    # ── Change 8: Add depth API routes ──
    depth_routes = _get_depth_routes()
    if "/api/depth/config" not in content:
        emitters_anchor = "# ── SocketIO Emitters"
        if emitters_anchor in content:
            content = content.replace(emitters_anchor, depth_routes + "\n\n" + emitters_anchor, 1)
        else:
            run_anchor = "def run_dashboard(monitor):"
            if run_anchor in content:
                content = content.replace(run_anchor, depth_routes + "\n\n" + run_anchor, 1)
            else:
                errors.append("Could not find anchor for depth API routes")

    # ── Change 9: Add depth init to run_dashboard() ──
    if "DhanDepthAnalyzer" not in content:
        depth_init = """
    # Initialize Dhan Market Depth Analyzer if enabled
    if Config.DEPTH_ENABLED:
        try:
            from dhan_depth import DhanDepthAnalyzer
            _depth_analyzer = DhanDepthAnalyzer(socketio, _instrument_cache)
            _depth_analyzer.start()
            logger.info("Dhan Market Depth Analyzer initialized")
        except Exception as e:
            logger.error("Failed to initialize Dhan Depth Analyzer: %s", e)

"""
        socketio_run_anchor = "    socketio.run(app,"
        if socketio_run_anchor in content:
            run_dashboard_anchor = "def run_dashboard(monitor):"
            if run_dashboard_anchor in content:
                old_run = "def run_dashboard(monitor):\n"
                next_line_match = content.split(old_run, 1)
                if len(next_line_match) == 2:
                    after = next_line_match[1]
                    if "global _depth_analyzer" not in content:
                        first_line = after.split('\n')[0]
                        if first_line.strip().startswith('global'):
                            content = content.replace(first_line, first_line.rstrip() + "\n    global _depth_analyzer", 1)
                        elif first_line.strip().startswith('"""'):
                            docstring_end = after.find('"""', after.find('"""') + 3)
                            if docstring_end >= 0:
                                insert_point = after[docstring_end:].find('\n') + docstring_end + 1
                                after = after[:insert_point] + "    global _depth_analyzer\n" + after[insert_point:]
                                content = old_run.join([next_line_match[0], after])
                            else:
                                content = content.replace(old_run, old_run + "    global _depth_analyzer\n", 1)
                        else:
                            content = content.replace(old_run, old_run + "    global _depth_analyzer\n", 1)

            content = content.replace(socketio_run_anchor, depth_init + socketio_run_anchor, 1)
        else:
            errors.append("Could not find socketio.run anchor for depth init")

    # ── Change 10: Add depth reconnect on token update/refresh ──
    if "_depth_analyzer" in content:
        # Token update route
        token_update_marker = "Reinitialize the Dhan API client"
        if token_update_marker in content and "_depth_analyzer" not in content.split(token_update_marker)[1].split("def ")[0]:
            reinit_anchor = '            _monitor.api.reinitialize(Config.DHAN_CLIENT_ID, new_token)\n            Config.DHAN_ACCESS_TOKEN = new_token'
            if reinit_anchor in content:
                content = content.replace(
                    reinit_anchor,
                    reinit_anchor + '\n        # Reconnect depth WebSocket with new token\n        if _depth_analyzer:\n            _depth_analyzer.reconnect()',
                    1
                )

        # Token refresh route
        refresh_marker = "Token refreshed successfully"
        if refresh_marker in content:
            load_dotenv_in_refresh = 'Config.DHAN_ACCESS_TOKEN = _os.getenv("DHAN_ACCESS_TOKEN", "")'
            if load_dotenv_in_refresh in content:
                sections = content.split(load_dotenv_in_refresh)
                for i in range(1, len(sections)):
                    next_chunk = sections[i][:200]
                    if '_depth_analyzer' not in next_chunk and 'return jsonify' in next_chunk:
                        sections[i] = '\n            # Reconnect depth WebSocket with new token\n            if _depth_analyzer:\n                _depth_analyzer.reconnect()' + sections[i]
                        content = load_dotenv_in_refresh.join(sections)
                        break

    # Write result
    original = read_file("dashboard.py")
    changed = content != original
    if changed:
        write_file("dashboard.py", content)
    if errors:
        print(f"  [WARN] dashboard.py - Applied with {len(errors)} warnings:")
        for e in errors:
            print(f"         - {e}")
    elif changed:
        print("  [OK]  dashboard.py - Depth changes applied")
    else:
        print("  [SKIP] dashboard.py - Depth changes already present")


def _get_depth_html():
    """Return the Market Depth HTML block."""
    return r"""    <!-- ═══ PAGE: MARKET DEPTH (Dhan 20-Level) ═══ -->
    {% if depth_enabled %}
    <div id="page-depth" style="display:none;">
        <div style="padding:20px 24px;">

            <!-- Connection Status Banner -->
            <div id="depth-disconnected" style="background:#3d2e00;border:1px solid #9e6a03;color:#d29922;padding:10px 20px;border-radius:8px;margin-bottom:16px;font-size:13px;display:none;">
                <strong>Disconnected</strong> — Depth feed not connected. Waiting for market hours or reconnecting...
            </div>

            <!-- Top Stats Row -->
            <div class="grid grid-main" style="margin-bottom:16px;">
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
            <div class="grid grid-detail">
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
    {% endif %}"""


def _get_depth_js():
    """Return the Dhan depth JavaScript block."""
    return r"""        // ═══ DHAN MARKET DEPTH (20-Level) ═══════════════════════════════
        (function() {
            if (!document.getElementById('page-depth')) return;

            var depthData = null;
            var deltaCanvas = document.getElementById('depth-delta-canvas');

            // Format quantity with commas
            function fmtQty(n) {
                if (!n) return '0';
                return n.toLocaleString('en-IN');
            }

            // Format price
            function fmtPrice(p) {
                if (!p) return '--';
                return p.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            }

            // Update imbalance bar (centered diverging bar)
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

                // Bars grow from center outward
                var absPct = Math.min(Math.abs(pct), 100);
                var barWidth = (absPct / 100) * 50; // max 50% of bar width
                if (pct >= 0) {
                    bidBar.style.width = barWidth + '%';
                    askBar.style.width = '0%';
                } else {
                    bidBar.style.width = '0%';
                    askBar.style.width = barWidth + '%';
                }
            }

            // Render order book table for one side
            function renderBookSide(bodyId, levels, side, walls) {
                var body = document.getElementById(bodyId);
                if (!body || !levels) return;

                var maxQty = 0;
                levels.forEach(function(l) { if (l.qty > maxQty) maxQty = l.qty; });

                // Build wall price set for quick lookup
                var wallPrices = {};
                if (walls) {
                    walls.forEach(function(w) { wallPrices[w.price] = w.strength; });
                }

                var html = '';
                levels.forEach(function(l, i) {
                    if (l.price <= 0) return;
                    var intensity = maxQty > 0 ? (l.qty / maxQty) : 0;
                    var isWall = wallPrices[l.price];
                    var bgColor, wallStyle = '';

                    if (side === 'bid') {
                        bgColor = 'rgba(63,185,80,' + (0.05 + intensity * 0.25) + ')';
                    } else {
                        bgColor = 'rgba(248,81,73,' + (0.05 + intensity * 0.25) + ')';
                    }

                    if (isWall) {
                        wallStyle = 'border-left:3px solid ' + (side === 'bid' ? '#3fb950' : '#f85149') + ';font-weight:700;';
                        bgColor = side === 'bid'
                            ? 'rgba(63,185,80,' + (0.15 + intensity * 0.35) + ')'
                            : 'rgba(248,81,73,' + (0.15 + intensity * 0.35) + ')';
                    }

                    var tdStyle = 'padding:3px 8px;font-family:monospace;font-size:12px;white-space:nowrap;';
                    var wallBadge = isWall ? ' <span style="font-size:10px;color:' + (side === 'bid' ? '#3fb950' : '#f85149') + ';">' + isWall.toFixed(1) + 'x</span>' : '';

                    if (side === 'bid') {
                        // Orders | Qty | Price (right-aligned, bid on left)
                        html += '<tr style="background:' + bgColor + ';' + wallStyle + '">'
                            + '<td style="' + tdStyle + 'text-align:right;color:#8b949e;">' + fmtQty(l.orders) + '</td>'
                            + '<td style="' + tdStyle + 'text-align:right;color:#3fb950;">' + fmtQty(l.qty) + wallBadge + '</td>'
                            + '<td style="' + tdStyle + 'text-align:right;color:#e0e6ed;">' + fmtPrice(l.price) + '</td>'
                            + '</tr>';
                    } else {
                        // Price | Qty | Orders (left-aligned, ask on right)
                        html += '<tr style="background:' + bgColor + ';' + wallStyle + '">'
                            + '<td style="' + tdStyle + 'color:#e0e6ed;">' + fmtPrice(l.price) + '</td>'
                            + '<td style="' + tdStyle + 'color:#f85149;">' + fmtQty(l.qty) + wallBadge + '</td>'
                            + '<td style="' + tdStyle + 'color:#8b949e;">' + fmtQty(l.orders) + '</td>'
                            + '</tr>';
                    }
                });

                body.innerHTML = html;
            }

            // Draw cumulative delta chart on canvas
            function drawDeltaChart(history) {
                if (!deltaCanvas) return;
                var ctx = deltaCanvas.getContext('2d');
                var w = deltaCanvas.width = deltaCanvas.offsetWidth;
                var h = deltaCanvas.height = 100;
                ctx.clearRect(0, 0, w, h);

                if (!history || history.length < 2) {
                    ctx.fillStyle = '#484f58';
                    ctx.font = '12px monospace';
                    ctx.fillText('Collecting data...', 10, h / 2);
                    return;
                }

                var values = history.map(function(d) { return d.d; });
                var min = Math.min.apply(null, values);
                var max = Math.max.apply(null, values);
                var range = max - min;
                if (range === 0) range = 1;

                // Zero line
                var zeroY = h - ((0 - min) / range) * (h - 10) - 5;
                ctx.strokeStyle = '#30363d';
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(0, zeroY);
                ctx.lineTo(w, zeroY);
                ctx.stroke();
                ctx.setLineDash([]);

                // Fill area
                var lastVal = values[values.length - 1];
                var fillColor = lastVal >= 0
                    ? 'rgba(63,185,80,0.1)'
                    : 'rgba(248,81,73,0.1)';
                var lineColor = lastVal >= 0 ? '#3fb950' : '#f85149';

                ctx.fillStyle = fillColor;
                ctx.beginPath();
                ctx.moveTo(0, zeroY);
                for (var i = 0; i < values.length; i++) {
                    var x = (i / (values.length - 1)) * w;
                    var y = h - ((values[i] - min) / range) * (h - 10) - 5;
                    ctx.lineTo(x, y);
                }
                ctx.lineTo(w, zeroY);
                ctx.closePath();
                ctx.fill();

                // Line
                ctx.strokeStyle = lineColor;
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                for (var j = 0; j < values.length; j++) {
                    var lx = (j / (values.length - 1)) * w;
                    var ly = h - ((values[j] - min) / range) * (h - 10) - 5;
                    if (j === 0) ctx.moveTo(lx, ly);
                    else ctx.lineTo(lx, ly);
                }
                ctx.stroke();
            }

            // Render alerts feed
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
                        icon = '&#9660;';
                        color = a.side === 'bid' ? '#f85149' : '#3fb950';
                        msg = (a.side === 'bid' ? 'Bid' : 'Ask') + ' wall at '
                            + fmtPrice(a.price) + ' absorbed ' + a.pct + '% ('
                            + fmtQty(a.prev_qty) + '&rarr;' + fmtQty(a.curr_qty) + ')';
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

            // Main update handler
            function handleDepthUpdate(data) {
                depthData = data;

                // Status banner
                var banner = document.getElementById('depth-disconnected');
                if (banner) banner.style.display = data.connected ? 'none' : 'block';

                // Top stats
                var symEl = document.getElementById('depth-symbol');
                if (symEl) symEl.textContent = data.symbol || '--';
                var expEl = document.getElementById('depth-expiry');
                if (expEl) expEl.textContent = 'Expiry: ' + (data.expiry || '--') + '  |  Lot: ' + (data.lot_size || '--');
                var ltpEl = document.getElementById('depth-ltp');
                if (ltpEl) ltpEl.textContent = data.ltp ? fmtPrice(data.ltp) : '--';
                var spreadEl = document.getElementById('depth-spread');
                if (spreadEl) spreadEl.textContent = 'Spread: ' + (data.spread != null ? fmtPrice(data.spread) : '--');
                var bidEl = document.getElementById('depth-total-bid');
                if (bidEl) bidEl.textContent = fmtQty(data.total_bid_qty);
                var askEl = document.getElementById('depth-total-ask');
                if (askEl) askEl.textContent = fmtQty(data.total_ask_qty);

                // Imbalance
                updateImbalance('imb5', data.imbalance_5);
                updateImbalance('imb10', data.imbalance_10);
                updateImbalance('imb20', data.imbalance_20);

                // Order book tables
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
                drawDeltaChart(data.delta_history);

                // Alerts
                renderAlerts(data.alerts);
            }

            // Listen for SocketIO depth updates
            function waitForSocket() {
                if (typeof io !== 'undefined' && window._socket) {
                    window._socket.on('depth_update', handleDepthUpdate);
                } else {
                    setTimeout(waitForSocket, 500);
                }
            }
            waitForSocket();

            // Also fetch initial snapshot on page load
            fetch('/api/depth/snapshot').then(function(r) { return r.json(); }).then(function(data) {
                if (data && data.symbol) handleDepthUpdate(data);
            }).catch(function() {});
        })();"""


def _get_depth_routes():
    """Return the Dhan depth Flask routes block."""
    return '''# ── Dhan Market Depth Routes ──────────────────────────────────────

@app.route("/api/depth/config")
def depth_config():
    """Get depth analyzer configuration."""
    if _depth_analyzer:
        return jsonify(_depth_analyzer.get_config())
    return jsonify({"running": False, "connected": False})


@app.route("/api/depth/snapshot")
def depth_snapshot():
    """Get current order book snapshot with analytics."""
    if _depth_analyzer:
        return jsonify(_depth_analyzer.get_snapshot())
    return jsonify({"symbol": "", "connected": False, "bids": [], "asks": []})


@app.route("/api/depth/reconnect", methods=["POST"])
def depth_reconnect():
    """Force reconnect the depth WebSocket (e.g. after token refresh)."""
    if _depth_analyzer:
        _depth_analyzer.reconnect()
        return jsonify({"status": "ok", "message": "Depth reconnect triggered"})
    return jsonify({"status": "error", "message": "Depth analyzer not running"}), 400

'''


def main():
    print("=" * 50)
    print("  Dhan Market Depth - Patch Installer")
    print("=" * 50)
    print()

    # Verify we're in the right directory
    if not os.path.exists(os.path.join(WORK_DIR, "dashboard.py")):
        print("ERROR: dashboard.py not found. Run this from the Risk-Management directory.")
        sys.exit(1)

    # Check dhan_depth.py exists
    if not os.path.exists(os.path.join(WORK_DIR, "dhan_depth.py")):
        print("ERROR: dhan_depth.py not found.")
        print("Run this first:")
        print("  git fetch origin claude/integrate-fyers-websockets-wDrFY")
        print("  git checkout origin/claude/integrate-fyers-websockets-wDrFY -- dhan_depth.py apply_depth_patch.py")
        sys.exit(1)

    # First: clean up any old Fyers DOM Analyzer code
    print("Checking for old Fyers code...")
    print()
    cleanup_fyers()
    print()

    print("Patching files...")
    print()

    patch_config()
    patch_dashboard()

    print()
    print("=" * 50)
    print("  Patch Complete!")
    print("=" * 50)
    print()
    print("Next steps:")
    print("  1. Restart the platform:")
    print("     bash stop.sh && bash start.sh")
    print()
    print("  2. Open your dashboard — you should see a 'Market Depth' tab")
    print()
    print("  3. DEPTH_ENABLED defaults to true. To disable, add to .env:")
    print("     DEPTH_ENABLED=false")
    print()


if __name__ == "__main__":
    main()
