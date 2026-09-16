"""两轴因子的机器可读条件求值器（引擎侧，无 LLM、无副作用）。

`cond` 语法（唯一来源：`docs/prompts/factor_produce_two_axis.md`）：

```
cond   := or_expr
or_expr:= and_expr (" 或 " and_expr)*
and_expr:= term (" 且 " term)*
term   := field op value          op ∈ == != < <= > >=
value  := 数字 | H|D|A | 另一个字段名
```

字段：`self/side_is/rank_mp/rank_od/rank_disp`、`mp/od/ah.water/ah.line/ah.dline/ah.dh/ah.da/
eu.move/disp/disp.ds`、`span/gl/x/gap/gs/ah.h0|h1|a0|a1|line0|line1/eu.h|d|a/disp.h|d|a/
disp.ds.h|d|a`。

用途：**每天在当天的腿上求值**，得到"哪些腿被该因子标记"，供
① 方向轴统计（命中率 − 市场 p̂）② 波动轴统计（兑现 vs 当日基线）③ prompt 渲染。
"""
from __future__ import annotations

import re
from collections import defaultdict

_TERM = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(==|!=|<=|>=|<|>)\s*(-?[\w.]+)\s*$")
_OPS = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


def _side_idx(side: str) -> int:
    return {"H": 0, "D": 1, "A": 2}.get(side, 1)


def build_eval_env(legs: list) -> dict:
    """预计算每场的三侧名次（rank_mp / rank_od / rank_disp）。"""
    by_match: dict = defaultdict(dict)
    for l in legs:
        by_match[(l.get("day"), l.get("lota_id"))][l.get("side")] = l
    env: dict = {}
    for key, peers in by_match.items():
        mps = sorted(peers.items(), key=lambda kv: kv[1].get("market_p") or 0)
        ods = sorted(peers.items(), key=lambda kv: kv[1].get("beidan_odds") or 0)
        dis = sorted(peers.items(),
                     key=lambda kv: ((kv[1].get("feats") or {}).get("disp_last") or [0, 0, 0])[
                         _side_idx(kv[0])])
        env[key] = {"mp": {s: i + 1 for i, (s, _) in enumerate(mps)},
                    "od": {s: i + 1 for i, (s, _) in enumerate(ods)},
                    "disp": {s: i + 1 for i, (s, _) in enumerate(dis)}}
    return env


def field_value(name: str, leg: dict, env: dict):
    """取一个字段在**这一条腿**上的值（缺失返回 None）。"""
    f = leg.get("feats") or {}
    ah = f.get("ah_crown") or {}
    side = leg.get("side")
    i = _side_idx(side)
    rk = env.get((leg.get("day"), leg.get("lota_id")), {})
    water = ah.get("h1") if side == "H" else (ah.get("a1") if side == "A" else None)
    eu1, disp, ds = f.get("eu1"), f.get("disp_last"), f.get("disp_first")

    def _at(seq, k):
        try:
            return seq[k]
        except (TypeError, IndexError, KeyError):
            return None

    def _d(a, b):
        return None if (a is None or b is None) else a - b

    table = {
        "self": side, "side_is": side, "side": side,   # side/self/side_is 三种写法等价
        "rank_mp": (rk.get("mp") or {}).get(side),
        "rank_od": (rk.get("od") or {}).get(side),
        "rank_disp": (rk.get("disp") or {}).get(side),
        "mp": leg.get("market_p"), "od": leg.get("beidan_odds"),
        "span": leg.get("span"), "x": leg.get("x"),
        "gl": float(leg.get("goal_line") or 0),
        "ah.water": water, "ah.line": ah.get("line1"), "ah.dline": ah.get("dline"),
        "ah.dh": _d(ah.get("h1"), ah.get("h0")), "ah.da": _d(ah.get("a1"), ah.get("a0")),
        "ah.h0": ah.get("h0"), "ah.h1": ah.get("h1"),
        "ah.a0": ah.get("a0"), "ah.a1": ah.get("a1"),
        "ah.line0": ah.get("line0"), "ah.line1": ah.get("line1"),
        "eu.move": (f.get("eu_move") or {}).get(side),
        "eu.h": _at(eu1, 0), "eu.d": _at(eu1, 1), "eu.a": _at(eu1, 2),
        "disp": _at(disp, i), "disp.ds": _at(ds, i),
        "disp.h": _at(disp, 0), "disp.d": _at(disp, 1), "disp.a": _at(disp, 2),
        "disp.ds.h": _at(ds, 0), "disp.ds.d": _at(ds, 1), "disp.ds.a": _at(ds, 2),
        "gap": f.get("strength_gap"), "gs": f.get("goals_sum"),
    }
    return table.get(name)


def eval_term(term: str, leg: dict, env: dict) -> bool:
    m = _TERM.match(term)
    if not m:
        raise ValueError(f"无法解析: {term!r}")
    field, op, raw = m.groups()
    val = field_value(field, leg, env)
    if field in ("self", "side_is", "side"):
        return _OPS[op](str(val or ""), raw)
    other = field_value(raw, leg, env)          # value 允许是另一个字段
    if other is not None:
        try:
            return _OPS[op](float(val), float(other))
        except (TypeError, ValueError):
            return False
    try:
        return _OPS[op](float(val), float(raw))
    except (TypeError, ValueError):
        return False                            # 字段缺失 ⇒ 条件不成立


def eval_cond(cond: str, leg: dict, env: dict) -> bool:
    """求值一条 cond；解析失败抛 ValueError（由调用方决定怎么处理）。"""
    cond = (cond or "").strip()
    if not cond:
        return False
    for or_part in re.split(r"\s*或\s*", cond):
        if all(eval_term(t, leg, env) for t in re.split(r"\s*且\s*", or_part)):
            return True
    return False
