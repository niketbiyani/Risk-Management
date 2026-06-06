"""
Analyse accuracy of rejection detection logic on real Nifty futures data.
For each detected rejection candle, checks if price actually moved in the
predicted direction over the next 3, 5, and 10 candles.
"""
import sys, os, json
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

# ── Find NIFTY near-month futures ─────────────────────────────────
nifty_futures = [
    i for i in cache._instruments
    if i.trading_symbol.upper().startswith('NIFTY')
    and i.instrument_type == 'FUTIDX'
    and i.exchange_segment == 'NSE_FNO'
]
nifty_futures.sort(key=lambda x: x.expiry_date or '')
today = date.today()
near_month = next(
    (f for f in nifty_futures if date.fromisoformat(f.expiry_date[:10]) >= today),
    nifty_futures[0]
)
security_id = str(near_month.security_id)
print(f"Using: {near_month.trading_symbol} (ID: {security_id})\n")

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
    raw  = api.get_chart_data(security_id, 'NSE_FNO', 'FUTIDX', ds, ds)
    data = raw.get('data', raw) if isinstance(raw, dict) else {}
    timestamps = data.get('timestamp', [])
    opens   = data.get('open',   [])
    highs   = data.get('high',   [])
    lows    = data.get('low',    [])
    closes  = data.get('close',  [])
    volumes = data.get('volume', [])
    for i, ts in enumerate(timestamps):
        try:
            if isinstance(ts, str):
                unix_ts = int(datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M:%S").timestamp())
            else:
                unix_ts = int(ts)
            all_candles.append({
                'time': unix_ts, 'open': float(opens[i]),
                'high': float(highs[i]), 'low': float(lows[i]),
                'close': float(closes[i]), 'volume': float(volumes[i]),
            })
        except: pass

all_candles.sort(key=lambda x: x['time'])
print(f"Total candles: {len(all_candles)}")
print(f"Price range: {min(c['low'] for c in all_candles):.0f} — {max(c['high'] for c in all_candles):.0f}\n")

# ── Detection parameters ──────────────────────────────────────────
VOL_MULTIPLIER = 1.8
VOL_LOOKBACK   = 20
BODY_RATIO     = 0.35

def rolling_avg_vol(candles, idx, lb=20):
    sub = candles[max(0,idx-lb):idx]
    return sum(c['volume'] for c in sub)/len(sub) if sub else candles[idx]['volume']

# ── Detect rejections ─────────────────────────────────────────────
rejections = []
for i, c in enumerate(all_candles):
    if i < 5 or i > len(all_candles) - 11:  # need 10 candles after
        continue
    rng = c['high'] - c['low']
    if rng < 1:
        continue
    avg_vol = rolling_avg_vol(all_candles, i)
    if c['volume'] <= avg_vol * VOL_MULTIPLIER:
        continue
    upper_wick = c['high'] - max(c['open'], c['close'])
    lower_wick = min(c['open'], c['close']) - c['low']

    direction = None
    if upper_wick > rng * 0.45 and (c['close'] - c['low']) < rng * BODY_RATIO:
        direction = 'down'  # bearish rejection
    elif lower_wick > rng * 0.45 and (c['high'] - c['close']) < rng * BODY_RATIO:
        direction = 'up'    # bullish rejection

    if direction:
        rejections.append({'idx': i, 'candle': c, 'direction': direction,
                           'vol_ratio': c['volume'] / avg_vol})

print(f"Detected {len(rejections)} rejection candles\n")

# ── Measure accuracy ──────────────────────────────────────────────
# For each rejection, check if price moved in predicted direction
# over next N candles. "Correct" = close of candle N is in predicted direction
# from the rejection candle's close. Also check max adverse excursion.

HORIZONS = [3, 5, 10]  # candles ahead to check

results = {h: {'correct':0,'wrong':0,'neutral':0} for h in HORIZONS}
detail  = []

for r in rejections:
    i   = r['idx']
    c   = r['candle']
    dir = r['direction']
    entry_price = c['close']

    row = {
        'time': datetime.fromtimestamp(c['time']).strftime('%d-%b %H:%M'),
        'direction': dir,
        'vol_ratio': round(r['vol_ratio'],1),
        'entry': round(entry_price,0),
        'horizons': {},
    }

    for h in HORIZONS:
        future = all_candles[i+1:i+h+1]
        if len(future) < h:
            continue
        future_close = future[-1]['close']
        move = future_close - entry_price
        max_adv = max(fc['high'] for fc in future) - entry_price  # how far up it went
        max_fav = entry_price - min(fc['low'] for fc in future)   # how far down it went

        if dir == 'down':
            correct = move < -5   # moved down by at least 5 pts
            wrong   = move > 5
            fav_move = -move      # negative move is favourable for bearish
            adv_move = move       # positive move is adverse
        else:
            correct = move > 5
            wrong   = move < -5
            fav_move = move
            adv_move = -move

        outcome = 'CORRECT' if correct else ('WRONG' if wrong else 'NEUTRAL')
        if correct:   results[h]['correct']  += 1
        elif wrong:   results[h]['wrong']    += 1
        else:         results[h]['neutral']  += 1

        row['horizons'][h] = {
            'outcome': outcome,
            'move': round(move, 0),
            'fav': round(fav_move, 0),
        }

    detail.append(row)

# ── Print results ─────────────────────────────────────────────────
print("=" * 70)
print("ACCURACY SUMMARY")
print("=" * 70)
for h in HORIZONS:
    r = results[h]
    total = r['correct'] + r['wrong'] + r['neutral']
    if total == 0: continue
    pct = round(r['correct']/total*100)
    print(f"  Next {h:2d} candles:  Correct={r['correct']}  Wrong={r['wrong']}  "
          f"Neutral={r['neutral']}  →  Accuracy: {pct}%")

print()
print("=" * 70)
print("DETAIL — each rejection")
print("=" * 70)
print(f"{'Time':<14} {'Dir':<6} {'Vol':<5} {'Entry':<7} "
      f"{'3c':>8} {'5c':>8} {'10c':>8}")
print("-" * 70)
for row in detail:
    h3  = row['horizons'].get(3,  {})
    h5  = row['horizons'].get(5,  {})
    h10 = row['horizons'].get(10, {})
    def fmt(h):
        if not h: return '   --  '
        o = h['outcome']
        m = h['move']
        tag = '✓' if o=='CORRECT' else ('✗' if o=='WRONG' else '~')
        return f"{tag}{m:+.0f}".rjust(7)
    print(f"{row['time']:<14} {'↓' if row['direction']=='down' else '↑':<6} "
          f"{row['vol_ratio']:<5} {row['entry']:<7.0f} "
          f"{fmt(h3):>8} {fmt(h5):>8} {fmt(h10):>8}")

print()
print("Legend: ✓=correct direction  ✗=wrong direction  ~=neutral (<5pts)")
print("        Number = price move in points from rejection candle close")
print(f"        Direction ↓=bearish rejection  ↑=bullish rejection")
