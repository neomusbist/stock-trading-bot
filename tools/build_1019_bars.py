import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime

tick_file = Path(r"C:\Users\MJS\Desktop\주식자동매매\runtime\shadow_ticks.jsonl.3")
symbol_minute_ticks = defaultdict(lambda: defaultdict(list))

print("Reading shadow_ticks.jsonl.3...")
with open(tick_file, 'r', encoding='utf-8') as f:
    for line in f:
        try:
            d = json.loads(line)
            sym = d.get('symbol')
            t_str = d.get('time') or d.get('received')
            price = d.get('price')
            vol = d.get('volume', 0)
            if not sym or not t_str or not price:
                continue
            
            # Filter regular session up to 15:30:00
            # 2026-10-19T14:29:13+09:00 -> minute bucket 2026-10-19T14:29:00+09:00
            minute_str = t_str[:16] + ":00+09:00"
            if "2026-10-19T14:" in minute_str or "2026-10-19T15:" in minute_str:
                if minute_str <= "2026-10-19T15:30:00+09:00":
                    symbol_minute_ticks[sym][minute_str].append((float(price), int(vol)))
        except Exception:
            continue

# Aggregate into bars
rows_1019 = {}
for sym, m_dict in symbol_minute_ticks.items():
    bars = []
    for m_time in sorted(m_dict.keys()):
        ticks = m_dict[m_time]
        o = ticks[0][0]
        h = max(t[0] for t in ticks)
        l = min(t[0] for t in ticks)
        c = ticks[-1][0]
        v = sum(t[1] for t in ticks)
        bars.append({
            'time': m_time,
            'open': o,
            'high': h,
            'low': l,
            'close': c,
            'volume': v
        })
    if len(bars) >= 15:
        rows_1019[sym] = {'bars': bars}

print(f"Aggregated {len(rows_1019)} symbols with >= 15 bars on 2026-10-19 afternoon.")

# Save to audit_data
out_file = Path(r"C:\Users\MJS\Documents\주식자동매매_개발\r6-capture-consistency-20261020\audit_data\price_context_cache_20261019.json")
with open(out_file, 'w', encoding='utf-8') as f:
    json.dump({'rows': rows_1019, 'date': '2026-10-19'}, f, indent=2)

print(f"Saved to {out_file}")
