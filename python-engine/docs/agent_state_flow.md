# Agent State 流转文档

> 基于 `src/agent.py`，LangGraph StateGraph 实现。

---

## 一、AgentState 数据结构

```python
class AgentState(TypedDict, total=False):
    user: str              # 用户标识
    day_date: str          # 日期 (yyyy-mm-dd)
    live: bool             # 是否实盘模式
    capital: float         # 资金

    role_loaded: bool      # 角色是否已加载

    # analyze 阶段
    matches: list[dict]    # 原始比赛数据
    safe_matches: list[dict]  # 剥离比分后的安全数据
    prompt: dict           # 构建的 prompt（含 system + token_count）
    llm_response: str      # LLM 原始响应
    orders: list[dict]     # 解析出的投注指令
    placed_count: int      # 实际下单数量

    # settle 阶段
    unsettled_orders: list[dict]  # 未结算订单
    scores: dict[str, str]        # lota_id → 比分
    settlement: dict              # 结算汇总 {settled, hit, miss, push, pnl}

    # 输出
    status_msg: str
    error: str
```

## 二、AgentRuntime（跨节点共享，不序列化）

```python
@dataclass
class AgentRuntime:
    role: Optional[Role]           # 角色实例
    dm: DataManager                # 数据管理器
    builder: PromptBuilder         # prompt 构建器
    llm_call: Optional[Callable]   # LLM 调用函数
    session: Optional[SessionLogger]  # 会话记录器
    last_settled_orders: list[dict]   # 最近结算的订单
```

按 `user` 获取单例：`_rt(state)`。

---

## 三、Analyze 子图

```
load_role ──► fetch_matches ──► strip_scores ──► build_prompt ──► call_llm ──► parse_orders ──► place_orders ──► END
                 │                                    │
                 └── (无比赛 → END)                    └── (非 live / 无 LLM → 跳过)
```

### 节点明细

| 节点 | 函数 | 输入 State | 写入 State | 说明 |
|------|------|-----------|-----------|------|
| `load_role` | `node_load_role` | `user, capital` | `role_loaded: True` | 从磁盘加载或创建角色，注入 runtime |
| `fetch_matches` | `node_fetch_matches` | `day_date` | `matches: list[dict]` | 按足球日窗口获取比赛，过滤队名为空/`?` 的数据 |
| `strip_scores` | `node_strip_scores` | `matches` | `safe_matches: list[dict]` | 剥离比分信息，避免 LLM 看到结果 |
| `build_prompt` | `node_build_prompt` | `safe_matches`, `day_date` | `prompt: dict` | 构建 system prompt，注入累积记忆 + 昨日回顾 |
| `call_llm` | `node_call_llm` | `prompt`, `safe_matches` | `llm_response: str` | 调用 DeepSeek API，记录 thinking + content |
| `parse_orders` | `node_parse_orders` | `llm_response`, `safe_matches` | `orders: list[dict]` | 正则提取 ` ```order ``` ` 区块，补 lota_id + 赔率 |
| `place_orders` | `node_place_orders` | `orders` | `placed_count: int` | 逐条执行 `role.place_order()`，保存角色 |

### 条件边

| 源节点 | 条件 | 去向 |
|--------|------|------|
| `fetch_matches` | `matches` 为空 | `END` |
| `fetch_matches` | `matches` 非空 | `strip_scores` |
| `build_prompt` | `live=True` 且 LLM 已注册 | `call_llm` |
| `build_prompt` | 否则 | `parse_orders`（跳过 LLM） |

---

## 四、Settle 子图

```
load_role ──► load_unsettled ──► fetch_scores ──► settle_orders ──► reflect ──► END
```

### 节点明细

| 节点 | 函数 | 输入 State | 写入 State | 说明 |
|------|------|-----------|-----------|------|
| `load_role` | `node_load_role` | `user` | `role_loaded: True` | 加载角色 |
| `load_unsettled` | `node_load_unsettled` | — | `unsettled_orders: list[dict]` | 从 role 获取所有未结算订单 |
| `fetch_scores` | `node_fetch_scores` | `unsettled_orders` | `scores: dict[str, str]` | 按 lota_id 获取比分 |
| `settle_orders` | `node_settle_orders` | `unsettled_orders`, `scores` | `settlement: dict` | 逐条结算，统计 hit/miss/push/pnl |
| `reflect` | `node_reflect` | `day_date` | _(写 runtime.memory)_ | LLM 反思，提炼 alpha 因子，回写 FactorMemory |

---

## 五、完整流程（`Agent.run_day()`）

```
settle(day_date)  ──►  回填前一天 slug PnL  ──►  analyze(day_date, live)
```

先结算再分析，中间回填前一天各 slug 的盈亏表现到 `SlugMemory`。

---

## 六、State 字段流转全景

```
user ─┬─ load_role ─► role_loaded
      ├─ fetch_matches ─► matches ─► strip_scores ─► safe_matches
      ├─ build_prompt ─► prompt
      ├─ call_llm ─► llm_response ─► parse_orders ─► orders
      ├─ place_orders ─► placed_count
      │
      ├─ load_unsettled ─► unsettled_orders
      ├─ fetch_scores ─► scores
      ├─ settle_orders ─► settlement
      │
      ├─ status_msg
      └─ error
```

## 七、Agent 公开接口

```python
class Agent:
    def __init__(self, user: str = "default")
    def register_llm(self, api_key: str = None)   # 注册 DeepSeek API
    def init_role(self, capital: float = 10000)    # 初始化角色
    def analyze(self, day_date: str, live: bool = False) -> dict   # 分析一天
    def settle(self, day_date: str = None) -> dict                  # 结算
    def run_day(self, day_date: str, live: bool = False) -> dict    # 结算 + 分析
    def status(self) -> str                                         # 角色状态
```
