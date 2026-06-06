"""
Fetch real Nifty futures 1m candles for last 3 trading days,
apply rejection detection logic, and generate a standalone HTML demo.
"""
import json
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, '/home/user/Risk-Management')
os.chdir('/home/user/Risk-Management')

from dotenv import load_dotenv
load_dotenv()

from dhan_api import DhanAPI
from instrument_cache import InstrumentCache

api = DhanAPI()

# ── Find Nifty near-month futures security ID ──────────────────────
print("Loading instrument cache...")
cache = InstrumentCache()
cache.load()

# Search for NIFTY FUTIDX instruments using InstrumentCache's _instruments list
nifty_futures = [
    i for i in cache._instruments
    if i.trading_symbol.upper().startswith('NIFTY')
    and i.instrument_type == 'FUTIDX'
    and i.exchange_segment == 'NSE_FNO'
]

# Sort by expiry to get near month first
nifty_futures.sort(key=lambda x: x.expiry_date or '')
print(f"Found {len(nifty_futures)} NIFTY FUTIDX contracts")
for f in nifty_futures[:5]:
    print(f"  {f.trading_symbol} — ID: {f.security_id} — Expiry: {f.expiry_date}")

if not nifty_futures:
    print("ERROR: No Nifty futures found")
    sys.exit(1)

# Use near-month (first expiry on or after today)
today = date.today()
near_month = None
for f in nifty_futures:
    try:
        exp_date = date.fromisoformat(f.expiry_date[:10])
        if exp_date >= today:
            near_month = f
            break
    except:
        pass

if not near_month:
    near_month = nifty_futures[0]

security_id = str(near_month.security_id)
symbol = near_month.trading_symbol
print(f"\nUsing: {symbol} (ID: {security_id})")

# ── Get last 3 trading days ────────────────────────────────────────
def prev_trading_days(n):
    days = []
    d = date.today()
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
    return list(reversed(days))

trading_days = prev_trading_days(3)
print(f"\nFetching data for: {[str(d) for d in trading_days]}")

# ── Fetch candles ──────────────────────────────────────────────────
all_candles = []

for day in trading_days:
    ds = day.strftime('%Y-%m-%d')
    print(f"  Fetching {ds}...", end='', flush=True)
    raw = api.get_chart_data(
        security_id=security_id,
        exchange_segment='NSE_FNO',
        instrument_type='FUTIDX',
        from_date=ds,
        to_date=ds,
    )

    data = raw.get('data', raw) if isinstance(raw, dict) else {}
    if not data:
        print(f" empty response: {raw}")
        continue

    timestamps = data.get('timestamp', data.get('start_Time', []))
    opens   = data.get('open',   [])
    highs   = data.get('high',   [])
    lows    = data.get('low',    [])
    closes  = data.get('close',  [])
    volumes = data.get('volume', [])

    if not timestamps:
        print(f" no timestamps. Keys: {list(data.keys())}")
        continue

    day_candles = []
    for i, ts in enumerate(timestamps):
        try:
            # ts may be "09:15:00" string or unix int
            if isinstance(ts, str):
                from datetime import datetime
                dt = datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M:%S")
                unix_ts = int(dt.timestamp())
            else:
                unix_ts = int(ts)

            day_candles.append({
                'time':   unix_ts,
                'open':   float(opens[i])   if i < len(opens)   else 0,
                'high':   float(highs[i])   if i < len(highs)   else 0,
                'low':    float(lows[i])    if i < len(lows)    else 0,
                'close':  float(closes[i])  if i < len(closes)  else 0,
                'volume': float(volumes[i]) if i < len(volumes) else 0,
            })
        except Exception as e:
            pass

    print(f" {len(day_candles)} candles")
    all_candles.extend(day_candles)

if not all_candles:
    print("ERROR: No candle data fetched")
    sys.exit(1)

all_candles.sort(key=lambda x: x['time'])
print(f"\nTotal candles: {len(all_candles)}")
print(f"Price range: {min(c['low'] for c in all_candles):.0f} — {max(c['high'] for c in all_candles):.0f}")

# ── Rejection Detection Logic ──────────────────────────────────────
# Parameters (defined upfront, not tuned to outcome)
VOL_MULTIPLIER    = 1.8   # candle volume must be > 1.8x rolling avg to be "high volume"
VOL_LOOKBACK      = 20    # bars for rolling volume average
BODY_RATIO        = 0.35  # close must be within 35% of candle range from the wick tip
                          # (i.e. candle rejected — long wick, small body at opposite end)
LEVEL_CLUSTER_PCT = 0.003 # levels within 0.3% of each other are the same level
MIN_REJECTIONS    = 2     # need at least 2 rejections to draw a key level line

def rolling_avg_vol(candles, idx, lookback):
    start = max(0, idx - lookback)
    subset = candles[start:idx]
    if not subset:
        return candles[idx]['volume']
    return sum(c['volume'] for c in subset) / len(subset)

markers = []
level_touches = {}  # price_level -> list of candle indices

for i, c in enumerate(all_candles):
    if i < 5:
        continue

    rng = c['high'] - c['low']
    if rng < 0.5:
        continue

    avg_vol = rolling_avg_vol(all_candles, i, VOL_LOOKBACK)
    is_high_vol = c['volume'] > avg_vol * VOL_MULTIPLIER

    if not is_high_vol:
        continue

    upper_wick = c['high'] - max(c['open'], c['close'])
    lower_wick = min(c['open'], c['close']) - c['low']

    # Bearish rejection: price spiked high but closed near low
    # Long upper wick, close in lower portion of range
    if upper_wick > rng * 0.45 and (c['close'] - c['low']) < rng * BODY_RATIO:
        level = round(c['high'] / 50) * 50  # snap to nearest 50
        if level == 0:
            level = c['high']
        markers.append({
            'time': c['time'],
            'direction': 'down',
            'price': c['high'],
            'level': level,
            'volume': c['volume'],
            'avg_vol': avg_vol,
        })
        key = str(int(level))
        level_touches.setdefault(key, []).append(i)

    # Bullish rejection: price spiked low but closed near high
    # Long lower wick, close in upper portion of range
    elif lower_wick > rng * 0.45 and (c['high'] - c['close']) < rng * BODY_RATIO:
        level = round(c['low'] / 50) * 50
        if level == 0:
            level = c['low']
        markers.append({
            'time': c['time'],
            'direction': 'up',
            'price': c['low'],
            'level': level,
            'volume': c['volume'],
            'avg_vol': avg_vol,
        })
        key = str(int(level))
        level_touches.setdefault(key, []).append(i)

# Key levels = snapped price levels touched 2+ times
key_levels = [
    int(k) for k, v in level_touches.items()
    if len(v) >= MIN_REJECTIONS
]

print(f"\nDetected {len(markers)} rejection candles")
print(f"Key levels (2+ rejections): {key_levels}")

# ── Generate HTML ──────────────────────────────────────────────────
candles_json  = json.dumps(all_candles)
markers_json  = json.dumps(markers)
levels_json   = json.dumps(key_levels)
params_json   = json.dumps({
    'vol_multiplier': VOL_MULTIPLIER,
    'vol_lookback': VOL_LOOKBACK,
    'body_ratio': BODY_RATIO,
    'level_cluster_pct': LEVEL_CLUSTER_PCT,
    'min_rejections': MIN_REJECTIONS,
})

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Nifty Futures — Real Rejection Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:monospace; padding:20px; margin:0; }}
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; max-width:960px; }}
  .header {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px; }}
  h3 {{ margin:0; font-size:14px; color:#e6edf3; }}
  .subtitle {{ font-size:11px; color:#8b949e; margin-bottom:12px; line-height:1.6; }}
  #price-chart {{ width:100%; height:380px; }}
  #vol-chart   {{ width:100%; height:90px; margin-top:2px; }}
  .legend {{ font-size:11px; color:#8b949e; margin:6px 0; min-height:16px; }}
  .key {{ display:flex; gap:20px; margin:10px 0; font-size:11px; color:#8b949e; align-items:center; flex-wrap:wrap; }}
  .dot {{ width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:3px; }}
  .params {{ margin-top:12px; padding:10px 14px; border-radius:6px; background:#0d1117;
             border:1px solid #21262d; font-size:11px; color:#8b949e; line-height:1.8; }}
  .stats {{ margin-top:10px; padding:10px 14px; border-radius:6px; background:#0d1117;
            border:1px solid #21262d; font-size:11px; line-height:1.8; }}
  .bear {{ color:#f85149; }} .bull {{ color:#3fb950; }} .gold {{ color:#d29922; }}
  .hl {{ color:#e6edf3; font-weight:700; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div>
      <h3>{symbol} — Last 3 Trading Days — 1m candles</h3>
      <div style="font-size:11px;color:#8b949e;margin-top:2px;">Real data from Dhan. Logic applied before looking at outcome.</div>
    </div>
    <div style="font-size:11px;color:#8b949e;text-align:right;">
      {len(all_candles)} candles &nbsp;|&nbsp; {len(markers)} rejections<br>
      {len(key_levels)} key levels
    </div>
  </div>

  <div class="legend" id="legend">Hover over chart</div>
  <div id="price-chart"></div>
  <div id="vol-chart"></div>

  <div class="key">
    <span><span class="dot" style="background:#f85149"></span> Bearish rejection (high vol, long upper wick)</span>
    <span><span class="dot" style="background:#3fb950"></span> Bullish rejection (high vol, long lower wick)</span>
    <span style="color:#58a6ff;">▌ Bright bar = high volume (&gt;{VOL_MULTIPLIER}x avg) &nbsp;|&nbsp; Dim bar = normal volume</span>
  </div>

  <div class="stats" id="stats-box">
    <span class="hl">Rejection summary:</span>
    &nbsp;Loading...
  </div>

  <div class="params">
    <span class="hl">Detection parameters (set before seeing data):</span><br>
    Volume threshold: <span class="gold">{VOL_MULTIPLIER}x</span> rolling {VOL_LOOKBACK}-bar average &nbsp;|&nbsp;
    Wick ratio: <span class="gold">&gt;45%</span> of candle range &nbsp;|&nbsp;
    Body position: close within <span class="gold">{int(BODY_RATIO*100)}%</span> of wick tip &nbsp;|&nbsp;
    Key level: <span class="gold">{MIN_REJECTIONS}+</span> rejections at same level
  </div>
</div>

<script>
var CANDLES  = {candles_json};
var MARKERS  = {markers_json};
var LEVELS   = {levels_json};

// ── Charts ────────────────────────────────────────────────────────
var pc = LightweightCharts.createChart(document.getElementById('price-chart'), {{
  width: document.getElementById('price-chart').clientWidth,
  height: 380,
  layout: {{ background: {{ color:'#0d1117' }}, textColor:'#8b949e' }},
  grid: {{ vertLines:{{ color:'#161b22' }}, horzLines:{{ color:'#161b22' }} }},
  crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
  rightPriceScale: {{ borderColor:'#21262d' }},
  timeScale: {{ borderColor:'#21262d', timeVisible:true }},
}});

var vc = LightweightCharts.createChart(document.getElementById('vol-chart'), {{
  width: document.getElementById('vol-chart').clientWidth,
  height: 90,
  layout: {{ background: {{ color:'#0d1117' }}, textColor:'#8b949e' }},
  grid: {{ vertLines:{{ color:'#161b22' }}, horzLines:{{ color:'#161b22' }} }},
  crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
  rightPriceScale: {{ borderColor:'#21262d', scaleMargins:{{ top:0.1, bottom:0 }} }},
  timeScale: {{ borderColor:'#21262d', timeVisible:false, visible:false }},
}});

pc.subscribeCrosshairMove(function(p) {{
  if (p.time) vc.setCrosshairPosition(0, p.time, volSeries);
}});

var candleSeries = pc.addCandlestickSeries({{
  upColor:'#3fb950', downColor:'#f85149',
  borderUpColor:'#3fb950', borderDownColor:'#f85149',
  wickUpColor:'#3fb950', wickDownColor:'#f85149',
}});
var volSeries = vc.addHistogramSeries({{ priceFormat:{{ type:'volume' }}, priceScaleId:'' }});

// ── Volume colouring ──────────────────────────────────────────────
var rejTimes = {{}};
MARKERS.forEach(function(m) {{ rejTimes[m.time] = m.direction; }});

// Rolling avg for colour threshold
function rollingAvg(arr, idx, lb) {{
  var start = Math.max(0, idx-lb);
  var sub = arr.slice(start, idx);
  if (!sub.length) return arr[idx].volume;
  return sub.reduce(function(s,c){{return s+c.volume;}},0)/sub.length;
}}

var volData = CANDLES.map(function(c,i) {{
  var isUp = c.close >= c.open;
  var avg = rollingAvg(CANDLES, i, 20);
  var isHigh = c.volume > avg * {VOL_MULTIPLIER};
  var dir = rejTimes[c.time];
  var color;
  if (dir === 'down')      color = '#f85149';
  else if (dir === 'up')   color = '#3fb950';
  else if (isHigh && isUp) color = '#3fb950';
  else if (isHigh)         color = '#f85149';
  else                     color = isUp ? '#1a4a1a' : '#4a1a1a';
  return {{ time:c.time, value:c.volume, color:color }};
}});

candleSeries.setData(CANDLES);
volSeries.setData(volData);

// ── Markers ───────────────────────────────────────────────────────
var lwMarkers = MARKERS.map(function(m) {{
  return {{
    time: m.time,
    position: m.direction === 'down' ? 'aboveBar' : 'belowBar',
    color: m.direction === 'down' ? '#f85149' : '#3fb950',
    shape: m.direction === 'down' ? 'arrowDown' : 'arrowUp',
    text: m.direction === 'down'
      ? 'Rej ↓ ' + Math.round(m.volume/m.avg_vol*10)/10 + 'x vol'
      : 'Rej ↑ ' + Math.round(m.volume/m.avg_vol*10)/10 + 'x vol',
    size: 1,
  }};
}});
candleSeries.setMarkers(lwMarkers);

// ── Key level lines ───────────────────────────────────────────────
// Removed — snapping to nearest 50 creates too many lines at arbitrary levels

pc.timeScale().fitContent();
vc.timeScale().fitContent();

// ── Legend ────────────────────────────────────────────────────────
pc.subscribeCrosshairMove(function(param) {{
  if (!param.time || !param.seriesData) return;
  var c = param.seriesData.get(candleSeries);
  if (!c) return;
  var dir = param.seriesData ? rejTimes[param.time] : null;
  var tag = dir === 'down' ? ' <span style="color:#f85149">▼ REJECTION</span>'
           : dir === 'up'  ? ' <span style="color:#3fb950">▲ REJECTION</span>' : '';
  document.getElementById('legend').innerHTML =
    'O:<span style="color:#e6edf3"> ' + c.open.toFixed(0) + '</span>  ' +
    'H:<span style="color:#3fb950"> ' + c.high.toFixed(0) + '</span>  ' +
    'L:<span style="color:#f85149"> ' + c.low.toFixed(0) + '</span>  ' +
    'C:<span style="color:#e6edf3"> ' + c.close.toFixed(0) + '</span>' + tag;
}});

// ── Stats ─────────────────────────────────────────────────────────
var bearCount = MARKERS.filter(function(m){{return m.direction==='down';}}).length;
var bullCount = MARKERS.filter(function(m){{return m.direction==='up';}}).length;
document.getElementById('stats-box').innerHTML =
  '<span class="hl">Rejection summary:</span> &nbsp;' +
  '<span class="bear">▼ ' + bearCount + ' bearish</span> &nbsp;|&nbsp; ' +
  '<span class="bull">▲ ' + bullCount + ' bullish</span> &nbsp;|&nbsp; ' +
  '<br>' +
  '<span style="color:#8b949e;">Each marker label shows volume multiple (e.g. "2.3x vol" = 2.3× average volume at that moment). ' +
  'Higher multiple = stronger conviction.</span>';
</script>
</body>
</html>"""

out_path = '/home/user/Risk-Management/demo_futures_rejections.html'
with open(out_path, 'w') as f:
    f.write(html)

print(f"\nGenerated: {out_path}")
