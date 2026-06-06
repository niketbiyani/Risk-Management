"""
Same rejection analysis but on the ATM PE option chart.
Finds the nearest ATM strike based on current spot, fetches 3 days of 1m data.
"""
import sys, os
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

# ── Get Nifty spot to find ATM ────────────────────────────────────
print("Getting Nifty spot price...")
spot = 0
try:
    result = api.dhan.ticker_data({'NSE_EQ': [13]})
    data = result.get('data', {})
    for k, v in data.items():
        if isinstance(v, list) and v:
            spot = float(v[0].get('last_price', 0) or v[0].get('LTP', 0))
        elif isinstance(v, dict):
            spot = float(v.get('last_price', 0) or v.get('LTP', 0))
except Exception as e:
    print(f"  spot fetch failed: {e}")

if spot == 0:
    spot = 23450  # fallback from yesterday's range
    print(f"  Using fallback spot: {spot}")
else:
    print(f"  Spot: {spot:.0f}")

atm_strike = round(spot / 50) * 50
print(f"  ATM strike: {atm_strike}")

# ── Find near-month expiry ────────────────────────────────────────
today = date.today()

# Get weekly expiry (nearest Thursday)
def nearest_thursday():
    d = today
    while d.weekday() != 3:  # 3 = Thursday
        d += timedelta(days=1)
    return d

weekly_expiry = nearest_thursday()
expiry_str = weekly_expiry.strftime('%Y-%m-%d')
print(f"  Target expiry: {expiry_str} (weekly)")

# Find ATM PE in instrument cache
pe_candidates = [
    i for i in cache._instruments
    if i.trading_symbol.upper().startswith('NIFTY')
    and i.instrument_type == 'OPTIDX'
    and i.option_type == 'PE'
    and i.exchange_segment == 'NSE_FNO'
    and abs(i.strike_price - atm_strike) < 100
    and i.expiry_date and i.expiry_date[:10] == expiry_str
]

if not pe_candidates:
    # Try monthly expiry
    print("  No weekly expiry found, trying monthly...")
    pe_candidates = [
        i for i in cache._instruments
        if i.trading_symbol.upper().startswith('NIFTY')
        and i.instrument_type == 'OPTIDX'
        and i.option_type == 'PE'
        and i.exchange_segment == 'NSE_FNO'
        and abs(i.strike_price - atm_strike) < 100
    ]
    pe_candidates.sort(key=lambda x: x.expiry_date or '')

if not pe_candidates:
    print("ERROR: No ATM PE found")
    sys.exit(1)

# Pick exact ATM strike
pe_candidates.sort(key=lambda x: abs(x.strike_price - atm_strike))
atm_pe = pe_candidates[0]
security_id = str(atm_pe.security_id)
symbol = atm_pe.trading_symbol
print(f"\nUsing: {symbol} (ID: {security_id})\n")

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
if not all_candles:
    print("ERROR: No candle data")
    sys.exit(1)

print(f"\nTotal candles: {len(all_candles)}")
print(f"Price range: {min(c['low'] for c in all_candles):.1f} — {max(c['high'] for c in all_candles):.1f}\n")

# ── Detection (same parameters as futures test) ───────────────────
VOL_MULTIPLIER = 1.8
BODY_RATIO     = 0.35

def rolling_avg_vol(candles, idx, lb=20):
    sub = candles[max(0,idx-lb):idx]
    return sum(c['volume'] for c in sub)/len(sub) if sub else candles[idx]['volume']

rejections = []
for i, c in enumerate(all_candles):
    if i < 5 or i > len(all_candles) - 11:
        continue
    rng = c['high'] - c['low']
    if rng < 0.3:
        continue
    avg_vol = rolling_avg_vol(all_candles, i)
    if c['volume'] <= avg_vol * VOL_MULTIPLIER:
        continue
    upper_wick = c['high'] - max(c['open'], c['close'])
    lower_wick = min(c['open'], c['close']) - c['low']

    direction = None
    if upper_wick > rng * 0.45 and (c['close'] - c['low']) < rng * BODY_RATIO:
        direction = 'down'
    elif lower_wick > rng * 0.45 and (c['high'] - c['close']) < rng * BODY_RATIO:
        direction = 'up'

    if direction:
        rejections.append({'idx': i, 'candle': c, 'direction': direction,
                           'vol_ratio': c['volume'] / avg_vol})

print(f"Detected {len(rejections)} rejection candles\n")

# ── Accuracy ──────────────────────────────────────────────────────
HORIZONS  = [3, 5, 10]
THRESHOLD = 0.5   # option moves in smaller increments than futures
results   = {h: {'correct':0,'wrong':0,'neutral':0} for h in HORIZONS}
detail    = []

for r in rejections:
    i, c, dir = r['idx'], r['candle'], r['direction']
    entry = c['close']
    row   = {'time': datetime.fromtimestamp(c['time']).strftime('%d-%b %H:%M'),
             'direction': dir, 'vol_ratio': round(r['vol_ratio'],1),
             'entry': round(entry,1), 'horizons': {}}

    for h in HORIZONS:
        future = all_candles[i+1:i+h+1]
        if len(future) < h:
            continue
        move = future[-1]['close'] - entry
        correct = (dir=='down' and move < -THRESHOLD) or (dir=='up' and move > THRESHOLD)
        wrong   = (dir=='down' and move > THRESHOLD)  or (dir=='up' and move < -THRESHOLD)
        outcome = 'CORRECT' if correct else ('WRONG' if wrong else 'NEUTRAL')
        if correct: results[h]['correct'] += 1
        elif wrong: results[h]['wrong']   += 1
        else:       results[h]['neutral'] += 1
        row['horizons'][h] = {'outcome': outcome, 'move': round(move, 1)}

    detail.append(row)

# ── Print ─────────────────────────────────────────────────────────
print("=" * 65)
print("ACCURACY SUMMARY — ATM PE")
print("=" * 65)
for h in HORIZONS:
    r = results[h]
    total = r['correct'] + r['wrong'] + r['neutral']
    if not total: continue
    pct = round(r['correct']/total*100)
    print(f"  Next {h:2d} candles:  Correct={r['correct']}  Wrong={r['wrong']}  "
          f"Neutral={r['neutral']}  →  Accuracy: {pct}%")

print()
print("=" * 65)
print("DETAIL")
print("=" * 65)
print(f"{'Time':<14} {'Dir':<5} {'Vol':<5} {'Entry':<8} {'3c':>7} {'5c':>7} {'10c':>7}")
print("-" * 65)
for row in detail:
    def fmt(h):
        hd = row['horizons'].get(h, {})
        if not hd: return '  --  '
        o, m = hd['outcome'], hd['move']
        tag = '✓' if o=='CORRECT' else ('✗' if o=='WRONG' else '~')
        return f"{tag}{m:+.1f}".rjust(7)
    print(f"{row['time']:<14} {'↓' if row['direction']=='down' else '↑':<5} "
          f"{row['vol_ratio']:<5} {row['entry']:<8.1f}"
          f"{fmt(3):>7} {fmt(5):>7} {fmt(10):>7}")

print()
print("Legend: ✓=correct  ✗=wrong  ~=neutral (|move|<0.5pts)")
