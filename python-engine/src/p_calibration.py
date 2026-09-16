"""方向路径校准：把「LLM 声称的概率 p̂」和「实际开奖结果」对齐，产出每因子的校准系数 k。

## 为什么

flex 模式下每条腿都要给出 p̂（从而得到 v̂ = p̂ × 赔率）。但 p̂ 是**模型自报**的，
没有约束它就会系统性高估（尤其在小样本、只见过的形态上）。北单的期望完全依赖
`Π v̂`，所以"声称的概率是否可信"必须被**用结果检验**，而不是靠市场封顶兜底。

## 口径

对每条已结算的腿，我们有若干 (声称概率 p̂_s, 结果 0/1) 对（每条被选方向一个），
且这条腿被归因到若干因子。因子 f 的校准系数：

    k_f = (Σ hit + a) / (Σ p̂ + a)          a = 先验强度（默认 2.0）

* 无样本 → `k_f = a/a = 1.000`（中性，不影响任何决策）；
* 系统性高估（Σhit < Σp̂）→ `k_f < 1` → 下一轮把该因子的声称概率**压低**；
* 系统性低估 → `k_f > 1`（放大仍受 `market_allowance` 封顶约束）。

一条腿的 k 取**各归因因子里最小的那个**（最保守；同一预测由多个因子支持时，
只要有一个因子历史上不可信，就不该放大仓位）。不带因子的腿 k=1。

按路径分开统计：`direction`（单选腿，1 个方向）与 `cover`（多选腿）——两者的
校准特性不同，不能混（单选看方向准不准，多选看概率均值准不准）。

## 存储

`<角色目录>/memory/p_calibration.json`：

```json
{
  "updated_at": "2026-09-11T12:30:00",
  "legs": {"total": 12, "direction": 5, "cover": 7, "no_factor": 1},
  "factors": {
    "离散凝聚顺向": {
      "direction": {"n": 4, "sum_p": 2.31, "sum_hit": 1.0,
                    "buckets": {"0.4-0.6": {"n": 3, "sum_p": 1.5, "sum_hit": 1.0}}}
    }
  }
}
```
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

# 先验强度：没有数据时 k=1；样本少时向 1 收缩
PRIOR_A = 2.0
# 分桶（声称概率）——用于看校准曲线，不参与 k 的计算
BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001))
MODES = ("direction", "cover")


def _bucket_of(p: float) -> str:
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return f"{lo:.1f}-{hi:.1f}"
    return f"{BUCKETS[-1][0]:.1f}-{BUCKETS[-1][1]:.1f}"


def mode_of_leg(picks: Iterable[str]) -> str:
    return "direction" if len(list(picks or [])) <= 1 else "cover"


class PCalibration:
    """每角色一份：因子 → 声称概率校准。"""

    def __init__(self, memory_dir: Path | str, prior: float = PRIOR_A):
        self.dir = Path(memory_dir)
        self.path = self.dir / "p_calibration.json"
        self.prior = float(prior)
        self.data: dict = {"legs": {}, "factors": {}}
        self._loaded = False

    # ── 读写 ──────────────────────────────────

    def load(self) -> "PCalibration":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = {"legs": raw.get("legs") or {},
                             "factors": raw.get("factors") or {}}
        except Exception:
            pass
        self._loaded = True
        return self

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ── 写入 ──────────────────────────────────

    def record_leg(self, factors: list[str], mode: str, claims: dict,
                   actual: Optional[str]) -> int:
        """记一条已结算的腿。claims: {side: p̂}；actual: 实际方向（None=走水/取消）。

        返回写入的 (p̂, hit) 对数量。
        """
        if mode not in MODES:
            mode = mode_of_leg(claims)
        if not claims:
            return 0
        n = 0
        for f in (factors or []) or [""]:
            for side, p in claims.items():
                try:
                    p = float(p)
                except (TypeError, ValueError):
                    continue
                if not (0.0 < p < 1.0):
                    continue
                hit = 1.0 if (actual and side == actual) else 0.0
                if actual is None:
                    # 走水/取消：不携带信息，跳过
                    continue
                key = f or "__no_factor__"
                entry = self.data["factors"].setdefault(key, {})
                st = entry.setdefault(mode, {"n": 0, "sum_p": 0.0, "sum_hit": 0.0,
                                             "buckets": {}})
                st["n"] += 1
                st["sum_p"] += p
                st["sum_hit"] += hit
                b = st["buckets"].setdefault(_bucket_of(p), {"n": 0, "sum_p": 0.0,
                                                              "sum_hit": 0.0})
                b["n"] += 1
                b["sum_p"] += p
                b["sum_hit"] += hit
                n += 1
        legs = self.data.setdefault("legs", {})
        legs["total"] = legs.get("total", 0) + 1
        legs[mode] = legs.get(mode, 0) + 1
        if not factors:
            legs["no_factor"] = legs.get("no_factor", 0) + 1
        return n

    # ── 读取 ──────────────────────────────────

    def k(self, factor: str, mode: Optional[str] = None) -> float:
        """因子校准系数；无样本返回 1.0（向先验收缩到中性）。"""
        entry = (self.data.get("factors") or {}).get(factor) or {}
        modes = [mode] if mode in MODES else list(MODES)
        n = sum_p = sum_hit = 0.0
        for m in modes:
            st = entry.get(m)
            if not st:
                continue
            n += float(st.get("n") or 0)
            sum_p += float(st.get("sum_p") or 0.0)
            sum_hit += float(st.get("sum_hit") or 0.0)
        a = self.prior
        if n <= 0:
            return 1.0
        return (sum_hit + a) / (sum_p + a)

    def k_for_leg(self, factors: list[str], mode: str) -> tuple[float, dict]:
        """一条腿的校准系数 = 各归因因子 k 的最小值；无因子/无样本 → 1.0。

        返回 (k, 明细)。
        """
        detail = {f: round(self.k(f, mode), 4) for f in (factors or [])}
        if not detail:
            return 1.0, {}
        return min(detail.values()), detail

    def summary(self) -> dict:
        out = {"legs": dict(self.data.get("legs") or {}), "factors": {}}
        for f, entry in (self.data.get("factors") or {}).items():
            out["factors"][f] = {}
            for m in MODES:
                st = entry.get(m)
                if not st or not st.get("n"):
                    continue
                n = float(st["n"])
                sum_p = float(st.get("sum_p") or 0.0)
                sum_hit = float(st.get("sum_hit") or 0.0)
                out["factors"][f][m] = {
                    "n": int(n),
                    "claimed": round(sum_p, 3),
                    "realized": round(sum_hit, 3),
                    "k": round(self.k(f, m), 3),
                }
        return out


def calibration_for(role) -> PCalibration:
    """按角色目录定位校准文件（沙箱回放时自动落在沙箱里）。"""
    try:
        base = Path(role._role_dir) / "memory"
    except Exception:
        base = Path(".")
    return PCalibration(base).load()
