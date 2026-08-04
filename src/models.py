"""
DSFootball Python CLI — 数据模型定义

轻量化设计：所有模型基于 dataclass + JSON 序列化，
持久化到 JSON 文件，无 ORM 依赖。
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Literal
from datetime import datetime
from enum import Enum


# ═══════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════

class PredictType(str, Enum):
    """预测类型 — 决定回测时的评估算法"""
    ASIAN = "亚盘"      # 亚盘: handicap + direction
    OU = "大小球"        # 大小球: threshold + over/under
    SFP = "胜平负"       # 胜平负: H/D/A
    SCORE = "比分"       # 比分: scores[]
    GOALS = "进球数"     # 进球数: goals

    def __str__(self):
        return self.value


class MatchState(int, Enum):
    """比赛状态"""
    NOT_STARTED = 0
    LIVE = 1
    FINISHED = 6


# ═══════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════

def _now() -> str:
    return datetime.now().isoformat()


def _uid(prefix: str = "") -> str:
    """生成短唯一 ID"""
    import secrets
    ts = str(int(datetime.now().timestamp() * 1000))
    rnd = secrets.token_hex(3)
    return f"{prefix}{ts}_{rnd}" if prefix else f"{ts}_{rnd}"


# ═══════════════════════════════════════════════
# 1. Match — 比赛
# ═══════════════════════════════════════════════

@dataclass
class Match:
    """比赛基本信息，来源 Lota API"""
    lota_id: str
    home_name: str
    away_name: str
    league_name: str
    match_time: str                              # "2026-06-11 19:30:00"
    state: int = 0                                # 0=未开赛, 1=进行中, 6=完场
    score: Optional[str] = None                   # "0:1"
    state_name: str = ""
    week: str = ""
    venues_name: str = ""
    match_type: str = "N/A"
    jingcai_number: Optional[str] = None
    beidan_number: Optional[str] = None
    # 赔率预览（可选，不持久化到 factors 缓存）
    odds_preview: Optional[dict] = None

    @property
    def is_finished(self) -> bool:
        return self.state == 6

    @property
    def goal_diff(self) -> int:
        if not self.score: return 0
        h, a = self.score.split(":")
        return int(h) - int(a)

    @property
    def total_goals(self) -> int:
        if not self.score: return 0
        h, a = self.score.split(":")
        return int(h) + int(a)


# ═══════════════════════════════════════════════
# 2. Prediction — 预测
# ═══════════════════════════════════════════════

@dataclass
class Prediction:
    """单条预测记录"""
    id: str = field(default_factory=lambda: _uid("pred_"))
    lota_id: str = ""
    desc: str = ""                                # 摘要/人类可读的预测文本
    value: dict = field(default_factory=dict)     # 结构化预测值 (schema)
    #  亚盘: {handicap: float, direction: "H"|"A"}
    #  大小球: {threshold: float, direction: "over"|"under"}
    #  胜平负: {result: "H"|"D"|"A"}
    #  比分: {scores: [{home:int, away:int}, ...]}
    #  进球数: {goals: int}
    type: PredictType = PredictType.ASIAN
    thought: str = ""                             # 思考路径
    odds: dict = field(default_factory=dict)      # 赔率快照（Pinnacle 终盘），比分/进球数为空
    #  胜平负: {h: float, d: float, a: float}
    #  亚盘:   {h: float, handicap: float, handicap_text: str, a: float}
    #  大小球: {over: float, threshold: float, threshold_text: str, under: float}
    # 回测结果（异步填充）
    hit: Optional[bool] = None
    result: Optional[str] = None                  # 实际比分 "2:1"
    checked_at: Optional[str] = None
    # 溯源
    batch_id: str = ""
    created_at: str = field(default_factory=_now)

    @property
    def is_checked(self) -> bool:
        return self.hit is not None


# ═══════════════════════════════════════════════
# 3. Factor — 决策因子（独立、可复用）
# ═══════════════════════════════════════════════

@dataclass
class Factor:
    """
    决策因子 — 独立的分析维度。

    slugs 指向 compact-fet 的哪些数据段（section），
    content 描述「如何计算」和「如何评估」。

    举例:
      Factor(slugs=["fair-odds", "eu-odds-pinnacle"],
             content="比较公平盘实力差与 Pinnacle 欧盘概率偏差，偏差>0.1 表示市场误判")
      Factor(slugs=["asian-handicap-crown"],
             content="观察皇冠亚盘水位变化，水位下降>0.05 且盘口未变 → 机构看好该方向")
      Factor(slugs=["betfair-buysell"],
             content="必发买卖盘净量，净买入>50万表示资金倾向")

    因子本身只存储评估逻辑，不含具体比赛的数值 —
    比赛数据通过 tools.get_sections_by_slugs(lota_id, factor.slugs) 按需获取。
    """
    id: str = field(default_factory=lambda: _uid("fac_"))
    slugs: list[str] = field(default_factory=list)  # 依赖的数据段 slug（如 ["fair-odds", "asian-handicap-crown"]）
    content: str = ""                                # 计算方法 + 评估数据描述
    #  格式: "## 计算方法\n...\n## 评估标准\n..."
    updated_at: str = field(default_factory=_now)


# ═══════════════════════════════════════════════
# 5. BacktestResult — 回测结果
# ═══════════════════════════════════════════════

@dataclass
class BacktestResult:
    """一次回测运行的汇总结果"""
    id: str = field(default_factory=lambda: _uid("bt_"))
    executed_at: str = field(default_factory=_now)
    match_ids: list[str] = field(default_factory=list)  # 回测的比赛 lota_id 列表
    total: int = 0                                # 总预测数
    hits: int = 0                                 # 命中数
    acc: float = 0.0                              # 准确率 hits/total
    roi: float = 0.0                              # 收益率（简算: 命中赔率累加/总投入）
    desc: str = ""                                # 备注

    def compute(self):
        """根据 hits/total 重算 acc"""
        if self.total > 0:
            self.acc = round(self.hits / self.total, 4)


# ═══════════════════════════════════════════════
# 6. Order — 虚拟投注订单
# ═══════════════════════════════════════════════

@dataclass
class Order:
    """
    投注订单 — 基于预测创建的虚拟投注。

    可投注类型（有赔率）:
      - 胜平负: bet_type="胜平负", pick="H"|"D"|"A", odds=对应赔率
      - 亚盘:   bet_type="亚盘", pick="H"|"A", odds=对应赔率, handicap=让球
      - 大小球: bet_type="大小球", pick="over"|"under", odds=对应赔率, threshold=盘口
      - 让球胜平负: bet_type="让球胜平负", pick="H"|"D"|"A", odds=对应赔率,
                   goal_line=竞彩让球线（负=主让，正=主受），用于竞彩串关

    比分和进球数不关联订单。
    """
    id: str = field(default_factory=lambda: _uid("ord_"))
    predict_id: str = ""                           # 关联 Prediction.id
    lota_id: str = ""                              # 比赛 ID（冗余查询）
    bet_type: str = ""                             # 胜平负 | 亚盘 | 大小球 | 让球胜平负
    pick: str = ""                                 # H/D/A | H/A | over/under
    odds: float = 0.0                              # 投注赔率（终盘 Pinnacle）
    handicap: Optional[float] = None               # 亚盘让球 / 大小球盘口
    goal_line: Optional[float] = None              # 竞彩让球线（让球胜平负）
    bet_size: float = 100.0                        # 投注金额
    # 结算
    hit: Optional[bool] = None                     # 命中/未中/走水(None)
    return_amount: float = 0.0                     # 返还金额
    profit: float = 0.0                            # 盈亏
    settled_at: Optional[str] = None
    created_at: str = field(default_factory=_now)


# ═══════════════════════════════════════════════
# 7. SystemPrompt — 可固化的 Agent 策略配置
# ═══════════════════════════════════════════════

@dataclass
class SystemPrompt:
    """
    Agent 策略配置 — 固化角色设定、记忆组合方式、决策框架。

    持久化到 agent_prompts/{name}.json，可版本迭代、clone、freeze。
    这决定了 agent "怎么想"和"怎么做"，不包含具体比赛数据。
    """
    id: str = field(default_factory=lambda: _uid("sp_"))
    name: str = ""                                 # 策略名，如 "conservative-v1"
    version: int = 1

    # ── 角色 ──
    role: str = ""                                 # 角色描述

    # ── 记忆组合方式 ──
    memory_config: dict = field(default_factory=lambda: {
        "max_recent_orders": 20,
        "include_summary": True,
        "include_streaks": True,
        "include_loss_patterns": False,
        "include_factor_perf": False,
        "include_settlement_review": True,
        "include_slug_perf": False,
        "include_reflections": True,
        "include_cross_agent_factors": False,  # alpha模式: 读取其他Agent的因子
    })

    # ── 数据段 ──
    default_slugs: list[str] = field(default_factory=lambda: [
        "match-head", "fair-odds", "eu-odds-pinnacle",
        "asian-handicap-pinnacle", "over-under-crown",
        "betfair-buysell", "discrete-odds",
    ])

    # ── 决策框架 ──
    framework: str = ""                            # 下注决策逻辑
    bet_sizing: str = ""                           # 资金管理规则

    # ── 输出格式 ──
    output_format: str = ""                        # 期望 LLM 输出格式

    # ── 元信息 ──
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    notes: str = ""                                # 设计思路 / 迭代原因


# ═══════════════════════════════════════════════
# JSON 序列化工具
# ═══════════════════════════════════════════════

def model_to_dict(obj) -> dict:
    """dataclass → dict，处理 Enum 和嵌套"""
    d = {}
    for k, v in asdict(obj).items():
        if isinstance(v, Enum):
            d[k] = v.value
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


def dict_to_model(data: dict, cls):
    """dict → dataclass，处理 Enum 字段"""
    import dataclasses
    field_types = {f.name: f.type for f in dataclasses.fields(cls)}
    kwargs = {}
    for k, v in data.items():
        if k not in field_types:
            continue
        ft = field_types[k]
        # 处理 Optional[PredictType]
        origin = getattr(ft, "__origin__", None)
        if origin is Literal:
            kwargs[k] = v
        elif isinstance(v, str) and ft is PredictType:
            kwargs[k] = PredictType(v)
        elif isinstance(v, str) and ft is Optional[PredictType]:
            kwargs[k] = PredictType(v)
        else:
            kwargs[k] = v
    return cls(**kwargs)


# ═══════════════════════════════════════════════
# 类型注册表（用于反序列化路由）
# ═══════════════════════════════════════════════

MODEL_REGISTRY: dict[str, type] = {
    "match": Match,
    "prediction": Prediction,
    "factor": Factor,
    "backtest_result": BacktestResult,
    "order": Order,
    "system_prompt": SystemPrompt,
}
