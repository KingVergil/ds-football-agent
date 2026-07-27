# live-asian-analyzer

走地亚盘双数据源分析。结合 compact-fet（赛前全维数据）和 AS_ODDS（走地水位时序），判断当前走地亚盘方向。

## 用法

用户提供 lota_id 或"主队 vs 客队"即可触发。

## 数据获取

```bash
python -c "
from src.data_manager import _get, DataManager
from src.tools import extract_odds
import json, re

dm = DataManager()
lid = '${LOTA_ID}'

# === 1. compact-fet 赛前 ===
ctx = dm.get_match_context(lid)
odds = extract_odds(lid)
eu = odds.get('eu',{})
asian = odds.get('asian',{})
ou = odds.get('ou',{})

fo = dm.get_sections(lid, ['fair-odds'])
do = dm.get_sections(lid, ['discrete-odds'])
euo = dm.get_sections(lid, ['eu-odds-pinnacle'])

diff_m = re.search(r'实力差:([-\d.]+)', fo)
eh = re.search(r'预期主队进球:([\d.]+)', fo)
ea = re.search(r'预期客队进球:([\d.]+)', fo)

print('=== COMPACT-FET (赛前) ===')
print(f'实力差:{float(diff_m.group(1)):+.2f}' if diff_m else '')
print(f'预期进球 H{eh.group(1)} vs A{ea.group(1)}' if eh else '')
if eu: print(f'欧赔终: H{eu[\"h\"]}/D{eu[\"d\"]}/A{eu[\"a\"]}')
if asian: print(f'亚盘终: H{asian[\"h\"]}/{asian[\"handicap_text\"]}({asian[\"handicap\"]:+.2f})/A{asian[\"a\"]}')
if ou: print(f'大小终: O{ou[\"over\"]}/{ou[\"threshold_text\"]}/U{ou[\"under\"]}')

disc_lines = [l for l in do.split('\n') if 'Δt' in l]
for dl in disc_lines[-3:]:
    m2 = re.search(r'Δt\\+\\d+m[↑↓→]+\s*([\d.]+)/([\d.]+)/([\d.]+)', dl)
    if m2:
        hf='🔥' if float(m2.group(1))<2 else ('✅' if float(m2.group(1))<3 else '  ')
        df='🔥' if float(m2.group(2))<2 else ('✅' if float(m2.group(2))<3 else '  ')
        af='🔥' if float(m2.group(3))<2 else ('✅' if float(m2.group(3))<3 else '  ')
        print(f'  离散 {hf}H{m2.group(1)} {df}D{m2.group(2)} {af}A{m2.group(3)}')

# 欧赔漂移
eu_lines = [l for l in euo.split('\n') if 'Δt' in l or 'OPt' in l]
if len(eu_lines)>=2:
    fn = re.findall(r'([\d.]+)', eu_lines[0])
    ln = re.findall(r'([\d.]+)', eu_lines[-1])
    if len(fn)>=3 and len(ln)>=3:
        hd, ad = float(ln[0])-float(fn[0]), float(ln[2])-float(fn[2])
        if abs(hd)>0.02 or abs(ad)>0.02: print(f'赔率漂移: H{hd:+.2f} A{ad:+.2f}')

# === 2. AS_ODDS 走地 ===
as_odds = _get('/matches/', {'lota_id': lid, 'include_odds': '1', 'odds_type': 'asia'})
m = as_odds['data']['matches'][0]
print(f'\n=== AS_ODDS (走地) ===')
print(f'状态: {m.get(\"state_name\",\"?\")}  比分: {m.get(\"score\",\"未开\")}')

odds_data = m.get('asia_odds_data', [])
kickoff = m.get('match_time', '')
live = [(o.get('handicapTime','')[-8:], o.get('score','?'), o.get('handicap','?'),
         o.get('home','?'), o.get('away','?'))
        for o in odds_data if o.get('score') and o.get('handicapTime','') >= kickoff]
live.sort()

# 进球+变盘事件
prev_sc, prev_hc = live[0][1], live[0][2] if live else ('0-0', '?')
print(f'{\"时间\":<10} {\"比分\":<6} {\"盘口\":<8} {\"主水\":<6} {\"客水\":<6} {\"事件\"}')
print('-'*60)
for t, sc, hc, h, a in live:
    evt = ''
    if sc != prev_sc: evt = f'⚽进球 {prev_sc}→{sc}'
    if hc != prev_hc: evt += f'  ⚠{prev_hc}→{hc}'
    if evt: print(f'{t:<10} {sc:<6} {hc:<8} {h:<6} {a:<6} {evt}')
    prev_sc, prev_hc = sc, hc

# 当前
last = live[-1]
print(f'\n→ 当前 [{last[0]}] 比分={last[1]} 盘口={last[2]} H{last[3]}/A{last[4]}')
"
```

## 分析框架

获取数据后，按以下步骤分析：

### 1. 赛前判断（compact-fet）
- 离散凝聚方向：H/D/A 哪个<3？
- 实力差是否匹配盘口？
- 赔率漂移方向？

### 2. 走地判断（AS_ODDS）
- **进球速度 vs 盘口降速**：进球后盘口降得比进球慢 = 市场看好还能进（追上盘）；降得比进球快 = 市场认为进球是运气（追下盘）
- **变盘方向**：升盘=看好让球方，降盘=看衰
- **水位**：主水>1.05 超高 = 不看好主方方向

### 3. 决策
- 赛前+走地一致 → 坚定方向
- 赛前看好、走地看衰 → 信走地（比赛现实 > 赛前模型）
- 盘口频繁震荡无方向 → 不下

## 输出格式

```
## {主队} vs {客队} ({联赛})

### 赛前
| 离散 | 实力差 | 亚盘初 | 判断 |
|------|--------|--------|------|
| H{x}🔥 | +{y} | {hc} | 主/客/无方向 |

### 走地
| 时间 | 比分 | 盘口 | 事件 |
|------|------|------|------|
| ... | ... | ... | ... |

### 盘口降速 vs 进球速度
进球{x}球 → 盘口降了{y}档 → {比预期快/慢/正常}

### 决策
**{上盘/下盘/不下}** — {理由}
```
