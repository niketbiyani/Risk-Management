"""
Fetch real ATM PE option 1m candles for last 3 trading days,
apply rejection detection logic, and generate a standalone HTML demo
with price chart (20 EMA + 50 EMA), volume, RSI(14), and MACD(12,26,9).
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
today      = date.today()
atm_strike = 23450  # from recent price range

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
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; max-width:1000px; }}
  .header {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px; }}
  h3 {{ margin:0; font-size:14px; color:#e6edf3; }}
  .chart-wrap {{ position:relative; }}
  .panel-label {{ position:absolute; top:4px; left:6px; font-size:10px; color:#484f58; z-index:10; pointer-events:none; }}
  #price-chart {{ width:100%; height:320px; }}
  #vol-chart   {{ width:100%; height:70px;  margin-top:1px; }}
  #rsi-chart   {{ width:100%; height:80px;  margin-top:1px; }}
  #macd-chart  {{ width:100%; height:85px;  margin-top:1px; }}
  .legend {{ font-size:11px; color:#8b949e; margin:6px 0; min-height:16px; }}
  .key {{ display:flex; gap:16px; margin:8px 0; font-size:11px; color:#8b949e; align-items:center; flex-wrap:wrap; }}
  .dot {{ width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:3px; }}
  .params {{ margin-top:10px; padding:8px 14px; border-radius:6px; background:#0d1117;
             border:1px solid #21262d; font-size:11px; color:#8b949e; line-height:1.8; }}
  .stats {{ margin-top:8px; padding:8px 14px; border-radius:6px; background:#0d1117;
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
      <div style="font-size:11px;color:#8b949e;margin-top:2px;">Real data from Dhan. Rejection logic applied before looking at outcome.</div>
    </div>
    <div style="font-size:11px;color:#8b949e;text-align:right;">
      {len(all_candles)} candles &nbsp;|&nbsp; {len(markers)} rejections
    </div>
  </div>

  <div class="legend" id="legend">Hover over chart</div>

  <div class="chart-wrap">
    <div class="panel-label">Price &nbsp; <span style="color:#58a6ff;">── 20 EMA</span> &nbsp; <span style="color:#d29922;">── 50 EMA</span></div>
    <div id="price-chart"></div>
  </div>
  <div class="chart-wrap">
    <div class="panel-label">Volume</div>
    <div id="vol-chart"></div>
  </div>
  <div class="chart-wrap">
    <div class="panel-label">RSI (14)</div>
    <div id="rsi-chart"></div>
  </div>
  <div class="chart-wrap">
    <div class="panel-label">MACD (12,26,9) &nbsp; <span style="color:#58a6ff;">── MACD</span> &nbsp; <span style="color:#f0883e;">── Signal</span></div>
    <div id="macd-chart"></div>
  </div>

  <div class="key">
    <span><span class="dot" style="background:#f85149"></span> Bearish rejection</span>
    <span><span class="dot" style="background:#3fb950"></span> Bullish rejection</span>
    <span style="color:#58a6ff;">── 20 EMA</span>
    <span style="color:#d29922;">── 50 EMA</span>
    <span style="color:#8b949e;">RSI: 70/50/30 reference lines</span>
  </div>

  <div class="stats" id="stats-box"></div>

  <div class="params">
    <span class="hl">Detection parameters:</span>
    Volume &gt; <span class="gold">{VOL_MULTIPLIER}x</span> {VOL_LOOKBACK}-bar rolling avg &nbsp;|&nbsp;
    Wick &gt; <span class="gold">45%</span> of range &nbsp;|&nbsp;
    Close within <span class="gold">{int(BODY_RATIO*100)}%</span> of wick tip
  </div>
</div>

<script>
var CANDLES = {candles_json};
var MARKERS = {markers_json};

// ── Indicator calculations ────────────────────────────────────────
function calcEMA(candles, period) {{
  var k = 2 / (period + 1), result = [];
  var ema = candles[0].close;
  for (var i = 0; i < candles.length; i++) {{
    if (i < period) {{
      ema = candles.slice(0, i+1).reduce(function(s,c){{return s+c.close;}}, 0) / (i+1);
    }} else {{
      ema = candles[i].close * k + ema * (1-k);
    }}
    if (i >= period-1) result.push({{ time: candles[i].time, value: parseFloat(ema.toFixed(3)) }});
  }}
  return result;
}}

function calcRSI(candles, period) {{
  var result = [], gains = 0, losses = 0;
  for (var i = 1; i <= period; i++) {{
    var d = candles[i].close - candles[i-1].close;
    if (d > 0) gains += d; else losses -= d;
  }}
  var avgG = gains/period, avgL = losses/period;
  for (var i = period; i < candles.length; i++) {{
    if (i > period) {{
      var d = candles[i].close - candles[i-1].close;
      avgG = (avgG*(period-1) + (d>0?d:0)) / period;
      avgL = (avgL*(period-1) + (d<0?-d:0)) / period;
    }}
    var rs  = avgL === 0 ? 100 : avgG/avgL;
    result.push({{ time: candles[i].time, value: parseFloat((100 - 100/(1+rs)).toFixed(2)) }});
  }}
  return result;
}}

function calcMACD(candles, fast, slow, signal) {{
  function ema(arr, p) {{
    var k = 2/(p+1), e = arr[0], res = [];
    for (var i = 0; i < arr.length; i++) {{
      e = i < p ? arr.slice(0,i+1).reduce(function(s,v){{return s+v;}},0)/(i+1) : arr[i]*k+e*(1-k);
      if (i >= p-1) res.push(e);
    }}
    return res;
  }}
  var closes  = candles.map(function(c){{return c.close;}});
  var fastEma = ema(closes, fast);
  var slowEma = ema(closes, slow);
  var offset  = slow - fast;
  var macdLine = fastEma.slice(offset).map(function(v,i){{return v - slowEma[i];}});
  var sigLine  = ema(macdLine, signal);
  var sigOffset = signal - 1;
  var startIdx  = slow - 1 + sigOffset;
  var result = {{ macd:[], signal:[], hist:[] }};
  for (var i = 0; i < sigLine.length; i++) {{
    var t = candles[startIdx + i].time;
    var m = parseFloat(macdLine[sigOffset+i].toFixed(3));
    var s = parseFloat(sigLine[i].toFixed(3));
    result.macd.push({{time:t, value:m}});
    result.signal.push({{time:t, value:s}});
    result.hist.push({{time:t, value:parseFloat((m-s).toFixed(3)), color: m>=s?'#3fb950':'#f85149'}});
  }}
  return result;
}}

// ── Chart factory ─────────────────────────────────────────────────
function makeChart(id, height, showTime) {{
  return LightweightCharts.createChart(document.getElementById(id), {{
    width: document.getElementById(id).clientWidth,
    height: height,
    layout: {{ background:{{ color:'#0d1117' }}, textColor:'#8b949e' }},
    grid: {{ vertLines:{{ color:'#161b22' }}, horzLines:{{ color:'#161b22' }} }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    rightPriceScale: {{ borderColor:'#21262d' }},
    timeScale: {{ borderColor:'#21262d', timeVisible:!!showTime, visible:!!showTime }},
  }});
}}

var pc = makeChart('price-chart', 320, false);
var vc = makeChart('vol-chart',    70, false);
var rc = makeChart('rsi-chart',    80, false);
var mc = makeChart('macd-chart',   85, true);

var allCharts = [pc, vc, rc, mc];

// ── Price + 20 EMA + 50 EMA ───────────────────────────────────────
var candleSeries = pc.addCandlestickSeries({{
  upColor:'#3fb950', downColor:'#f85149',
  borderUpColor:'#3fb950', borderDownColor:'#f85149',
  wickUpColor:'#3fb950', wickDownColor:'#f85149',
}});
var ema20Series = pc.addLineSeries({{ color:'#58a6ff', lineWidth:1, priceLineVisible:false, lastValueVisible:false }});
var ema50Series = pc.addLineSeries({{ color:'#d29922', lineWidth:1, priceLineVisible:false, lastValueVisible:false }});

candleSeries.setData(CANDLES);
ema20Series.setData(calcEMA(CANDLES, 20));
ema50Series.setData(calcEMA(CANDLES, 50));

// ── Rejection markers ─────────────────────────────────────────────
var rejTimes = {{}};
MARKERS.forEach(function(m) {{ rejTimes[m.time] = m; }});
candleSeries.setMarkers(MARKERS.map(function(m) {{
  return {{
    time: m.time,
    position: m.direction==='down' ? 'aboveBar' : 'belowBar',
    color: m.direction==='down' ? '#f85149' : '#3fb950',
    shape: m.direction==='down' ? 'arrowDown' : 'arrowUp',
    text: (m.direction==='down' ? '↓' : '↑') + Math.round(m.volume/m.avg_vol*10)/10 + 'x',
    size: 1,
  }};
}}));

// ── Volume ────────────────────────────────────────────────────────
var volSeries = vc.addHistogramSeries({{ priceFormat:{{ type:'volume' }}, priceScaleId:'' }});
function rollingAvg(arr, idx, lb) {{
  var sub = arr.slice(Math.max(0,idx-lb), idx);
  return sub.length ? sub.reduce(function(s,c){{return s+c.volume;}},0)/sub.length : arr[idx].volume;
}}
volSeries.setData(CANDLES.map(function(c,i) {{
  var isUp=c.close>=c.open, avg=rollingAvg(CANDLES,i,20), isHigh=c.volume>avg*{VOL_MULTIPLIER};
  var rej=rejTimes[c.time];
  return {{ time:c.time, value:c.volume,
    color: rej&&rej.direction==='down'?'#f85149': rej&&rej.direction==='up'?'#3fb950':
           isHigh&&isUp?'#3fb950': isHigh?'#f85149': isUp?'#1a4a1a':'#4a1a1a' }};
}}));

// ── RSI (14) ──────────────────────────────────────────────────────
var rsiSeries = rc.addLineSeries({{ color:'#e6edf3', lineWidth:1, priceLineVisible:false, lastValueVisible:true }});
rsiSeries.setData(calcRSI(CANDLES, 14));
rc.addLineSeries({{ color:'#f85149', lineWidth:1, lineStyle:2, priceLineVisible:false, lastValueVisible:false }})
  .setData(CANDLES.map(function(c){{return {{time:c.time,value:70}};}}));
rc.addLineSeries({{ color:'#30363d', lineWidth:1, lineStyle:2, priceLineVisible:false, lastValueVisible:false }})
  .setData(CANDLES.map(function(c){{return {{time:c.time,value:50}};}}));
rc.addLineSeries({{ color:'#3fb950', lineWidth:1, lineStyle:2, priceLineVisible:false, lastValueVisible:false }})
  .setData(CANDLES.map(function(c){{return {{time:c.time,value:30}};}}));

// ── MACD (12,26,9) ────────────────────────────────────────────────
var macdData   = calcMACD(CANDLES, 12, 26, 9);
var macdLine   = mc.addLineSeries({{ color:'#58a6ff', lineWidth:1, priceLineVisible:false, lastValueVisible:true }});
var sigLine    = mc.addLineSeries({{ color:'#f0883e', lineWidth:1, priceLineVisible:false, lastValueVisible:true }});
var histSeries = mc.addHistogramSeries({{ priceScaleId:'', priceLineVisible:false, lastValueVisible:false }});
macdLine.setData(macdData.macd);
sigLine.setData(macdData.signal);
histSeries.setData(macdData.hist);

// ── Fit & sync ────────────────────────────────────────────────────
allCharts.forEach(function(ch){{ ch.timeScale().fitContent(); }});

var _sync = false;
allCharts.forEach(function(ch) {{
  ch.subscribeCrosshairMove(function(p) {{
    if (_sync || !p.time) return;
    _sync = true;
    allCharts.forEach(function(other) {{
      if (other !== ch) other.setCrosshairPosition(0, p.time,
        other===vc ? volSeries : other===rc ? rsiSeries : macdLine);
    }});
    _sync = false;
  }});
}});

// ── Legend ────────────────────────────────────────────────────────
pc.subscribeCrosshairMove(function(param) {{
  if (!param.time || !param.seriesData) return;
  var c = param.seriesData.get(candleSeries);
  if (!c) return;
  var rej = rejTimes[param.time];
  var tag = rej ? (rej.direction==='down'
    ? ' <span style="color:#f85149">▼ BEARISH REJ ' + Math.round(rej.volume/rej.avg_vol*10)/10 + 'x vol</span>'
    : ' <span style="color:#3fb950">▲ BULLISH REJ ' + Math.round(rej.volume/rej.avg_vol*10)/10 + 'x vol</span>') : '';
  document.getElementById('legend').innerHTML =
    'O:<span style="color:#e6edf3"> ' + c.open.toFixed(1) + '</span>  ' +
    'H:<span style="color:#3fb950"> ' + c.high.toFixed(1) + '</span>  ' +
    'L:<span style="color:#f85149"> ' + c.low.toFixed(1)  + '</span>  ' +
    'C:<span style="color:#e6edf3"> ' + c.close.toFixed(1)+ '</span>' + tag;
}});

var bearCount = MARKERS.filter(function(m){{return m.direction==='down';}}).length;
var bullCount = MARKERS.filter(function(m){{return m.direction==='up';}}).length;
document.getElementById('stats-box').innerHTML =
  '<span class="hl">Rejections:</span> &nbsp;' +
  '<span class="bear">▼ ' + bearCount + ' bearish</span> &nbsp;|&nbsp; ' +
  '<span class="bull">▲ ' + bullCount + ' bullish</span> &nbsp;|&nbsp; ' +
  '<span style="color:#58a6ff;">── 20 EMA (blue)</span> &nbsp;' +
  '<span style="color:#d29922;">── 50 EMA (gold)</span>';
</script>
</body>
</html>"""

out = '/root/Risk-Management/demo_pe_rejections.html'
with open(out, 'w') as f:
    f.write(html)
print(f"\nGenerated: {out}")
