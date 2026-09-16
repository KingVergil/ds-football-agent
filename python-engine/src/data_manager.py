"""
DSFootball Python CLI — 统一数据访问层

封装 match / compact-fet / tags 的缓存和 API 访问，
提供按 lota_id 关联查询预测、订单、赔率的便捷方法。

规则:
  - 已完场比赛: 优先本地缓存, 没有再走 API
  - 未开赛/进行中: 先查缓存, 可强制刷新
"""

import json
import os
import re
import time
import contextlib
from collections import defaultdict
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone

import requests

try:
    import fcntl  # POSIX 跨进程文件锁（macOS/Linux）
except ImportError:  # 非 POSIX 环境退化为无锁（仅提示一次）
    fcntl = None

from .tools import (
    _SECTION_RULES,
    _parse_handicap_text,
    extract_odds,
    compact_fet_to_tags as _compact_fet_to_tags,
)
from .beidan_settlement import handicap_result as _beidan_handicap_result, result_code_to_pick as _beidan_result_code_to_pick


# ═══════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════

# ⚠️ 数据接口地址**不写死**：公开面默认走域名，私有/自建端点用环境变量覆盖。
#   export LOTA_API_BASE=http://<your-host>:<port>/predictions/api/v2
# 内网 IP、端口、自建服务地址一律不进代码（2026-09-16）。
BASE_URL = os.environ.get("LOTA_API_BASE", "http://deepdata.lota.tv/predictions/api/v2")

# ── 离线开关 ──
# 置 True 后 _get() 直接短路（不联网），所有 fetch_*/refresh_*/prepare_* 自动
# 回退到本地缓存。用于 walkforward 回放等「只用本地已拉好的缓存」场景，
# 避免回放把共享 data/ 缓存覆盖 / 污染。
_OFFLINE = False


def set_offline(flag: bool = True) -> None:
    """全局开关：True = 禁止一切对外 HTTP（只读本地缓存）。"""
    global _OFFLINE
    _OFFLINE = bool(flag)


def is_offline() -> bool:
    return _OFFLINE


# ── 回测取数开关（fet_txt 时间切片）──
# 沙箱回放（DS_ROLES_ROOT 存在）时自动启用，见 src/backtest_fet.py。
# 启用后：在切片索引范围内的场次，compact-fet / tags 一律走本地 fet_txt 切片
# （按「访问时刻 → 开赛前的哪一个快照档」解析），不再读线上/本地实时缓存——
# 线上 `live/` 是赛前终盘快照，回放读它就是前视泄漏。
def _backtest_fet():
    """返回启用的切片源；未启用返回 None（线上恒为 None）。"""
    try:
        from . import backtest_fet as _bf
    except Exception:
        return None
    try:
        if not _bf.active():
            return None
        return _bf.current()
    except Exception:
        return None


# 数据根目录
PROJECT_ROOT = Path(__file__).parent.parent
DATA_ROOT = PROJECT_ROOT / "data"

MATCHES_DIR = DATA_ROOT / "matches"
FEATURES_DIR = DATA_ROOT / "features"
TAGS_DIR = Path(__file__).parent.parent / "data" / "tags"
PREDICTS_DIR = Path(__file__).parent.parent / "data" / "predicts"
ORDERS_DIR = Path(__file__).parent.parent / "data" / "orders"
BEIDAN_DIR = DATA_ROOT / "beidan"
BEIDAN_SP_DIR = DATA_ROOT / "beidan_sp"

# 确保目录存在
for d in [MATCHES_DIR, FEATURES_DIR, TAGS_DIR, PREDICTS_DIR, ORDERS_DIR,
          BEIDAN_DIR, BEIDAN_SP_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════

class LotaAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.is_fatal = status_code == 403


# ═══════════════════════════════════════════════
# API 客户端
# ═══════════════════════════════════════════════

def _get_api_key() -> str:
    settings_dir = Path.home() / ".claude" / "settings.json"
    if settings_dir.exists():
        try:
            cfg = json.loads(settings_dir.read_text())
            return cfg.get("lota", {}).get("api_key", "") or cfg.get("lotaKey", "")
        except Exception:
            pass
    return os.environ.get("LOTA_API_KEY", "")


def _headers() -> dict:
    return {"X-API-Key": _get_api_key(), "Content-Type": "application/json"}


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写入：先写临时文件再替换，避免并发进程读到半截文件。"""
    # 临时文件名带进程号，避免多进程并发写同一文件时互相踩（固定 .tmp 会被
    # 先完成的进程 os.replace 挪走，后到的进程 replace 找不到源而报错）
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ═══════════════════════════════════════════════
# 跨进程调度（多狗并发取数）
# ═══════════════════════════════════════════════

_LOCKS_DIR = DATA_ROOT / ".dm_locks"
_STATE_PATH = DATA_ROOT / ".dm_state.json"

# 竞彩编号里的周X ↔ 足球日：竞彩按"销售日"编号，足球日窗口 [D 12:01, D+1 12:00]
# 的起始日 D 就是销售日，所以 D 的星期必须等于编号里的周X。
_ZH_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def football_day_weekday(day_str: str) -> str:
    """足球日起始日 → 竞彩编号应该用的周X（如 2026-09-10 → 周四）。"""
    try:
        return _ZH_WEEKDAYS[datetime.strptime(day_str[:10], "%Y-%m-%d").weekday()]
    except (ValueError, TypeError):
        return ""


def beidan_day_window(day_str: str) -> Optional[tuple[str, str]]:
    """足球日 D 的北单窗口 = [D 12:01:00, D+1 12:00:00]。

    ⚠️ 上游 /beidan/sp?date=D 的窗口口径依赖「服务端当前时间」：
      12:00 之后 → [D 12:01, D+1 12:00]；12:00 之前 → [D-1 12:01, D 12:00]。
    所以中午前跑结算时，同一个 D 会拿到前一足球日的数据（实测 2026-09-16 10:55
    用 date=2026-09-15 拿到的是足球日 09-14 的 40 场）。
    结算一律改用显式时间窗，绕开这个漂移。
    """
    try:
        start = datetime.strptime(str(day_str)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    start = start.replace(hour=12, minute=1, second=0, microsecond=0)
    return (start.strftime("%Y-%m-%d %H:%M:%S"),
            (start + timedelta(days=1)).replace(hour=12, minute=0).strftime("%Y-%m-%d %H:%M:%S"))


def jc_number_weekday(number: str) -> str:
    """竞彩编号 → 周X（如 周五002 → 周五）；无编号返回空串。"""
    m = re.match(r"\s*(周[一二三四五六日天])", str(number or ""))
    if not m:
        return ""
    return "周日" if m.group(1) == "周天" else m.group(1)


def _sanitize_lock_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", name)


@contextlib.contextmanager
def _file_lock(name: str, timeout: float = 300.0):
    """跨进程互斥锁（fcntl.flock，按资源名）。

    多只狗是独立进程，进程内单例管不到并发 → 用锁文件做单飞：
    同一资源（日历日 / lota_id）同时只有一个进程去线上拉，其余进程
    等锁后直接读本地缓存。timeout 内拿不到锁则退化为阻塞等待，
    避免因上游慢导致并发进程直接放弃协调。
    """
    _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(_LOCKS_DIR / f"{_sanitize_lock_name(name)}.lock", "w")
    try:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                        break
                    time.sleep(0.2)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()


def _load_dm_state() -> dict:
    if _STATE_PATH.exists():
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_dm_state(state: dict) -> None:
    _atomic_write_text(_STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))


def _record_dm_state(section: str, key: str, entry: dict) -> None:
    """更新共享状态文件（加状态锁，避免多资源并发写互相覆盖）。"""
    with _file_lock("dm-state", timeout=30):
        st = _load_dm_state()
        st.setdefault(section, {})[key] = entry
        _save_dm_state(st)

# 最近一次 API 调用的失败信息（status: int|None；None 表示网络/连接异常）。
# 成功调用会重置为 None。用于让「回填北单缓存」区分「上游 502/断网」与「确实无数据」，
# 避免把宕机前的好缓存覆盖成空/半截结果。
_last_api_error: Optional[dict] = None

# 瞬态状态码：502/503/504（网关/负载均衡抖动）与 429（限流）值得重试。
_TRANSIENT_STATUS = {429, 502, 503, 504}


def _get(path: str, params: dict = None, retries: int = 3) -> dict | list | None:
    """GET 请求。瞬态 502/503/504/429 与连接错误会指数退避重试。

    各调用方语义不变：失败仍返回 None（依赖方据此回退本地缓存）；
    403 视为 fatal（立刻抛 LotaAPIError）。重试耗尽后把失败写入 _last_api_error。
    """
    global _last_api_error
    if _OFFLINE:
        # 离线：不做任何网络请求，直接当作拉取失败，让依赖方回退本地缓存。
        _last_api_error = {"status": None, "msg": "[offline] 已禁用网络请求"}
        return None
    url = f"{BASE_URL}{path}"
    _last_api_error = None
    last_err: Optional[dict] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
            if resp.status_code == 403:
                raise LotaAPIError(403, "Lota API key 过期或超限")
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in _TRANSIENT_STATUS and attempt < retries - 1:
                last_err = {
                    "status": resp.status_code,
                    "msg": f"{url} → {resp.status_code}",
                }
                time.sleep(min(2 ** attempt, 8))  # 1s / 2s / 4s
                continue
            msg = f"{url} → {resp.status_code}"
            print(f"[lota] {msg}")
            _last_api_error = {"status": resp.status_code, "msg": msg}
            return None
        except requests.RequestException as e:
            last_err = {"status": None, "msg": f"{url} → {e}"}
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
                continue
            print(f"[lota] {last_err['msg']}")
            _last_api_error = last_err
            return None
    # 理论到不了这里；防御：把最后一次瞬态错误也记录下来
    _last_api_error = last_err
    return None


# ═══════════════════════════════════════════════
# DataManager
# ═══════════════════════════════════════════════

class DataManager:
    """统一数据访问层 — 单例"""

    _instance = None
    _live_strict = False  # live 模式标记：禁止用过期缓存兜底（见 get_compact_fet）

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ═══════════════════════════════════════════
    # Data Freshness Check
    # ═══════════════════════════════════════════

    def set_live_mode(self, live: bool) -> None:
        """设置 live 模式。live 模式下 get_compact_fet 刷新失败时拒绝回退旧缓存。"""
        type(self)._live_strict = bool(live)

    def check_data_freshness(self, day_date: str = None) -> bool:
        """
        检查数据管道是否健康。通过对比本地缓存中最新的特征数据时间戳
        与当前时间的差距，如果超过 3 小时则发出警告。

        同时检查当天比赛：如果已开赛的比赛中存在 state=-1
        （数据未更新），则极可能数据管道中断（如 Celery 崩溃）。

        Returns: True if fresh, False if stale.
        """
        now = datetime.now()
        warn_msgs = []

        # 1. 检查特征文件缓存的新鲜度
        newest_cached = None
        newest_lota_id = ""
        feature_files = sorted(FEATURES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        checked = 0
        for fp in feature_files:
            if checked >= 50:  # 采样最近 50 个文件
                break
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("_api_failed"):
                continue
            cached_at = data.get("_cached_at", "")
            if cached_at:
                try:
                    ct = datetime.fromisoformat(cached_at)
                    if ct.tzinfo is not None:
                        # 缓存时间戳可能带时区（如 +08:00 / Z），统一转本地 naive 再比较
                        ct = ct.astimezone().replace(tzinfo=None)
                    if newest_cached is None or ct > newest_cached:
                        newest_cached = ct
                        newest_lota_id = fp.stem
                except Exception:
                    pass
            checked += 1

        if newest_cached:
            gap_h = (now - newest_cached).total_seconds() / 3600
            if gap_h > 3:
                warn_msgs.append(
                    f"⚠️ 最新特征缓存({newest_lota_id}): {newest_cached.strftime('%m-%d %H:%M')}，"
                    f"距今 {gap_h:.1f}h（阈值 3h）"
                )

        # 2. 检查已开赛比赛：state=-1 且 match_time 已过 → 数据管道可能中断
        # 未来比赛 state=-1 属于正常（尚未入库），不报警
        if day_date:
            from datetime import date as _date, timedelta
            now_str = now.strftime("%Y-%m-%d %H:%M")
            cutoff_str = (now + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
            for d in [day_date,
                      (_date.fromisoformat(day_date) + timedelta(days=1)).isoformat()]:
                try:
                    matches = self.get_cached_matches(d, lottery_type="all")
                except Exception:
                    continue
                stale_matches = []
                for m in matches:
                    mt = m.get("match_time", "")
                    state = m.get("state", 0)
                    # 只关注已开赛但 state=-1 的竞彩比赛（数据管道异常）
                    if mt and state == -1 and mt <= now_str and m.get("jingcai_number"):
                        # 精简格式：ID 主vs客
                        stale_matches.append(
                            f"{m.get('lota_id','?')} "
                            f"{m.get('home_name','?')}vs{m.get('away_name','?')}"
                        )
                if stale_matches:
                    warn_msgs.append(
                        f"⚠️ {d}: {len(stale_matches)} 场 state=-1:"
                    )
                    for sm in stale_matches:
                        warn_msgs.append(f"  · {sm}")

        if warn_msgs:
            print("")
            print("┌──────────────────────────────────────────────┐")
            print("│ 🔴 DATA FRESHNESS WARNING                     │")
            print("├──────────────────────────────────────────────┤")
            for msg in warn_msgs:
                print(f"│ {msg}")
            print("│                                              │")
            print("│ 可能原因: Celery/MySQL 崩溃或 fet-text 未刷新 │")
            print("│ 建议: 检查服务器状态后重新运行                │")
            print("└──────────────────────────────────────────────┘")
            print("")
            return False
        return True

    # ═══════════════════════════════════════════
    # Blacklist
    # ═══════════════════════════════════════════

    def get_blacklist(self) -> set[str]:
        """读取黑名单 lota_id（已完赛/异常比赛，禁止下注）"""
        path = DATA_ROOT / "blacklist.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    # ═══════════════════════════════════════════
    # Match
    # ═══════════════════════════════════════════

    def fetch_matches_by_date(self, date_str: str, lottery_type: str = "jingcai",
                              is_jingcai: bool = False, is_beidan: bool = False) -> list[dict]:
        """API 查询某日比赛列表。

        is_jingcai=True 时附带竞彩让球胜平负数据（jc_hhad，含 goal_line/赔率），
        默认 False 不多打 spdex 库。
        is_beidan=True 时附带北单让球胜平负/开奖数据（beidan_info，含 goal_line/赔率/result/sp）。
        """
        params = {"date": date_str}
        if lottery_type and lottery_type != "all":
            params["type"] = lottery_type
        if is_jingcai:
            params["is_jingcai"] = "true"
        if is_beidan:
            params["is_beidan"] = "true"
        data = _get("/matches", params)
        if not data:
            return []
        result = data.get("data") or {}
        if isinstance(result, dict):
            matches = result.get("matches") or result.get("match") or []
        else:
            matches = result if isinstance(result, list) else []
        return matches if isinstance(matches, list) else []

    def fetch_beidan_matches_by_date(self, date_str: str) -> list[dict]:
        """从 v2-api 拉取单个日历日的北单比赛（带 beidan_info: goal_line+开奖sp）."""
        return self.fetch_matches_by_date(date_str, lottery_type="all", is_beidan=True)

    def fetch_beidan_sp(self, date_str: str) -> dict[str, dict]:
        """从 v2-api 拉取某足球日的北单开奖 SP（data.results: {lota_id: {result, spvalue, score, ...}}）。

        date_str 是足球日 D；请求用显式窗口 [D 12:01, D+1 12:00]，
        避免上游 date= 口径在中午前后漂移（见 beidan_day_window）。
        """
        window = beidan_day_window(date_str)
        params = ({"start_date": window[0], "end_date": window[1]} if window
                  else {"date": date_str})
        data = _get("/beidan/sp", params)
        if not data:
            return {}
        result = data.get("data") or {}
        results = result.get("results") if isinstance(result, dict) else {}
        return results if isinstance(results, dict) else {}

    def _fetch_all_matches(self, params: dict) -> list[dict]:
        """分页拉取 matches 全量结果（服务器 limit 默认 500，超出会被截断）。"""
        out: list[dict] = []
        offset = 0
        limit = 2000
        while True:
            p = {**params, "limit": limit, "offset": offset}
            data = _get("/matches", p)
            if not data:
                break
            result = data.get("data") or {}
            matches = result.get("matches") if isinstance(result, dict) else (result if isinstance(result, list) else [])
            if not isinstance(matches, list) or not matches:
                break
            out.extend(matches)
            total = result.get("total") if isinstance(result, dict) else None
            if total is None or offset + len(matches) >= int(total):
                break
            offset += len(matches)
        return out

    def fetch_matches_by_date_range(self, start: str, end: str, lottery_type: str = "jingcai",
                                    is_jingcai: bool = False) -> list[dict]:
        """API 查询日期范围内的全部比赛（分页拉完，is_jingcai=True 附带竞彩让球数据）"""
        params = {"start_date": start, "end_date": end}
        if lottery_type and lottery_type != "all":
            params["type"] = lottery_type
        if is_jingcai:
            params["is_jingcai"] = "true"
        return self._fetch_all_matches(params)

    def fetch_match_by_id(self, lota_id: str) -> Optional[dict]:
        """API 查询单场比赛详情（含比分）"""
        data = _get("/matches", {"lota_id": lota_id})
        if not data:
            return None
        result = data.get("data") or {}
        matches = result.get("matches") if isinstance(result, dict) else result
        if isinstance(matches, list) and matches:
            return matches[0]
        return None

    def fetch_compact_fet(self, lota_id: str) -> Optional[dict]:
        """API 查询 compact-fet"""
        return _get("/compact-fet", {"lota_id": lota_id})

    def get_cached_matches(self, date_str: str, lottery_type: str = "jingcai") -> list[dict]:
        """本地缓存: 某日比赛列表"""
        path = MATCHES_DIR / f"{date_str}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                matches = data if isinstance(data, list) else data.get("matches", [])
                if lottery_type and lottery_type != "all":
                    matches = [m for m in matches if m.get("lottery_type", "") == lottery_type]
                return matches
            except Exception:
                pass
        return []

    def get_cached_jc_matches(self, date_str: str) -> list[dict]:
        """读取某足球日缓存中的竞彩比赛（jingcai_number 非空），含 jc_hhad 让球数据。

        缓存没有 lottery_type 字段，直接按 jingcai_number 过滤，供串关等竞彩玩法使用。
        """
        return [
            m for m in self.get_cached_matches(date_str, lottery_type="all")
            if m.get("jingcai_number")
        ]

    def _merge_preserved_odds(self, ms: list[dict], date_str: str,
                              with_jc_odds: bool, with_beidan_odds: bool) -> None:
        """把旧缓存里未重抓的赔率字段补回 ms（原地），避免刷新一种盘口冲掉另一种。

        仅当对应盘口本次不刷新时才有意义；旧行在 matches/<date>.json 中不存在则跳过。
        """
        if with_jc_odds and with_beidan_odds:
            return
        old_map = {
            m.get("lota_id"): m
            for m in self.get_cached_matches(date_str, lottery_type="all")
        }
        for m in ms:
            om = old_map.get(m.get("lota_id"))
            if not om:
                continue
            if not with_jc_odds and m.get("jc_hhad") is None and om.get("jc_hhad") is not None:
                m["jc_hhad"] = om["jc_hhad"]
            if not with_beidan_odds and m.get("beidan_info") is None and om.get("beidan_info") is not None:
                m["beidan_info"] = om["beidan_info"]

    def save_matches_cache(self, date_str: str, matches: list[dict]) -> None:
        """写入比赛列表缓存（缩进格式，与仓库现有文件一致）"""
        _atomic_write_text(
            MATCHES_DIR / f"{date_str}.json",
            json.dumps(matches, ensure_ascii=False, indent=2),
        )

    def _cached_feature_time(self, lota_id: str) -> str:
        """从 features/<lid>.json 的 compact-fet 头部读真实开赛时间（无缓存返回空串）。"""
        path = FEATURES_DIR / f"{lota_id}.json"
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            text = (data.get("compact_fet") if isinstance(data, dict) else "") or ""
        except Exception:
            return ""
        m = re.search(r"⏰时间:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2})", text)
        return m.group(1).replace("T", " ")[:16] if m else ""

    @staticmethod
    def _time_shift_hours(a: str, b: str) -> float:
        """两个 'YYYY-MM-DD HH:MM' 的绝对小时差；任一侧不可解析返回 0。"""
        try:
            ta = datetime.strptime(str(a).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
            tb = datetime.strptime(str(b).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return 0.0
        return abs((ta - tb).total_seconds()) / 3600.0

    @staticmethod
    def _football_day_of(match_time: str) -> str:
        """'YYYY-MM-DD HH:MM' → 所属足球日起始日（[D 12:01, D+1 12:00]）。"""
        try:
            mdt = datetime.strptime(str(match_time).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return ""
        return (mdt - timedelta(hours=12, minutes=1)).date().isoformat()

    def reconcile_match_times(self, day: str, *, with_source: bool = True,
                              max_source_checks: int = 20) -> dict:
        """校正缓存里被"临时赛程"写死的 match_time，并把比赛搬到正确的足球日桶。

        背景（2026-09-11 事故）：数据源早期给某场沙特联临时时间 09-11 02:00，缓存
        之后源把赛程改到 09-11 23:45；缓存里的旧时间让这场"周五002"落进足球日
        09-10 的竞彩清单，多只狗据此提前一天下注。

        可疑判据（命中任一）：
          1. 竞彩编号周X ≠ 本足球日星期（周五002 出现在周四的桶里）
          2. features/<lid>.json 的 compact-fet ⏰时间 与缓存 match_time 差 > 30min
        处理：以数据源单场接口为准改写 match_time；新时间属于别的足球日则把记录
        搬到对应桶文件并从原桶移除。返回校正明细（供 prepare 告警/审计）。
        """
        source_matches = self.get_cached_matches(day, lottery_type="all")
        if not source_matches:
            return {"day": day, "suspects": 0, "corrected": [], "moved": [], "checks": 0}

        want_wd = football_day_weekday(day)
        suspects: list[tuple[dict, str]] = []
        for m in source_matches:
            lid = m.get("lota_id")
            if not lid:
                continue
            num_wd = jc_number_weekday(m.get("jingcai_number"))
            if num_wd and want_wd and num_wd != want_wd:
                suspects.append(
                    (m, f"竞彩编号 {m.get('jingcai_number')} 属{num_wd}，本足球日是{want_wd}")
                )
                continue
            feat_time = self._cached_feature_time(lid)
            if feat_time and self._time_shift_hours(feat_time, m.get("match_time", "")) > 0.5:
                suspects.append(
                    (m, f"特征时间 {feat_time} ≠ 列表时间 {str(m.get('match_time'))[:16]}")
                )

        corrected: list[dict] = []
        moved: list[dict] = []
        checks = 0
        changed_days: set[str] = set()
        for m, reason in suspects:
            lid = m["lota_id"]
            old_time = str(m.get("match_time") or "")[:16]
            new_time = ""
            if with_source and checks < max_source_checks:
                checks += 1
                try:
                    rec = self.fetch_match_by_id(lid) or {}
                except Exception:
                    rec = {}
                new_time = str(rec.get("match_time") or "")[:16]
                for key in ("jingcai_number", "beidan_number", "state", "state_name"):
                    if rec.get(key) not in (None, ""):
                        m[key] = rec[key]
            if not new_time:
                new_time = self._cached_feature_time(lid)
            if not new_time or new_time == old_time:
                continue
            m["match_time"] = new_time if len(new_time) > 16 else f"{new_time}:00"
            entry = {"lota_id": lid, "old": old_time, "new": new_time,
                     "reason": reason, "home": m.get("home_name"), "away": m.get("away_name")}
            corrected.append(entry)
            target_day = self._football_day_of(new_time)
            if target_day and target_day != day:
                moved.append({**entry, "from": day, "to": target_day})

        if corrected:
            removed = {c["lota_id"] for c in corrected if self._football_day_of(c["new"]) != day}
            day_ms = [m for m in source_matches if m.get("lota_id") not in removed]
            day_ms.sort(key=lambda x: x.get("match_time", ""))
            self.save_matches_cache(day, day_ms)
            changed_days.add(day)
            for mv in moved:
                target = mv["to"]
                tms = self.get_cached_matches(target, lottery_type="all")
                tms = [m for m in tms if m.get("lota_id") != mv["lota_id"]]
                rec = next((x for x in source_matches if x.get("lota_id") == mv["lota_id"]), None)
                if rec is None:
                    continue
                tms.append(rec)
                tms.sort(key=lambda x: x.get("match_time", ""))
                self.save_matches_cache(target, tms)
                changed_days.add(target)

        return {"day": day, "suspects": len(suspects), "corrected": corrected,
                "moved": moved, "checks": checks, "changed_days": sorted(changed_days)}

    def refresh_matches_cache(self, date_str: str, with_jc_odds: bool = False,
                              with_beidan_odds: bool = False) -> list[dict]:
        """刷新某日比赛缓存（全量），可选叠加竞彩让球到 jc_hhad、北单让球/开奖到 beidan_info。

        服务器 is_jingcai=true / is_beidan=true 只返回对应子集，因此先拉全量比赛，
        再单独拉对应子集，按 lota_id 把 jc_hhad / beidan_info 合并进全量缓存，
        避免覆盖掉非对应类型的比赛。未请求重抓的赔率类型保留旧缓存的字段
        （避免只刷 beidan 时把已有 jc_hhad 冲掉，反之亦然）。
        """
        ms = self.fetch_matches_by_date(date_str, lottery_type="all")
        if not ms:
            return ms
        self._merge_preserved_odds(ms, date_str, with_jc_odds, with_beidan_odds)

        if ms and with_jc_odds:
            jc_ms = self.fetch_matches_by_date(date_str, lottery_type="all", is_jingcai=True)
            jc_hhad_map = {
                m.get("lota_id"): m.get("jc_hhad")
                for m in jc_ms if m.get("lota_id")
            }
            for m in ms:
                lid = m.get("lota_id")
                if lid in jc_hhad_map:
                    m["jc_hhad"] = jc_hhad_map[lid]

        if ms and with_beidan_odds:
            beidan_ms = self.fetch_beidan_matches_by_date(date_str)
            beidan_map = {
                m.get("lota_id"): m.get("beidan_info")
                for m in beidan_ms if m.get("lota_id") and m.get("beidan_info") is not None
            }
            for m in ms:
                lid = m.get("lota_id")
                if lid in beidan_map:
                    m["beidan_info"] = beidan_map[lid]

        self.save_matches_cache(date_str, ms)
        return ms

    def refresh_matches_range(self, start_date: str, end_date: str,
                              with_jc_odds: bool = False,
                              with_beidan_odds: bool = False) -> dict:
        """按足球日起始日批量刷新 [start_date, end_date] 的比赛缓存。

        一次范围拉取（分页），按足球日窗口 [D 12:01, D+1 12:00] 切分写盘到 D.json。
        with_jc_odds=True 时额外拉一次竞彩子集，with_beidan_odds=True 时额外拉一次北单子集，
        分别把 jc_hhad / beidan_info 按 lota_id 合并进全量缓存；未重抓的类型保留旧字段。

        Returns: {date_str: 场数}
        """
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") + timedelta(hours=12, minutes=1)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1, hours=12)

        all_ms = self.fetch_matches_by_date_range(
            start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            lottery_type="all",
        )
        if not all_ms:
            return {}

        # 保留旧缓存里未重抓的赔率字段（分桶后按 D.json 逐日回填）
        for cd in {start_date, end_date}:
            day_ms = [m for m in all_ms if (m.get("match_time") or "")[:10] == cd]
            if day_ms:
                self._merge_preserved_odds(day_ms, cd, with_jc_odds, with_beidan_odds)

        if with_jc_odds:
            jc_ms = self.fetch_matches_by_date_range(
                start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                lottery_type="all",
                is_jingcai=True,
            )
            jc_hhad_map = {
                m.get("lota_id"): m.get("jc_hhad")
                for m in jc_ms if m.get("lota_id")
            }
            for m in all_ms:
                lid = m.get("lota_id")
                if lid in jc_hhad_map:
                    m["jc_hhad"] = jc_hhad_map[lid]

        if with_beidan_odds:
            beidan_ms = self.fetch_beidan_matches_by_date_range(
                start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            beidan_map = {
                m.get("lota_id"): m.get("beidan_info")
                for m in beidan_ms if m.get("lota_id") and m.get("beidan_info") is not None
            }
            for m in all_ms:
                lid = m.get("lota_id")
                if lid in beidan_map:
                    m["beidan_info"] = beidan_map[lid]

        # 按足球日切分: [D 12:01, D+1 12:00] 的比赛 → D.json
        buckets: dict[str, list[dict]] = {}
        for m in all_ms:
            mt = m.get("match_time", "")
            if len(mt) < 16:
                continue
            try:
                mdt = datetime.strptime(mt[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if mdt < start_dt or mdt > end_dt:
                continue
            d = (mdt - timedelta(hours=12, minutes=1)).date().isoformat()
            buckets.setdefault(d, []).append(m)

        written = {}
        for d in sorted(buckets):
            if not (start_date <= d <= end_date):
                continue
            buckets[d].sort(key=lambda x: x.get("match_time", ""))
            self.save_matches_cache(d, buckets[d])
            written[d] = len(buckets[d])
        return written


    def fetch_beidan_matches_by_date_range(self, start_date: str, end_date: str) -> list[dict]:
        """从 v2-api 拉取日期范围内的北单比赛（带 beidan_info: goal_line+开奖sp）."""
        return self._fetch_all_matches({
            "start_date": start_date,
            "end_date": end_date,
            "is_beidan": "true",
        })

    def _read_legacy_beidan(self, date_str: str) -> list[dict]:
        """读过渡期的 legacy beidan/<date>.json（北单子集，含 beidan_info）。"""
        path = BEIDAN_DIR / f"{date_str}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else data.get("matches", [])
            except Exception:
                pass
        return []

    def get_cached_beidan_matches(self, date_str: str) -> list[dict]:
        """本地缓存: 某足球日的北单比赛（含 beidan_info）。

        统一从 matches/<date>.json 读取（北单是比赛列表子集，按 beidan_number 后过滤）。
        若统一缓存里旧数据尚未带 beidan_info，则用 legacy beidan/<date>.json 补全。
        """
        ms = [
            m for m in self.get_cached_matches(date_str, lottery_type="all")
            if m.get("beidan_number")
        ]
        if not ms:
            return self._read_legacy_beidan(date_str)
        by_id = {m.get("lota_id"): m for m in ms}
        filled = False
        for lm in self._read_legacy_beidan(date_str):
            lid = lm.get("lota_id")
            if lid in by_id and not by_id[lid].get("beidan_info") and lm.get("beidan_info"):
                by_id[lid]["beidan_info"] = lm["beidan_info"]
                filled = True
        return ms

    def refresh_beidan_cache(self, start_date: str, end_date: str) -> dict:
        """把日期范围内的北单比赛（含 beidan_info）合并进统一 matches/<date>.json 缓存。

        不再单独写 beidan/<date>.json（避免重复存比赛）；按比赛 match_time 的日历日分桶，
        upsert 到对应 matches/<cd>.json：已有行保留 jc_hhad 等字段，仅更新 beidan_info；
        缺失行（旧日期无 matches 缓存）则以北单完整行补入。
        上游 502/断网时 fetch 会返回空或半截结果：一律不写缓存（保留宕机前的旧缓存），并抛错。

        Returns: {日历日: 北单场数}
        """
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") + timedelta(hours=12, minutes=1)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1, hours=12)
        ms = self.fetch_beidan_matches_by_date_range(
            start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if _last_api_error:
            err = _last_api_error
            raise LotaAPIError(
                err.get("status") or 502,
                f"北单数据源上游异常（{err.get('msg', '')}），已中止刷新，保留现有比赛缓存",
            )
        if not ms:
            return {}

        # 按 match_time 的日历日分桶（与 matches/<date>.json 键一致）
        buckets: dict[str, list[dict]] = defaultdict(list)
        for m in ms:
            mt = m.get("match_time", "")
            if len(mt) < 10:
                continue
            buckets[mt[:10]].append(m)

        # backfill 窗口会跨到 end_date+1 的凌晨（属于 end_date 足球日），需一并写入
        cal_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        written: dict[str, int] = {}
        for cd in sorted(buckets):
            if not (start_date <= cd <= cal_end):
                continue
            path = MATCHES_DIR / f"{cd}.json"
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    existing = raw if isinstance(raw, list) else (raw.get("matches") or [])
                except Exception:
                    existing = []
            else:
                existing = []
            by_id = {m.get("lota_id"): m for m in existing if m.get("lota_id")}
            for m in buckets[cd]:
                lid = m.get("lota_id") or ""
                if not lid:
                    continue
                row = by_id.get(lid)
                if row is None:
                    by_id[lid] = dict(m)
                else:
                    jc = row.get("jc_hhad")
                    row.clear()
                    row.update(m)
                    if jc is not None and row.get("jc_hhad") is None:
                        row["jc_hhad"] = jc
                by_id[lid]["beidan_info"] = m.get("beidan_info")
            merged = sorted(by_id.values(), key=lambda x: str(x.get("match_time", "")))
            self.save_matches_cache(cd, merged)
            written[cd] = sum(1 for x in merged if x.get("beidan_number"))
        return written

    def refresh_beidan_history(self, days: int = 60) -> dict:
        """回填过去 days 天的北单缓存（含 goal_line + 开奖sp），合并进统一 matches/*.json."""
        today = datetime.now().date()
        start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        return self.refresh_beidan_cache(start, end)

    def get_cached_beidan_match(self, lota_id: str) -> Optional[dict]:
        """按 lota_id 查找单场北单比赛（含 beidan_info）。优先统一 matches 缓存，回退 legacy。"""
        if not lota_id:
            return None
        for path in sorted(MATCHES_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                matches = data if isinstance(data, list) else data.get("matches", [])
                for m in matches:
                    if m.get("lota_id") == lota_id and m.get("beidan_number"):
                        return m
            except Exception:
                continue
        for path in sorted(BEIDAN_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                matches = data if isinstance(data, list) else data.get("matches", [])
                for m in matches:
                    if m.get("lota_id") == lota_id:
                        return m
            except Exception:
                continue
        return None

    def get_cached_beidan_results(self, lota_ids: set[str]) -> dict[str, dict]:
        """批量返回 {lota_id: beidan_info}。优先统一 matches 缓存，缺失再回退 legacy beidan。"""
        result: dict[str, dict] = {}
        if not lota_ids:
            return result
        for path in sorted(MATCHES_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            matches = data if isinstance(data, list) else data.get("matches", [])
            for m in matches:
                lid = m.get("lota_id")
                if lid in lota_ids and m.get("beidan_info") and lid not in result:
                    result[lid] = m["beidan_info"]
            if len(result) >= len(lota_ids):
                return result
        for path in sorted(BEIDAN_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            matches = data if isinstance(data, list) else data.get("matches", [])
            for m in matches:
                lid = m.get("lota_id")
                if lid in lota_ids and m.get("beidan_info") and lid not in result:
                    result[lid] = m["beidan_info"]
            if len(result) >= len(lota_ids):
                break
        return result

    def save_beidan_sp_cache(self, sp_date: str, sp_map: dict[str, dict]) -> None:
        """把某足球日的原始开奖 SP 持久化到独立缓存，避免比赛缓存轮换导致开奖丢失。"""
        if not sp_map:
            return
        _atomic_write_text(
            BEIDAN_SP_DIR / f"{sp_date}.json",
            json.dumps(sp_map, ensure_ascii=False, indent=2),
        )

    def get_beidan_sp_cache(self, sp_date: str) -> dict[str, dict]:
        """读取某足球日的原始开奖 SP 缓存。"""
        path = BEIDAN_SP_DIR / f"{sp_date}.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def merge_beidan_sp(self, sp_map: dict[str, dict],
                        day: Optional[str] = None) -> dict:
        """把开奖 SP 合并进本地 beidan_info（按 lota_id 更新 matches/ 与 beidan/ 缓存）。

        合并前做合理性校验：官方 result 与 score+goal_line 推导方向矛盾（脏值）的场次
        只标记 result_suspect，不合并 result/spvalue，结算时按未开奖跳过，避免按脏值结错账。

        day 非空时限定写入足球日 day 的窗口 [day 12:01, day+1 12:00] 内的场次，
        避免上游把别的足球日的开奖塞进这一天时污染缓存。

        Returns: {"updated": 成功合并场数, "dirty": 被判脏值并标记的场数}
        """
        if not sp_map:
            return {"updated": 0, "dirty": 0}
        window = beidan_day_window(day) if day else None
        updated = 0
        dirty = 0
        for d in (MATCHES_DIR, BEIDAN_DIR):
            for path in sorted(d.glob("*.json")):
                try:
                    u, dd = self._merge_sp_into_file(path, sp_map, window)
                    updated += u
                    dirty += dd
                except Exception:
                    continue
        return {"updated": updated, "dirty": dirty}

    def _beidan_result_suspect(self, sp: dict, goal_line) -> bool:
        """官方开奖 result 是否与 score+goal_line 推导方向矛盾（脏值）。

        背景：上游 /beidan/sp 曾把「未开奖」页面的赛前赔率误抓成开奖 SP
        （如桑普多利亚 vs 尤维斯塔比亚：库里 result=3/sp=1.82，实际应为 result=0/sp=10.89）。
        仅当 result、score、goal_line 均可得且推导方向与官方结果不一致时判为可疑。

        goal_line 缺失时必须放行：handicap_result 会把缺失的让球线当 0 处理，
        从而把「主队受让 4 球输了 = 官方平」这类正常场次误判成脏值
        （实测 2026-09-14 足球日 9 场被误标，拖住了 09-15 的结算）。
        """
        raw = sp.get("result")
        score = sp.get("score")
        if raw is None or str(raw).strip() == "" or str(raw).strip() == "*":
            return False
        if not score or ":" not in str(score):
            return False
        if goal_line is None or str(goal_line).strip() == "":
            return False
        try:
            actual = _beidan_result_code_to_pick(str(raw).strip())
            derived = _beidan_handicap_result(str(score), goal_line)
        except Exception:
            return False
        return bool(actual and derived and actual != derived)

    def _merge_sp_into_file(self, path: Path, sp_map: dict[str, dict],
                            window: Optional[tuple[str, str]] = None) -> tuple[int, int]:
        """把 sp_map 合并进单个缓存文件。

        window 非空时只合并 match_time 落在窗口内的场次；match_time 缺失的行跳过。

        Returns: (updated, dirty) —— 正常合并场数 / 被判脏值仅标记的场数。
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0, 0
        if isinstance(data, list):
            matches = data
        elif isinstance(data, dict):
            matches = data.get("matches")
        else:
            return 0, 0
        if not isinstance(matches, list):
            return 0, 0
        updated = 0
        dirty = 0
        for m in matches:
            if not isinstance(m, dict):
                continue
            if window:
                mt = str(m.get("match_time") or "")
                if not mt or not (window[0] <= mt <= window[1]):
                    continue
            sp = sp_map.get(m.get("lota_id"))
            if not sp:
                continue
            bi = m.get("beidan_info") or {}
            if self._beidan_result_suspect(sp, bi.get("goal_line")):
                # 脏值：标记可疑，不合并 result/spvalue，结算将按未开奖跳过
                bi["result_suspect"] = True
                m["beidan_info"] = bi
                dirty += 1
                continue
            for k in ("result", "result_des", "spvalue", "score", "draw_datetime"):
                if sp.get(k) is not None:
                    bi[k] = sp[k]
            # 之前按脏值标过的 result_suspect 在成功合并后要清掉，否则这行永远
            # 按「未开奖」结算（9 场误标就卡住了整天的腿）。
            bi.pop("result_suspect", None)
            m["beidan_info"] = bi
            updated += 1
        if updated:
            _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        return updated, dirty


    def get_cached_match(self, lota_id: str) -> Optional[dict]:
        """从本地缓存查找单场比赛（扫描 matches + features）"""
        # 1. 从 features 缓存中获取（含 match 基础信息）
        #    但跳过无效缓存（state=None/-1 表示网络错误时的占位数据）
        feat = self.get_cached_compact_fet(lota_id)
        if feat and not feat.get("_api_failed"):
            match = feat.get("match") or (feat.get("data") or {}).get("match")
            if match and match.get("state") not in (None, -1):
                return match
        # 2. 扫描 matches 目录（lottery_type="all" 避免默认 "jingcai" 过滤掉 None 值）
        for d in self._recent_dates(30):
            for m in self.get_cached_matches(d, lottery_type="all"):
                if m.get("lota_id") == lota_id:
                    return m
        return None
    
    def refresh_score_match(self, lota_id: str) -> Optional[dict]:
        """通过 API 查询单场比赛最新比分/状态（跨进程单飞，见 _refresh_score_match_impl）。"""
        with _file_lock(f"score:{lota_id}", timeout=300):
            return self._refresh_score_match_impl(lota_id)

    def _refresh_score_match_impl(self, lota_id: str) -> Optional[dict]:
        """
        通过 API 查询单场比赛最新比分/状态，更新写回 matches + features 缓存。
        仅对已完场(state==6)的比赛请求 API。返回 API match dict 或 None。
        """
        now_bj = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)

        # 先查本地缓存判断是否需要请求 API
        cached = self.get_cached_match(lota_id)
        if cached:
            state = cached.get("state", 0)
            score = cached.get("score", "")
            if state == 6 and score and score != ":" and len(score) >= 3:
                return cached  # 已有完场比分，无需 API
            # state=-1 或 None：网络错误时的占位缓存，允许重新请求 API
            if state in (None, -1):
                pass  # 继续走 API 刷新
            elif state == 0:
                # 未开赛 → 检查 match_time
                mt = cached.get("match_time", "")
                if mt:
                    try:
                        mt_clean = mt.replace("T", " ")[:16]
                        if datetime.strptime(mt_clean, "%Y-%m-%d %H:%M") > now_bj:
                            return None  # 尚未开赛，无需 API
                    except ValueError:
                        pass
            else:
                # state 1-5：已开赛/进行中 → 只有开赛超过 3 小时仍未完场才允许刷新
                # （比赛进行中不打 API 避免骚扰源端；踢完但缓存没更新的比赛借此补比分）
                mt = cached.get("match_time", "")
                elapsed_ok = False
                if mt:
                    try:
                        mt_clean = mt.replace("T", " ")[:16]
                        elapsed_ok = (
                            datetime.strptime(mt_clean, "%Y-%m-%d %H:%M")
                            + timedelta(hours=3)
                            < now_bj
                        )
                    except ValueError:
                        pass
                if not elapsed_ok:
                    return None
        else:
            # 无缓存匹配记录：从 features 缓存解析 match_time，若未开赛则跳过
            feat = self.get_cached_compact_fet(lota_id)
            if feat and not feat.get("_api_failed"):
                mt = self._parse_match_time_from_fet(feat)
                if mt:
                    try:
                        if datetime.strptime(mt, "%Y-%m-%d %H:%M") > now_bj:
                            return None  # 尚未开赛，跳过 API
                    except ValueError:
                        pass

        fetched = self.fetch_match_by_id(lota_id)
        if not fetched:
            return None

        state = fetched.get("state", 0)
        score = (
            fetched.get("score") or
            f"{fetched.get('home_score', '')}:{fetched.get('away_score', '')}"
        )

        # 更新 matches 日期文件缓存
        for date_file in sorted(MATCHES_DIR.glob("*.json")):
            try:
                raw = json.loads(date_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            if isinstance(raw, dict):
                matches = raw.get("matches", [])
            elif isinstance(raw, list):
                matches = raw
            else:
                continue

            dirty = False
            for m in matches:
                if m.get("lota_id") == lota_id:
                    if state == 6:
                        m["state"] = 6
                        if score and score != ":":
                            m["score"] = score
                    elif state != m.get("state", 0):
                        m["state"] = state
                    dirty = True
                    break

            if dirty:
                if isinstance(raw, dict):
                    raw["matches"] = matches
                else:
                    raw = matches
                date_file.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                break  # 只会在一个日期文件中

        # 同步更新 features 缓存中的 score
        if state == 6 and score and score != ":":
            feat = self.get_cached_compact_fet(lota_id)
            if feat:
                data = feat.get("data") or {}
                data["score"] = score
                feat["data"] = data
                self.save_compact_fet_cache(lota_id, feat)

        return fetched

    def refresh_scores(self):
        """
        扫描本地 matches 缓存中所有已开始但未标记完场的比赛，
        通过 API 查询最新比分/状态，更新写回缓存文件。
        """
        # match_time 是北京时间(UTC+8)，now 也钉死北京时间，避免宿主机时区不一致导致误判
        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        total_updated = 0
        total_checked = 0

        date_files = sorted(MATCHES_DIR.glob("*.json"))
        if not date_files:
            print("[refresh_scores] 无缓存文件")
            return

        print(f"[refresh_scores] 扫描 {len(date_files)} 个日期文件...")

        for date_file in date_files:
            try:
                raw = json.loads(date_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            if isinstance(raw, dict):
                matches = raw.get("matches", [])
            elif isinstance(raw, list):
                matches = raw
            else:
                continue

            if not isinstance(matches, list):
                continue

            dirty = False
            for m in matches:
                lid = m.get("lota_id", "")
                if not lid:
                    continue
                state = m.get("state", 0)
                if state == 6:
                    continue  # 已完场

                # 检查比赛是否已开始（match_time < now）
                mt = m.get("match_time", "")
                if mt:
                    try:
                        mt_clean = mt.replace("T", " ")[:16]
                        if datetime.strptime(mt_clean, "%Y-%m-%d %H:%M") > now:
                            continue  # 未开始
                    except ValueError:
                        pass

                total_checked += 1
                fetched = self.fetch_match_by_id(lid)
                if not fetched:
                    continue

                new_state = fetched.get("state", 0)
                if new_state == 6:
                    score = (
                        fetched.get("score") or
                        f"{fetched.get('home_score', '')}:{fetched.get('away_score', '')}"
                    )
                    m["state"] = 6
                    if score and score != ":":
                        m["score"] = score
                    dirty = True
                    total_updated += 1
                    print(f"  ✅ {lid} {m.get('home_name', '?')} vs {m.get('away_name', '?')}: {score}")

                    # 同步更新 features 缓存中的 score
                    feat = self.get_cached_compact_fet(lid)
                    if feat and score and score != ":":
                        data = feat.get("data") or {}
                        data["score"] = score
                        feat["data"] = data
                        self.save_compact_fet_cache(lid, feat)

                elif new_state != state:
                    m["state"] = new_state
                    dirty = True

                time.sleep(0.05)  # 温和限速

            # 写回日期文件
            if dirty:
                try:
                    if isinstance(raw, dict):
                        raw["matches"] = matches
                    else:
                        raw = matches
                    date_file.write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception as e:
                    print(f"  ❌ 写回 {date_file.name} 失败: {e}")

        print(f"[refresh_scores] 完成: 检查 {total_checked}, 更新 {total_updated} 场")



    def get_match(self, lota_id: str, refresh: bool = False) -> Optional[dict]:
        """
        获取比赛信息。已完场优先本地, 没有再走 API。

        规则:
          - 先查本地缓存
          - 如果缓存命中且 state==6（完场），直接返回（除非 refresh=True）
          - 否则尝试 API 获取最新数据并更新缓存
        """
        cached = self.get_cached_match(lota_id)
        if cached and not refresh:
            state = cached.get("state", 0)
            if state == 6:  # 完场，本地是权威数据
                return cached

        # 走 API
        fetched = self.fetch_match_by_id(lota_id)
        if fetched:
            return fetched
        return cached  # API 失败则返回缓存

    def _recent_dates(self, days: int = 30) -> list[str]:
        return [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    # ═══════════════════════════════════════════
    # Compact-fet（原始 JSON + 缓存）
    # ═══════════════════════════════════════════

    def get_cached_compact_fet(self, lota_id: str) -> Optional[dict]:
        """读取本地 compact-fet 缓存（先 Python CLI 目录，再 JS 项目目录）

        回测模式（切片源启用）：索引内的场次**只**认 fet_txt 切片，
        命中返回切片 payload、无可用档位返回 None（绝不回退实时缓存）。
        """
        src = _backtest_fet()
        if src is not None and src.in_scope(lota_id):
            return src.compact_fet(lota_id)
        # 1. JS 项目 features（主要缓存）
        path = FEATURES_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # 2. Python CLI 自有目录
        py_feat = Path(__file__).parent.parent / "data" / "features" / f"{lota_id}.json"
        if py_feat.exists():
            try:
                return json.loads(py_feat.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def save_compact_fet_cache(self, lota_id: str, data: dict) -> None:
        """写入 compact-fet 缓存"""
        data["_cached_at"] = datetime.now().isoformat()
        _atomic_write_text(
            FEATURES_DIR / f"{lota_id}.json",
            json.dumps(data, ensure_ascii=False),
        )

    # live / upcoming 比赛的 compact-fet 缓存有效期（秒）
    # 15 分钟：避免频繁重复拉取同一场未开赛赔率，降低上游限流风险。
    COMPACT_FET_CACHE_TTL = 15 * 60
    # 临近开赛时缩短 TTL，避免 15 分钟窗口内的盘口移动被旧缓存掩盖
    NEAR_KICKOFF_MINUTES = 60
    NEAR_KICKOFF_TTL = 2 * 60

    # negative cache（_api_failed）有效期：超时后允许重试，避免瞬时故障永久跳过
    NEGATIVE_CACHE_TTL = 720  # 12 分钟

    def get_compact_fet(self, lota_id: str, refresh: bool = False) -> Optional[dict]:
        """
        获取 compact-fet。缓存策略:

          - 已完场 / 已开赛: 缓存直接用（开赛后赔率已锁定，无需刷新）
          - 未开赛 (match_time > now): 缓存 COMPACT_FET_CACHE_TTL 秒有效，过期自动刷新
          - live 模式下未开赛刷新失败: 拒绝回退过期缓存（见 _live_strict）
          - refresh=True: 强制跳过缓存，直接走 API

        失败时写入 negative cache（_api_failed=True），带 TTL 过期后可重试。
        """
        if not refresh:
            cached = self.get_cached_compact_fet(lota_id)
            if cached:
                if cached.get("_api_failed"):
                    # negative cache 有 TTL，过期后允许重试
                    cached_at = cached.get("_cached_at", "")
                    if cached_at:
                        try:
                            ct = datetime.fromisoformat(cached_at)
                            if (datetime.now() - ct).total_seconds() < self.NEGATIVE_CACHE_TTL:
                                return None
                        except Exception:
                            pass
                    # 过期或无法解析时间 → 走 API 重试（不直接 return None）

                # 已完场比赛 → 缓存永久有效
                if self._is_match_finished(cached):
                    return cached

                # 已开赛（match_time <= now）→ 直接用缓存，开赛后赔率已锁定
                if not self._is_match_upcoming(cached):
                    return cached

                # 未开赛 → 检查 TTL（临近开赛自动缩短）
                cached_at = cached.get("_cached_at", "")
                if cached_at:
                    try:
                        ct = datetime.fromisoformat(cached_at)
                        if (datetime.now() - ct).total_seconds() < self._compact_fet_ttl(cached):
                            return cached
                    except Exception:
                        pass
                # 缓存过期，走 API

        data = self.fetch_compact_fet(lota_id)
        if data:
            # 保留旧缓存中的 score（API 返回的 compact-fet 可能不含比分）
            if not refresh:
                old = self.get_cached_compact_fet(lota_id)
                if old:
                    old_score = (old.get("data") or {}).get("score", "")
                    if old_score and old_score != ":":
                        (data.get("data") or {})["score"] = old_score
            self.save_compact_fet_cache(lota_id, data)
        else:
            old = self.get_cached_compact_fet(lota_id)
            if old and not old.get("_api_failed") and old.get("compact_fet"):
                if self._live_strict and self._is_match_upcoming(old):
                    # live 模式：禁止用过期缓存兜底（旧赔率会进提示词，误导分析）
                    old_at = old.get("_cached_at", "") or "?"
                    print(
                        f"[data] 🔒 live 模式: {lota_id} compact-fet 刷新失败，"
                        f"拒绝使用旧缓存 ({old_at})，跳过该场数据（未开赛场次）"
                    )
                    return None
                # API 失败时：如果已有有效缓存（无 _api_failed），保留旧数据不覆盖
                # 防止瞬时网络故障把正常缓存毒化成 _api_failed 桩
                return old  # 保留旧的有效缓存，等下次 TTL 过期再重试
            # 无旧缓存或旧缓存也是失败桩 → 写入 negative cache（带 TTL，见 NEGATIVE_CACHE_TTL）
            self.save_compact_fet_cache(lota_id, {
                "_api_failed": True,
                "lota_id": lota_id,
            })
        return data

    def _is_match_finished(self, compact_fet: dict) -> bool:
        """从 compact-fet 缓存判断比赛是否已完场"""
        data = compact_fet.get("data") or {}
        match = compact_fet.get("match") or data.get("match") or {}
        # 优先看 state 字段
        state = match.get("state") or data.get("state")
        if state == 6:
            return True
        # 兜底：有实际比分也视为完场
        score = data.get("score") or compact_fet.get("score") or ""
        if score and score != ":" and len(score) >= 3:
            return True
        return False

    def _is_match_upcoming(self, compact_fet: dict) -> bool:
        """是否未开赛（match_time > now）。解析失败时按未开赛处理（保持严格刷新）。"""
        mt = self._parse_match_time_from_fet(compact_fet)
        if not mt:
            return True
        try:
            return datetime.strptime(mt, "%Y-%m-%d %H:%M") > datetime.now()
        except ValueError:
            return True

    def needs_upcoming_refresh(self, lota_id: str) -> bool:
        """该场是否值得强制刷新 compact-fet。

        无缓存 → True；未开赛 → True（赔率还在变，必须拿最新的）；
        已开赛/完场 → False（开赛后赔率已锁定，重抓没有意义且徒增上游压力）。
        """
        cached = self.get_cached_compact_fet(lota_id)
        if not cached:
            return True
        return self._is_match_upcoming(cached)

    def _compact_fet_ttl(self, cached: dict) -> int:
        """未开赛 compact-fet 的缓存 TTL：临近开赛时缩短。"""
        mt = self._parse_match_time_from_fet(cached)
        if mt:
            try:
                kickoff = datetime.strptime(mt, "%Y-%m-%d %H:%M")
                mins = (kickoff - datetime.now()).total_seconds() / 60
                if 0 < mins <= self.NEAR_KICKOFF_MINUTES:
                    return self.NEAR_KICKOFF_TTL
            except Exception:
                pass
        return self.COMPACT_FET_CACHE_TTL

    @staticmethod
    def _parse_match_time_from_fet(feat: dict) -> str:
        """从 compact-fet 文本提取比赛时间 (YYYY-MM-DD HH:MM)"""
        fet_text = feat.get("compact_fet") or ""
        if not fet_text:
            return ""
        m = re.search(r'时间[：:]\s*([\d\-:\s]+)', fet_text)
        return m.group(1).strip()[:16] if m else ""

    def has_usable_compact_fet(self, lota_id: str) -> bool:
        """compact-fet 是否「可用」：存在、非失败桩、且有实际可读内容。

        live 分析用：仅「拉得到」还不够，必须是「拉得到且内容有效」，
        否则把空/旧/失败桩数据放进 prompt 会误导分析和出单（所有狗统一）。
        """
        if not lota_id:
            return False
        cached = self.get_cached_compact_fet(lota_id)
        if not cached or cached.get("_api_failed"):
            return False
        fet_text = cached.get("compact_fet") or ""
        if fet_text.strip():
            return True
        match = cached.get("match") or (cached.get("data") or {}).get("match") or {}
        if match and (match.get("home_name") or match.get("away_name")):
            return True
        return False

    def get_compact_fet_text(self, lota_id: str) -> str:
        """获取 compact-fet 文本（用于 tag 提取）"""
        data = self.get_compact_fet(lota_id)
        if not data:
            return ""
        inner = data.get("data") or {}
        return inner.get("compact_fet") or data.get("compact_fet") or ""

    # ═══════════════════════════════════════════
    # Tags（compact-fet → 语义段落）
    # ═══════════════════════════════════════════

    def get_tags(self, lota_id: str) -> dict[str, str]:
        """
        获取 tagged sections。优先本地 tags 缓存，没有则从 compact-fet 即时切分。
        """
        # 1. 已缓存的 tags
        cached = self._load_cached_tags(lota_id)
        if cached:
            return cached.get("sections", {})

        # 2. 从 compact-fet 切分
        text = self.get_compact_fet_text(lota_id)
        if not text:
            return {}

        sections = _compact_fet_to_tags(lota_id, None)  # 直接用文本（tools 函数）
        # 先尝试用 tools 的 compact_fet_to_tags（它接受 data dict）
        data = self.get_compact_fet(lota_id)
        if data:
            sections = _compact_fet_to_tags(lota_id, data)
        else:
            # fallback: 直接对文本切分
            sections = self._parse_tags_from_text(text)

        self._save_cached_tags(lota_id, sections)
        return sections

    def _parse_tags_from_text(self, text: str) -> dict[str, str]:
        """纯文本 → tagged sections（不依赖 compact-fet JSON）"""
        hits: list[tuple[str, int]] = []
        for slug, pattern in _SECTION_RULES:
            m = re.search(pattern, text)
            if m:
                hits.append((slug, m.start()))
        hits.sort(key=lambda x: x[1])

        sections: dict[str, str] = {}
        for i, (slug, start) in enumerate(hits):
            end = hits[i + 1][1] if i + 1 < len(hits) else len(text)
            sections[slug] = text[start:end].strip()
        return sections

    def get_sections(self, lota_id: str, slugs: list[str],
                     source_order: bool = False) -> str:
        """
        按 slug 列表获取指定段落，拼接为 prompt 可用的文本。

        source_order=True：按**原始数据里的段落顺序**输出（例如 match-head 在最前），
        用于"prompt 要忠实呈现原始 txt"的场景；默认 False 保持各狗原有顺序（隔离红线）。

        用法:
          context = dm.get_sections(lota_id, ["fair-odds", "asian-handicap-crown"])
          # → "[section:fair-odds]\n公平盘数据:\n..."
        """
        sections = self.get_tags(lota_id)
        parts = []
        if source_order:
            wanted = set(slugs or [])
            for slug, text in (sections or {}).items():
                if slug in wanted and text:
                    parts.append(f"[section:{slug}]\n{text}")
            # 源顺序里没有、但被显式点名要的段（例如 extra_slugs 新增）补在后面
            for slug in slugs or []:
                if slug in (sections or {}) or slug in {p.split(":")[1].rstrip("]")
                                                       for p in parts}:
                    continue
                text = (sections or {}).get(slug)
                if text:
                    parts.append(f"[section:{slug}]\n{text}")
            return "\n\n".join(parts)
        for slug in slugs:
            text = sections.get(slug)
            if text:
                parts.append(f"[section:{slug}]\n{text}")
        return "\n\n".join(parts)

    def _load_cached_tags(self, lota_id: str) -> Optional[dict]:
        # 回测模式：段落一律从 fet_txt 切片即时切分，不读线上 tags 缓存
        # （线上 tags 由 live 终盘快照切出，回放读它会前视；也不回写磁盘）
        src = _backtest_fet()
        if src is not None and src.in_scope(lota_id):
            return {
                "lota_id": lota_id,
                "generated_at": datetime.now().isoformat(),
                "sections": src.sections(lota_id),
                "_backtest_fet": True,
            }
        path = TAGS_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def _save_cached_tags(self, lota_id: str, sections: dict[str, str]) -> None:
        payload = {
            "lota_id": lota_id,
            "generated_at": datetime.now().isoformat(),
            "sections": sections,
        }
        (TAGS_DIR / f"{lota_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    # ═══════════════════════════════════════════
    # 数据准备与调度（多狗并发取数）
    # ═══════════════════════════════════════════

    # live 模式下比赛缓存的“新鲜窗口”：窗口内直接用本地，避免多狗重复打线上。
    # 窗口外（或缓存缺失）才在锁内单飞去线上刷新。
    MATCHES_CACHE_MAX_AGE = 5 * 60

    # 北单开奖 SP 的“新鲜窗口”：开奖后 SP 已定稿，窗口内重复结算直接读本地。
    BEIDAN_SP_CACHE_MAX_AGE = 10 * 60

    def prepare_matches(self, date_str: str, live: bool = False,
                        with_jc_odds: bool = False, with_beidan_odds: bool = False,
                        max_age_seconds: Optional[int] = None,
                        owner: str = "") -> dict:
        """协调式拉取某日历日比赛列表（跨进程单飞）。

        规则:
          - 缓存满足「本地就绪」→ 直接本地（source=local）：
              * 非 live → 只要缓存存在即可
              * live → 刷新时间在 max_age_seconds 内
              * 请求了竞彩/北单盘口时，对应场次必须已带 jc_hhad / beidan_info
                （避免竞彩 prefetch 写过的基础缓存缺北单盘口被误判为就绪）
          - 否则在「该日期的互斥锁」内线上刷新；并发的其它进程等锁后
            读到刚写好的缓存 → 自动变 local，不重复打线上
          - 线上刷新为空/失败 → 回退本地缓存并附 warning

        Returns:
            {"date", "matches", "count", "source": "online"|"local"|"none",
             "warning": str|None}
        """
        max_age = max_age_seconds if max_age_seconds is not None else self.MATCHES_CACHE_MAX_AGE
        with _file_lock(f"matches:{date_str}", timeout=600):
            cached = self.get_cached_matches(date_str, lottery_type="all")
            if self._prepare_ready(date_str, cached, live, with_jc_odds,
                                   with_beidan_odds, max_age):
                return {"date": date_str, "matches": cached, "count": len(cached),
                        "source": "local", "warning": None}

            ms = self.refresh_matches_cache(
                date_str, with_jc_odds=with_jc_odds, with_beidan_odds=with_beidan_odds
            )
            if ms:
                _record_dm_state("matches", date_str, {
                    "source": "online",
                    "at": datetime.now().isoformat(),
                    "count": len(ms),
                    "by": owner or "",
                    "live": live,
                    "jc_odds": with_jc_odds,
                    "beidan": with_beidan_odds,
                })
                return {"date": date_str, "matches": ms, "count": len(ms),
                        "source": "online", "warning": None}
            if cached:
                return {"date": date_str, "matches": cached, "count": len(cached),
                        "source": "local",
                        "warning": f"{date_str} 线上拉取为空/失败，回退本地缓存"}
            return {"date": date_str, "matches": [], "count": 0,
                    "source": "none", "warning": f"{date_str} 线上拉取为空"}

    def _prepare_ready(self, date_str: str, cached: list[dict], live: bool,
                       with_jc_odds: bool, with_beidan_odds: bool,
                       max_age_seconds: int) -> bool:
        """本地缓存是否已满足准备就绪（新鲜度 + 盘口完整性）。"""
        if not cached:
            return False
        if live and not self._matches_cache_fresh(date_str, max_age_seconds):
            return False
        if with_jc_odds:
            jc = [m for m in cached if m.get("jingcai_number")]
            if jc and not all(m.get("jc_hhad") for m in jc):
                return False
        if with_beidan_odds:
            bd = [m for m in cached if m.get("beidan_number")]
            # 北单狗跑某天却没有任何北单行 → 视为未就绪，线上确认（与旧 live 语义一致）
            if not bd:
                return False
            if not all(m.get("beidan_info") for m in bd):
                return False
        return True

    def _matches_cache_fresh(self, date_str: str, max_age_seconds: int) -> bool:
        path = MATCHES_DIR / f"{date_str}.json"
        if not path.exists():
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age <= max_age_seconds

    def _compact_fet_status(self, cached: Optional[dict]) -> str:
        """compact-fet 缓存状态: fresh / stale / negative / missing"""
        if not cached:
            return "missing"
        if cached.get("_api_failed"):
            cached_at = cached.get("_cached_at", "")
            if cached_at:
                try:
                    ct = datetime.fromisoformat(cached_at)
                    if (datetime.now() - ct).total_seconds() < self.NEGATIVE_CACHE_TTL:
                        return "negative"  # 失败桩仍有效，不重试
                except Exception:
                    pass
            return "stale"
        # 已完场 / 已开赛 → 赔率锁定，缓存永久有效
        if self._is_match_finished(cached):
            return "fresh"
        if not self._is_match_upcoming(cached):
            return "fresh"
        cached_at = cached.get("_cached_at", "")
        if cached_at:
            try:
                ct = datetime.fromisoformat(cached_at)
                if (datetime.now() - ct).total_seconds() < self._compact_fet_ttl(cached):
                    return "fresh"
            except Exception:
                pass
        return "stale"

    def prepare_features(self, lota_ids: list[str], with_tags: bool = False,
                         owner: str = "") -> dict:
        """协调式预取 compact-fet（可选附带 tags），按 lota_id 跨进程单飞。

        同一场同时只有一个进程去线上拉；其余进程等锁后读到同一份本地缓存。
        TTL 规则沿用 get_compact_fet（完场永久 / 已开赛锁定 / 未开赛 15 分钟）。

        Returns:
            {"total", "cached", "fetched", "failed", "failed_lids": [...]}
        """
        total = len(lota_ids)
        cached = fetched = failed = 0
        failed_lids: list[str] = []

        for lid in lota_ids:
            if not lid:
                continue
            with _file_lock(f"features:{lid}", timeout=600):
                status = self._compact_fet_status(self.get_cached_compact_fet(lid))
                if status == "fresh":
                    cached += 1
                    continue
                if status == "negative":
                    # 失败桩 TTL 内：不重试，视为失败（与 get_compact_fet 语义一致）
                    failed += 1
                    failed_lids.append(lid)
                    continue
                data = self.get_compact_fet(lid)
            if data is None:
                failed += 1
                failed_lids.append(lid)
                continue
            # 区分「真的拉到新数据」与「API 失败回退旧缓存」
            if self._compact_fet_status(self.get_cached_compact_fet(lid)) == "fresh":
                fetched += 1
            else:
                cached += 1  # 旧缓存兜底（与 node_fetch_features 原计数一致）
            if with_tags:
                # 回测模式：段落由切片源即时提供，禁止把切片 tags 写进线上 tags 缓存
                _src = _backtest_fet()
                if _src is not None and _src.in_scope(lid):
                    continue
                try:
                    from .tools import compact_fet_to_tags, save_tagged_sections
                    sections = compact_fet_to_tags(lid, data)
                    if sections:
                        save_tagged_sections(lid, sections)
                except Exception:
                    pass

        return {
            "total": total,
            "cached": cached,
            "fetched": fetched,
            "failed": failed,
            "failed_lids": failed_lids,
        }

    def prepare_beidan_sp(self, sp_date: str, max_age_seconds: Optional[int] = None,
                          owner: str = "") -> dict:
        """协调式拉取并合并某足球日的北单开奖 SP（跨进程单飞）。

        同一 sp_date 同时只有一个进程打 /beidan/sp；窗口内（默认 10 分钟）已成功
        合并过则直接读本地，避免多只北单狗并发结算重复拉取 + 并发写缓存。

        Returns:
            {"date", "source": "online"|"local"|"none", "count", "updated",
             "warning": str|None, "sp": dict[str, dict]|None}
        """
        max_age = max_age_seconds if max_age_seconds is not None else self.BEIDAN_SP_CACHE_MAX_AGE
        with _file_lock(f"beidan-sp:{sp_date}", timeout=600):
            st = (_load_dm_state().get("beidan_sp") or {}).get(sp_date)
            if st and st.get("at") and (st.get("count") or 0) > 0:
                try:
                    ct = datetime.fromisoformat(st["at"])
                    if (datetime.now() - ct).total_seconds() <= max_age:
                        return {"date": sp_date, "source": "local",
                                "count": st.get("count", 0), "updated": 0,
                                "warning": None, "sp": None}
                except Exception:
                    pass

            sp = self.fetch_beidan_sp(sp_date)
            if sp:
                self.save_beidan_sp_cache(sp_date, sp)
                merged = self.merge_beidan_sp(sp, day=sp_date)
                updated = merged["updated"]
                dirty = merged["dirty"]
                _record_dm_state("beidan_sp", sp_date, {
                    "source": "online",
                    "at": datetime.now().isoformat(),
                    "count": len(sp),
                    "updated": updated,
                    "dirty": dirty,
                    "by": owner or "",
                })
                warning = None
                if dirty:
                    warning = (f"{sp_date} 北单开奖SP有 {dirty} 场 result 与比分/让球推导"
                               f"矛盾（脏值），已标 result_suspect 跳过结算")
                return {"date": sp_date, "source": "online", "count": len(sp),
                        "updated": updated, "dirty": dirty, "warning": warning,
                        "sp": sp}
            if st:
                return {"date": sp_date, "source": "local", "count": st.get("count", 0),
                        "updated": 0, "warning": f"{sp_date} 开奖SP拉取失败，保留本地缓存",
                        "sp": None}
            return {"date": sp_date, "source": "none", "count": 0, "updated": 0,
                    "warning": f"{sp_date} 开奖SP拉取为空", "sp": None}

    def prepare_day(self, day_date: str, jingcai_only: bool = True,
                    beidan_only: bool = False, live: bool = False,
                    with_features: bool = True, with_tags: bool = True,
                    owner: str = "", with_jc_odds: Optional[bool] = None,
                    with_beidan_odds: Optional[bool] = None) -> dict:
        """准备一个足球日的完整数据（比赛列表 + compact-fet + tags），返回就绪报告。

        不同狗传不同 day_date / 范围时互不阻塞：同日期共享单飞锁与状态文件，
        已准备好的日期会秒回 ready（source=local），避免重复打线上。

        with_jc_odds / with_beidan_odds 显式控制是否叠加竞彩/北单盘口字段；
        默认 None 时分别跟随 jingcai_only / beidan_only。

        Returns:
            {"day", "window", "calendar_dates", "status": "ready"|"partial"|"empty",
             "ready", "source", "matches": {cd: {...}}, "candidates",
             "features": {...}|None, "failed_lids", "warnings"}
        """
        from datetime import date as _date
        from .environment import get_football_day, football_day_calendar_dates

        if with_jc_odds is None:
            with_jc_odds = jingcai_only
        if with_beidan_odds is None:
            with_beidan_odds = beidan_only
        if beidan_only:
            jingcai_only = False

        d = _date.fromisoformat(day_date)
        window_start, window_end = get_football_day(d)
        cal_dates = football_day_calendar_dates(d)

        match_reports: dict[str, dict] = {}
        for cd in cal_dates:
            match_reports[cd] = self.prepare_matches(
                cd, live=live,
                with_jc_odds=with_jc_odds, with_beidan_odds=with_beidan_odds,
                owner=owner,
            )

        all_matches: list[dict] = []
        for cd in cal_dates:
            all_matches += match_reports[cd].get("matches") or []

        candidates = [
            m for m in all_matches
            if window_start <= str(m.get("match_time", ""))[:16] <= window_end
            and m.get("lota_id")
            and m.get("home_name", "?") not in ("", "?")
            and m.get("away_name", "?") not in ("", "?")
            and (not jingcai_only or m.get("jingcai_number"))
            and (not beidan_only or m.get("beidan_number"))
        ]
        # 去重：同一场可能同时落在相邻日历日缓存
        seen_lids: set[str] = set()
        uniq: list[dict] = []
        for m in candidates:
            lid = m.get("lota_id")
            if lid in seen_lids:
                continue
            seen_lids.add(lid)
            uniq.append(m)
        candidates = uniq

        features = None
        if with_features:
            features = self.prepare_features(
                [m.get("lota_id", "") for m in candidates],
                with_tags=with_tags, owner=owner,
            )

        sources = {r.get("source") for r in match_reports.values()}
        source = ("online" if "online" in sources
                  else ("local" if "local" in sources else "none"))
        failed_count = len(features.get("failed_lids", [])) if features else 0
        # 北单模式：候选场次必须 100% 带 beidan_info（含开奖字段）才算数据就绪
        beidan_missing: list[str] = []
        if beidan_only:
            beidan_missing = [
                m.get("lota_id", "") for m in candidates
                if not m.get("beidan_info")
            ]
        beidan_complete = not beidan_missing
        failed_count += len(beidan_missing)
        status = ("empty" if not candidates
                  else ("ready" if failed_count == 0 else "partial"))

        report = {
            "day": day_date,
            "window": f"{window_start[:16]} ~ {window_end[:16]}",
            "calendar_dates": cal_dates,
            "status": status,
            "ready": status == "ready",
            "source": source,
            "matches": {
                cd: {"source": r.get("source"), "count": r.get("count", 0)}
                for cd, r in match_reports.items()
            },
            "candidates": len(candidates),
            "features": features,
            "failed_lids": features.get("failed_lids", []) if features else [],
            "beidan_complete": beidan_complete,
            "beidan_missing": beidan_missing,
            "warnings": [
                r["warning"] for r in match_reports.values() if r.get("warning")
            ] + ([f"{day_date} {len(beidan_missing)} 场北单缺 beidan_info"]
                 if beidan_missing else []),
        }
        _record_dm_state("days", day_date, {
            "prepared_at": datetime.now().isoformat(),
            "by": owner or "",
            "status": status,
            "ready": report["ready"],
            "source": source,
            "candidates": len(candidates),
            "features_ok": (features.get("fetched", 0) + features.get("cached", 0)
                            if features else None),
            "features_failed": failed_count,
            "beidan_complete": beidan_complete,
        })
        return report

    def prepare_range(self, start_date: str, end_date: str,
                      jingcai_only: bool = True, beidan_only: bool = False,
                      live: bool = False, with_features: bool = True,
                      with_tags: bool = True, owner: str = "",
                      with_jc_odds: Optional[bool] = None,
                      with_beidan_odds: Optional[bool] = None) -> dict:
        """按范围准备多个足球日（覆盖不同狗的不同数据范围），返回逐日就绪报告。"""
        from datetime import date as _date, timedelta as _td

        if with_jc_odds is None:
            with_jc_odds = jingcai_only
        if with_beidan_odds is None:
            with_beidan_odds = beidan_only
        if beidan_only:
            jingcai_only = False

        d = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        days: dict[str, dict] = {}
        total_candidates = total_failed = 0
        while d <= end:
            day_key = d.isoformat()
            report = self.prepare_day(
                day_key, jingcai_only=jingcai_only, beidan_only=beidan_only,
                live=live, with_features=with_features, with_tags=with_tags,
                owner=owner, with_jc_odds=with_jc_odds,
                with_beidan_odds=with_beidan_odds,
            )
            days[day_key] = report
            total_candidates += report["candidates"]
            total_failed += len(report.get("failed_lids") or [])
            total_failed += len(report.get("beidan_missing") or [])
            d += _td(days=1)

        ready_days = sum(1 for r in days.values() if r["ready"])
        return {
            "start": start_date,
            "end": end_date,
            "status": ("ready" if ready_days == len(days)
                       else ("partial" if days else "empty")),
            "days": days,
            "summary": {
                "days": len(days),
                "ready_days": ready_days,
                "candidates": total_candidates,
                "failed": total_failed,
            },
        }

    def data_ready(self, day_date: str) -> dict:
        """查询某足球日数据是否已准备就绪（只读状态，不触发任何拉取）。"""
        with _file_lock("dm-state", timeout=30):
            st = _load_dm_state()
        entry = (st.get("days") or {}).get(day_date)
        if not entry:
            return {"day": day_date, "ready": False, "reason": "not_prepared"}
        return {"day": day_date, **entry}

    def prepared_days(self) -> list[str]:
        """返回状态文件中已准备的足球日列表（升序）。"""
        with _file_lock("dm-state", timeout=30):
            st = _load_dm_state()
        return sorted((st.get("days") or {}).keys())

    # ═══════════════════════════════════════════
    # Odds（Pinnacle 终盘）
    # ═══════════════════════════════════════════

    def get_odds(self, lota_id: str) -> dict:
        """获取 Pinnacle 终盘赔率（欧赔/亚盘/大小球）"""
        return extract_odds(lota_id, self.get_compact_fet(lota_id))

    # ═══════════════════════════════════════════
    # 关联查询（match → predictions + orders）
    # ═══════════════════════════════════════════

    def get_predictions(self, lota_id: str) -> list[dict]:
        """查询某场比赛的所有预测"""
        path = PREDICTS_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except Exception:
                pass
        return []

    def get_orders(self, lota_id: str) -> list[dict]:
        """查询某场比赛的所有订单"""
        path = ORDERS_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except Exception:
                pass
        return []

    def get_match_context(self, lota_id: str, rich: bool = False) -> dict:
        """
        一键获取比赛全貌: 基础信息 + 赔率 + 预测 + 订单。

        rich=True 时额外回填赛果比分与北单让球线（**仅反思用**：这些字段来自
        赛后缓存，绝不可进入分析/下单 prompt）。默认 False = 线上单狗原行为。

        Returns:
          {
            "lota_id": str,
            "match": dict,         # 比赛基础信息（队名/联赛/时间/比分）
            "score": str,          # 实际比分
            "odds": dict,          # Pinnacle 终盘赔率
            "predictions": list,   # 预测列表
            "orders": list,        # 订单列表
            "tags_summary": str,   # 各 section 的简短摘要
          }
        """
        # 比赛基础信息（从 compact-fet 提取）
        match_info = self._extract_match_info(lota_id, rich=rich)
        score = match_info.get("score", "")

        return {
            "lota_id": lota_id,
            "match": match_info,
            "score": score,
            "odds": self.get_odds(lota_id),
            "predictions": self.get_predictions(lota_id),
            "orders": self.get_orders(lota_id),
            "tags_summary": self._tags_summary(lota_id),
        }

    def _extract_match_info(self, lota_id: str, rich: bool = False) -> dict:
        """从 compact-fet 文本提取比赛基础信息。

        多级回退:
          1. compact-fet 文本解析（最详细）
          2. features 缓存（含 score）
          3. matches 列表缓存（队名/联赛/时间）  ← 新增兜底
        """
        text = self.get_compact_fet_text(lota_id)
        if not text:
            return self._fallback_match_info(lota_id)

        home = away = league = match_time = ""
        for line in text.split("\n")[:5]:
            if "🆚" in line:
                parts = line.split("🆚")
                if len(parts) == 2:
                    home = parts[0].split(":")[-1].strip() if ":" in parts[0] else parts[0].strip()
                    away = parts[1].strip()
            if "联赛类型:" in line:
                league = line.split(":")[1].split("｜")[0].strip() if ":" in line else ""
            if "时间:" in line and ":" in line:
                time_part = line.split(":", 1)[1].strip() if ":" in line else ""
                match_time = time_part.split("｜")[0].strip() if "｜" in time_part else time_part

        # compact-fet 文本可能没有队名（某些比赛格式不同），用 match list 补全
        if not home or not away:
            fallback = self._fallback_match_info(lota_id)
            if not home:
                home = fallback.get("home", "")
            if not away:
                away = fallback.get("away", "")
            if not league:
                league = fallback.get("league", "")
            if not match_time:
                match_time = fallback.get("match_time", "")

        # score from features
        score = ""
        feat = self.get_cached_compact_fet(lota_id)
        if feat:
            score = (feat.get("data") or {}).get("score", "")

        # ── 回放/北单场景的赛果回填（2026-09-13）──
        # 回放用 fet_txt **赛前切片**（pass_6_hours 等）→ 里面没有比分；
        # 而真实赛果在 `data/beidan/<日>.json` / `matches/<日>.json` 的 beidan_info 里。
        # 结果：反思输入出现「比分:? 且没有 goal_line」（用户实测报障）。
        # ⚠️ 仅在 rich=True（北单串关/沙盒反思显式打开）时回填：
        #   1. 线上单狗/分析 prompt 走的 rich=False → 返回的 score 与以前逐字节相同；
        #   2. 回填读的是**赛后**赛果缓存，绝不能进入分析（下单）prompt → 见
        #      tests/test_persona_reflect_split.py 的后视红线断言。
        goal_line = None
        if rich:
            if not score:
                try:
                    bi = (self.get_cached_beidan_results({lota_id}) or {}).get(lota_id) or {}
                    if bi.get("score") not in (None, ""):
                        score = str(bi.get("score"))
                    gl = bi.get("goal_line")
                    if gl not in (None, ""):
                        goal_line = float(gl)
                except Exception:
                    pass
            if goal_line is None:
                try:
                    m = self.get_cached_match(lota_id) or {}
                    gl = ((m.get("beidan_info") or {}).get("goal_line"))
                    if gl not in (None, ""):
                        goal_line = float(gl)
                    if not score and m.get("score") not in (None, ""):
                        score = str(m.get("score"))
                except Exception:
                    pass

        return {
            "home": home,
            "away": away,
            "league": league,
            "match_time": match_time,
            "score": score,
            "goal_line": goal_line,
        }

    def _fallback_match_info(self, lota_id: str) -> dict:
        """三级回退: features 缓存 → matches 列表 → 空 dict"""
        # 1. features 缓存（可能含 score 和 match 子对象）
        feat = self.get_cached_compact_fet(lota_id)
        if feat:
            data = feat.get("data") or {}
            match = feat.get("match") or data.get("match") or {}
            if match:
                return {
                    "home": match.get("home_name", match.get("home", "")),
                    "away": match.get("away_name", match.get("away", "")),
                    "league": match.get("league_name", match.get("league", "")),
                    "match_time": match.get("match_time", ""),
                    "score": data.get("score", feat.get("score", "")),
                }
            return {
                "home": "", "away": "", "league": "", "match_time": "",
                "score": data.get("score", feat.get("score", "")),
            }

        # 2. matches 列表缓存（有完整的队名/联赛/时间）
        match = self.get_cached_match(lota_id)
        if match:
            return {
                "home": match.get("home_name", match.get("home", "")),
                "away": match.get("away_name", match.get("away", "")),
                "league": match.get("league_name", match.get("league", "")),
                "match_time": match.get("match_time", ""),
                "score": match.get("score", ""),
            }

        return {}

    def _tags_summary(self, lota_id: str) -> str:
        """所有 tag section 的简短摘要（用于快速浏览）"""
        sections = self.get_tags(lota_id)
        lines = []
        for slug, text in sorted(sections.items()):
            # 取第一行作为摘要
            first_line = text.split("\n")[0][:120]
            lines.append(f"  [{slug}] {first_line}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

def save_beidan_sp_cache_merge(sp_dates: list[str], sp_map: dict[str, dict]) -> int:
    """把 {lota_id: beidan_info(含开奖)} 合并进 beidan_sp/<date>.json。

    与 save_beidan_sp_cache（整表覆盖）不同：按 lota_id upsert，保留已有场次、
    用传入的开奖字段补全。用于 settle 后把逐腿开奖落盘，跨环境同步不再丢。
    Returns: 实际写入的场次数。
    """
    if not sp_map:
        return 0
    written = 0
    for sp_date in sp_dates or []:
        path = BEIDAN_SP_DIR / f"{sp_date}.json"
        cur: dict[str, dict] = {}
        if path.exists():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    cur = d
            except Exception:
                cur = {}
        changed = False
        for lid, info in sp_map.items():
            if not lid or not isinstance(info, dict):
                continue
            base = dict(cur.get(lid) or {})
            base.update({k: v for k, v in info.items()
                         if k in ("result", "result_des", "spvalue", "score",
                                  "goal_line", "beidan_id", "draw_datetime")
                         and v is not None})
            cur[lid] = base
            changed = True
        if changed:
            _atomic_write_text(path, json.dumps(cur, ensure_ascii=False, indent=2))
            written += len(cur)
    return written


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python data_manager.py <lota_id>")
        print("      python data_manager.py --tags <lota_id>")
        sys.exit(1)

    if sys.argv[1] == "--tags":
        lid = sys.argv[2]
        dm = DataManager()
        sections = dm.get_tags(lid)
        for slug, text in sorted(sections.items()):
            print(f"\n{'='*60}")
            print(f"[{slug}] ({len(text)} chars)")
            print(f"{'='*60}")
            print(text[:500])
    else:
        lid = sys.argv[1]
        dm = DataManager()
        ctx = dm.get_match_context(lid)
        print(json.dumps({
            "lota_id": ctx["lota_id"],
            "match": ctx["match"],
            "score": ctx["score"],
            "odds": ctx["odds"],
            "predictions_count": len(ctx["predictions"]),
            "orders_count": len(ctx["orders"]),
        }, ensure_ascii=False, indent=2))
        print(f"\n--- Tags ---")
        print(ctx["tags_summary"][:2000])
