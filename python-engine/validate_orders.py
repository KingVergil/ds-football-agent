#!/usr/bin/env python3
"""
全面校验 & 修正历史订单的让球符号 — v5 (final).
策略: 从 features 的 Pinnacle/Crown/澳门 亚盘终盘"受"前缀判断方向，统一到 负=主让。
"""
import json, os, re, sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
BASE = os.path.join(os.path.dirname(__file__), 'data')
ROLES_DIR = os.path.join(BASE, 'roles')
FEAT_DIR = os.path.join(BASE, 'features')


def parse_ah_direction(lid):
    """Returns 'home_gives', 'home_receives', or None"""
    path = os.path.join(FEAT_DIR, f'{lid}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            feat = json.load(f)
        cf = feat.get('compact_fet', '')

        for source in ['亚盘:Pinnacle', '亚盘:Crown', '亚盘:澳门']:
            parts = cf.split(source)
            if len(parts) < 2:
                continue
            section = parts[1]
            for em in ['欧盘:', '大小球:', '必发欧盘', '亚盘:', '公平盘']:
                if em in section:
                    section = section.split(em)[0]

            lines = [l.strip() for l in section.split('\n') if 'Δt' in l and '/' in l]
            if not lines:
                continue
            last = lines[-1]
            m = re.search(r'([\d.]+)/([^/\d][^/]*?)/([\d.]+)/([\d.]+)', last)
            if not m:
                continue

            hc_text = m.group(2).strip()
            if hc_text.startswith('受'):
                return 'home_receives'
            else:
                return 'home_gives'
        return None
    except Exception:
        return None


def settle_one(o, score):
    if not re.match(r'^\d+:\d+$', score):
        return o
    hg, ag = map(int, score.split(":"))
    diff, total = hg - ag, hg + ag
    bt, pick = o.get("bet_type", ""), o.get("pick", "")
    hc = float(o.get("handicap") or 0)
    odds = float(o.get("odds") or 0)
    bet = float(o.get("bet_size") or 100)
    if bt == "胜平负":
        actual = "H" if hg > ag else ("A" if hg < ag else "D")
        hit = (pick == actual) if pick in ("H", "D", "A") else None
        ra = bet if hit is None else (bet * odds if hit else 0.0)
        pr = 0.0 if hit is None else (ra - bet)
    else:
        is_q = abs(hc % 0.5) > 0.001
        if not is_q:
            if bt == "亚盘":
                adj = diff + hc
                if adj == 0:
                    hit, ra, pr = None, bet, 0.0
                else:
                    win = (adj > 0) if pick == "H" else (adj < 0)
                    hit, ra, pr = (True, bet * (1 + odds), bet * odds) if win else (False, 0.0, -bet)
            else:
                if total == hc:
                    hit, ra, pr = None, bet, 0.0
                else:
                    win = (total > hc) if pick == "over" else (total < hc)
                    hit, ra, pr = (True, bet * (1 + odds), bet * odds) if win else (False, 0.0, -bet)
        else:
            hc1, hc2 = hc - 0.25, hc + 0.25

            def _half(hc):
                if bt == "亚盘":
                    adj = diff + hc
                    if adj == 0: return "push"
                    if pick == "H": return "win" if adj > 0 else "lose"
                    else: return "win" if adj < 0 else "lose"
                else:
                    if total == hc: return "push"
                    if pick == "over": return "win" if total > hc else "lose"
                    else: return "win" if total < hc else "lose"

            r1, r2 = _half(hc1), _half(hc2)
            hb = bet / 2
            ra = sum(hb * (1 + odds) if r == "win" else (hb if r == "push" else 0) for r in (r1, r2))
            pr = ra - bet
            hit = True if r1 == "win" and r2 == "win" else (False if r1 == "lose" and r2 == "lose" else None)
    o["hit"], o["return_amount"], o["profit"] = hit, round(ra, 2), round(pr, 2)
    o["settled_at"] = datetime.now().isoformat()
    return o


# ── Main ──
issues = []
uncertain = set()

for dog in sorted(os.listdir(ROLES_DIR)):
    rf = os.path.join(ROLES_DIR, dog, f'{dog}.json')
    if not os.path.exists(rf):
        continue
    with open(rf) as f:
        r = json.load(f)
    for o in r.get('orders', []):
        if o.get('bet_type') != '亚盘':
            continue
        hc = float(o.get('handicap') or 0)
        if hc == 0:
            continue

        lid = o.get('lota_id', '')
        direction = parse_ah_direction(lid)
        if direction is None:
            uncertain.add(lid)
            continue

        if direction == 'home_gives' and hc > 0:
            issues.append(dict(dog=dog, lid=lid, hc=hc, pick=o.get('pick'),
                               score=o.get('score', '?'), hit=o.get('hit'), profit=o.get('profit')))
        elif direction == 'home_receives' and hc < 0:
            issues.append(dict(dog=dog, lid=lid, hc=hc, pick=o.get('pick'),
                               score=o.get('score', '?'), hit=o.get('hit'), profit=o.get('profit')))

print(f"Wrong sign: {len(issues)}")
print(f"Uncertain (no AH data in features): {len(uncertain)}")

for i in issues:
    print(
        f"  {i['dog']} | {i['lid']} | pick={i['pick']} hc={i['hc']:+.2f} | score={i['score']} | {i['hit']} profit={i['profit']}")

# ── Fix ──
if issues:
    by_dog = defaultdict(list)
    for i in issues:
        by_dog[i['dog']].append(i['lid'])

    for dog, lids in by_dog.items():
        rf = os.path.join(ROLES_DIR, dog, f'{dog}.json')
        with open(rf) as f:
            r = json.load(f)
        old_cap = float(r.get('capital', r.get('initial_capital', 1000)))
        fixed = 0
        for o in r.get('orders', []):
            if o.get('lota_id') not in lids:
                continue
            if o.get('bet_type') != '亚盘':
                continue
            o['handicap'] = -float(o['handicap'])
            fixed += 1
            sc = o.get('score', '')
            if sc:
                settle_one(o, sc)
        if fixed > 0:
            init = float(r.get('initial_capital', 1000))
            tp = sum(o.get('profit', 0) or 0 for o in r.get('orders', []) if o.get('settled_at'))
            pb = sum(float(o.get('bet_size', 0) or 0) for o in r.get('orders', []) if not o.get('settled_at'))
            r['capital'] = round(init + tp - pb, 2)
            r['updated_at'] = datetime.now().isoformat()
            with open(rf, 'w') as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
            print(f"  Fixed {dog}: {fixed}, cap {old_cap:.0f}→{r['capital']:.0f} (Δ{r['capital'] - old_cap:+.0f})")

    print(f"\nTotal fixed: {sum(len(v) for v in by_dog.values())}")
else:
    print("\n✅ All verified orders correct!")
