"""
Fetch real ATM PE option 1m candles for last 3 trading days,
apply rejection detection logic, and generate a standalone HTML demo.
"""
import json, sys, os
from datetime import date, timedelta, datetime

sys.path.insert(0, '/root/Risk-Management')
os.chdir('/root/Risk-Management')

from dotenv import load_dotenv
load_dotenv()

from dhan_api import DhanAPI
from instrument_cache import InstrumentCache

api   = DhanAPI()
cache = InstrumentCache()
cache.load()

# ── Find ATM PE ───────────────────────────────────────────────────
today     = date.today()
atm_strike = 23450  # from recent price range

# Find nearest weekly expiry (Thursday)
d = today
while d.weekday() != 3:
    d += timedelta(days=1)
weekly_expiry = d.strftime('%Y-%m-%d')

pe_candidates = [
    i for i in cache._instruments
    if i.trading_symbol.upper().startswith('NIFTY')
    and i.instrument_type == 'OPTIDX'
    and i.option_type == 'PE'
    and i.exchange_segment == 'NSE_FNO'
    and abs(i.strike_price - atm_strike) < 100
]
pe_candidates.sort(key=lambda x: (abs(x.strike_price - atm_strike), x.expiry_date or ''))

if not pe_candidates:
    print("ERROR: No ATM PE found")
    sys.exit(1)

atm_pe      = pe_candidates[0]
security_id = str(atm_pe.security_id)
symbol      = atm_pe.trading_symbol
print(f"Using: {symbol} (ID: {security_id})")

# ── Fetch last 3 trading days ─────────────────────────────────────
def prev_trading_days(n):
    days, d = [], date.today()
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return list(reversed(days))

all_candles = []
for day in prev_trading_days(3):
    ds = day.strftime('%Y-%m-%d')
    print(f"  Fetching {ds}...", end='', flush=True)
    raw  = api.get_chart_data(security_id, 'NSE_FNO', 'OPTIDX', ds, ds)
    data = raw.get('data', raw) if isinstance(raw, dict) else {}
    timestamps = data.get('timestamp', [])
    opens   = data.get('open',   [])
    highs   = data.get('high',   [])
    lows    = data.get('low',    [])
    closes  = data.get('close',  [])
    volumes = data.get('volume', [])
    day_candles = []
    for i, ts in enumerate(timestamps):
        try:
            if isinstance(ts, str):
                unix_ts = int(datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M:%S").timestamp())
            else:
                unix_ts = int(ts)
            day_candles.append({
                'time': unix_ts, 'open': float(opens[i]),
                'high': float(highs[i]), 'low': float(lows[i]),
                'close': float(closes[i]), 'volume': float(volumes[i]),
            })
        except: pass
    print(f" {len(day_candles)} candles")
    all_candles.extend(day_candles)

all_candles.sort(key=lambda x: x['time'])
print(f"\nTotal candles: {len(all_candles)}")
print(f"Price range: {min(c['low'] for c in all_candles):.1f} — {max(c['high'] for c in all_candles):.1f}")

# ── Rejection detection ───────────────────────────────────────────
VOL_MULTIPLIER = 1.8
VOL_LOOKBACK   = 20
BODY_RATIO     = 0.35

def rolling_avg_vol(candles, idx, lb=20):
    sub = candles[max(0,idx-lb):idx]
    return sum(c['volume'] for c in sub)/len(sub) if sub else candles[idx]['volume']

markers = []
for i, c in enumerate(all_candles):
    if i < 5:
        continue
    rng = c['high'] - c['low']
    if rng < 0.3:
        continue
    avg_vol = rolling_avg_vol(all_candles, i)
    if c['volume'] <= avg_vol * VOL_MULTIPLIER:
        continue
    upper_wick = c['high'] - max(c['open'], c['close'])
    lower_wick = min(c['open'], c['close']) - c['low']
    direction  = None
    if upper_wick > rng * 0.45 and (c['close'] - c['low']) < rng * BODY_RATIO:
        direction = 'down'
    elif lower_wick > rng * 0.45 and (c['high'] - c['close']) < rng * BODY_RATIO:
        direction = 'up'
    if direction:
        markers.append({'time': c['time'], 'direction': direction,
                        'volume': c['volume'], 'avg_vol': avg_vol})

print(f"Detected {len(markers)} rejection candles")

# ── Generate HTML ─────────────────────────────────────────────────
candles_json = json.dumps(all_candles)
markers_json = json.dumps(markers)

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{symbol} — Rejection Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:monospace; padding:20px; margin:0; }}
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; max-width:960px; }}
  .header {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px; }}
  h3 {{ margin:0; font-size:14px; color:#e6edf3; }}
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
      {len(all_candles)} candles &nbsp;|&nbsp; {len(markers)} rejections
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

  <div class="stats" id="stats-box"></div>

  <div class="params">
    <span class="hl">Detection parameters:</span><br>
    Volume threshold: <span class="gold">{VOL_MULTIPLIER}x</span> rolling {VOL_LOOKBACK}-bar average &nbsp;|&nbsp;
    Wick ratio: <span class="gold">&gt;45%</span> of candle range &nbsp;|&nbsp;
    Body position: close within <span class="gold">{int(BODY_RATIO*100)}%</span> of wick tip
  </div>
</div>

<script>
var CANDLES = {candles_json};
var MARKERS = {markers_json};

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
  timeScale: {{ borderColor:'#21262d', visible:false }},
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

var rejTimes = {{}};
MARKERS.forEach(function(m) {{ rejTimes[m.time] = m; }});

function rollingAvg(arr, idx, lb) {{
  var sub = arr.slice(Math.max(0,idx-lb), idx);
  return sub.length ? sub.reduce(function(s,c){{return s+c.volume;}},0)/sub.length : arr[idx].volume;
}}

var volData = CANDLES.map(function(c,i) {{
  var isUp   = c.close >= c.open;
  var avg    = rollingAvg(CANDLES, i, 20);
  var isHigh = c.volume > avg * {VOL_MULTIPLIER};
  var rej    = rejTimes[c.time];
  var color;
  if (rej && rej.direction==='down')      color = '#f85149';
  else if (rej && rej.direction==='up')   color = '#3fb950';
  else if (isHigh && isUp)               color = '#3fb950';
  else if (isHigh)                       color = '#f85149';
  else                                   color = isUp ? '#1a4a1a' : '#4a1a1a';
  return {{ time:c.time, value:c.volume, color:color }};
}});

candleSeries.setData(CANDLES);
volSeries.setData(volData);

var lwMarkers = MARKERS.map(function(m) {{
  return {{
    time: m.time,
    position: m.direction==='down' ? 'aboveBar' : 'belowBar',
    color: m.direction==='down' ? '#f85149' : '#3fb950',
    shape: m.direction==='down' ? 'arrowDown' : 'arrowUp',
    text: m.direction==='down'
      ? 'Rej ↓ ' + Math.round(m.volume/m.avg_vol*10)/10 + 'x'
      : 'Rej ↑ ' + Math.round(m.volume/m.avg_vol*10)/10 + 'x',
    size: 1,
  }};
}});
candleSeries.setMarkers(lwMarkers);

pc.timeScale().fitContent();
vc.timeScale().fitContent();

pc.subscribeCrosshairMove(function(param) {{
  if (!param.time || !param.seriesData) return;
  var c = param.seriesData.get(candleSeries);
  if (!c) return;
  var rej = rejTimes[param.time];
  var tag = rej ? (rej.direction==='down'
    ? ' <span style="color:#f85149">▼ BEARISH REJ</span>'
    : ' <span style="color:#3fb950">▲ BULLISH REJ</span>') : '';
  document.getElementById('legend').innerHTML =
    'O:<span style="color:#e6edf3"> ' + c.open.toFixed(1) + '</span>  ' +
    'H:<span style="color:#3fb950"> ' + c.high.toFixed(1) + '</span>  ' +
    'L:<span style="color:#f85149"> ' + c.low.toFixed(1)  + '</span>  ' +
    'C:<span style="color:#e6edf3"> ' + c.close.toFixed(1)+ '</span>' + tag;
}});

var bearCount = MARKERS.filter(function(m){{return m.direction==='down';}}).length;
var bullCount = MARKERS.filter(function(m){{return m.direction==='up';}}).length;
document.getElementById('stats-box').innerHTML =
  '<span class="hl">Rejection summary:</span> &nbsp;' +
  '<span class="bear">▼ ' + bearCount + ' bearish</span> &nbsp;|&nbsp; ' +
  '<span class="bull">▲ ' + bullCount + ' bullish</span><br>' +
  '<span style="color:#8b949e;">Marker label = volume multiple vs 20-bar rolling average. Higher = stronger conviction.</span>';
</script>
</body>
</html>"""

out = '/root/Risk-Management/demo_pe_rejections.html'
with open(out, 'w') as f:
    f.write(html)
print(f"\nGenerated: {out}")
