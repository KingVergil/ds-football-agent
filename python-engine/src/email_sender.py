"""邮件发送工具。

支持通过 mail_cfg 配置不同邮箱服务商，也兼容环境变量（QQ 邮箱）。
mail_cfg 格式:
  {
    "host": "smtp.163.com",
    "port": 25,
    "user": "you@example.com",
    "password": "授权码或密码",
    "use_ssl": false,
    "from_addr": "you@example.com",      # 可选，默认同 user
    "to_addrs": ["a@qq.com", "b@qq.com"], # 可选，默认读 QQ_EMAIL_RECIPIENTS_FILE 或自己
    "valid_from": "2026-01-01 00:00:00",  # 可选，生效开始时间（超出窗口则跳过发送）
    "valid_until": "2027-12-31 23:59:59", # 可选，生效截止时间
  }

凭证一律走环境变量，不硬编码：
  - QQ 邮箱：QQ_EMAIL_ADDR / QQ_EMAIL_AUTH_CODE（默认回退路径）
  - 163 邮箱（mail_cfg="163"）：EMAIL_163_ADDR / EMAIL_163_AUTH_CODE
"""

import os
import sys
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

# ── 内置 mail_cfg ──

BUILTIN_CFG = {
    "163": {
        "host": "smtp.163.com",
        "port": 25,
        "use_ssl": False,
        "valid_from": "2026-01-01 00:00:00",
        "valid_until": "2027-12-31 23:59:59",
    },
}

DEFAULT_RECIPIENTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "email_recipients.txt",
)


def _get_cfg(mail_cfg: dict | str | None = None) -> dict | None:
    """解析 mail_cfg。可以是 dict、内置名称（如 '163'）、或 None（回退到 QQ 环境变量）。"""
    if mail_cfg is None:
        # 回退到 QQ 环境变量模式
        addr = os.environ.get("QQ_EMAIL_ADDR", "")
        if addr:
            return {
                "host": "smtp.qq.com",
                "port": 465,
                "user": addr,
                "password": os.environ.get("QQ_EMAIL_AUTH_CODE", ""),
                "use_ssl": True,
            }
        return None

    if isinstance(mail_cfg, str):
        base = BUILTIN_CFG.get(mail_cfg)
        if base is None:
            print(f"[email] 未知内置配置: {mail_cfg}", file=sys.stderr)
            return None
        cfg = dict(base)  # 浅拷贝
        if mail_cfg == "163":
            # 163 凭证从环境变量读取（邮箱地址 + SMTP 授权码），避免硬编码进源码
            addr = os.environ.get("EMAIL_163_ADDR", "")
            auth = os.environ.get("EMAIL_163_AUTH_CODE", "")
            if not addr or not auth:
                print("[email] 内置 '163' 需要环境变量 EMAIL_163_ADDR / EMAIL_163_AUTH_CODE", file=sys.stderr)
                return None
            cfg["user"] = addr
            cfg["password"] = auth
        return cfg

    return dict(mail_cfg)


def _resolve_recipients(cfg: dict, agent_name: str = "") -> list[str]:
    """解析收件人列表，支持按行登记有效时间窗口和 agent 过滤。

    文件格式: 邮箱[, 生效开始时间, 失效时间, agent过滤]
    agent过滤: 逗号分隔的 agent 名，* 或空 = 全部
    时间格式: YYYY-MM-DD HH:MM:SS，可省略一个或两个都省略。
    """
    # 1. cfg 里直接指定的（测试用，忽略 agent 过滤）
    if cfg.get("to_addrs"):
        return cfg["to_addrs"]

    # 2. 文件列表
    file_path = os.environ.get("QQ_EMAIL_RECIPIENTS_FILE", DEFAULT_RECIPIENTS_FILE)
    if os.path.exists(file_path):
        now = datetime.now()
        recipients = []
        for line in open(file_path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = [p.strip() for p in line.split(",")]
            email = parts[0]
            valid_from = parts[1] if len(parts) > 1 and parts[1] else None
            valid_until = parts[2] if len(parts) > 2 and parts[2] else None
            # agent 过滤: 第 4 列起全部合并（兼容 agent 名中含逗号或空列导致切分过多）
            agent_filter = ",".join(p.strip() for p in parts[3:] if p.strip()) if len(parts) > 3 else ""

            # agent 过滤: 指定了 agent 列 → 只发给匹配的
            if agent_filter and agent_name:
                allowed = {a.strip() for a in agent_filter.split(",") if a.strip()}
                if "*" not in allowed and agent_name not in allowed:
                    continue

            # 检查时间窗口
            if valid_from:
                try:
                    if now < datetime.strptime(valid_from, "%Y-%m-%d %H:%M:%S"):
                        print(f"[email] SKIP {email}: 尚未到生效时间 ({valid_from})", file=sys.stderr)
                        continue
                except ValueError:
                    print(f"[email] WARN: {email} valid_from 格式错误: {valid_from}，跳过时间检查", file=sys.stderr)

            if valid_until:
                try:
                    if now > datetime.strptime(valid_until, "%Y-%m-%d %H:%M:%S"):
                        print(f"[email] SKIP {email}: 已过有效时间 ({valid_until})", file=sys.stderr)
                        continue
                except ValueError:
                    print(f"[email] WARN: {email} valid_until 格式错误: {valid_until}，跳过时间检查", file=sys.stderr)

            recipients.append(email)

        if recipients:
            return recipients

    # 3. QQ_EMAIL_TO 单收件人
    single = os.environ.get("QQ_EMAIL_TO", "")
    if single:
        return [single]

    # 4. 回退到发件人自己
    return [cfg.get("from_addr", cfg.get("user", ""))]


def _is_within_valid_window(cfg: dict) -> bool:
    """检查当前时间是否在配置的有效时间窗口内。

    如果 cfg 中未设置 valid_from / valid_until，默认视为始终有效。
    """
    now = datetime.now()
    valid_from = cfg.get("valid_from")
    valid_until = cfg.get("valid_until")

    if valid_from:
        try:
            t_from = datetime.strptime(valid_from, "%Y-%m-%d %H:%M:%S")
            if now < t_from:
                print(f"[email] SKIP: 尚未到生效时间 (valid_from={valid_from})", file=sys.stderr)
                return False
        except ValueError:
            print(f"[email] WARN: valid_from 格式错误: {valid_from}，跳过时间检查", file=sys.stderr)

    if valid_until:
        try:
            t_until = datetime.strptime(valid_until, "%Y-%m-%d %H:%M:%S")
            if now > t_until:
                print(f"[email] SKIP: 已过有效时间 (valid_until={valid_until})", file=sys.stderr)
                return False
        except ValueError:
            print(f"[email] WARN: valid_until 格式错误: {valid_until}，跳过时间检查", file=sys.stderr)

    return True


def send_email(subject: str, body: str, mail_cfg: dict | str | None = None, *, is_html: bool = False, agent_name: str = "") -> bool:
    """发送邮件。mail_cfg=None 则尝试 QQ 环境变量。agent_name 用于收件人 agent 过滤。"""
    cfg = _get_cfg(mail_cfg)
    if cfg is None:
        print("[email] ERROR: 无邮件配置。请传入 mail_cfg 或设置 QQ_EMAIL_ADDR", file=sys.stderr)
        return False

    # ── 有效时间窗口检查 ──
    if not _is_within_valid_window(cfg):
        return False

    host = cfg["host"]
    port = cfg["port"]
    user = cfg["user"]
    password = cfg["password"]
    use_ssl = cfg.get("use_ssl", port == 465)
    from_addr = cfg.get("from_addr", user)
    to_addrs = _resolve_recipients(cfg, agent_name=agent_name)

    if not to_addrs:
        print("[email] ERROR: 无收件人", file=sys.stderr)
        return False

    msg = MIMEText(body, "html" if is_html else "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(user, password)
                s.send_message(msg)
        print(f"[email] 已发送 → {len(to_addrs)} 位收件人: {', '.join(to_addrs)}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("[email] ERROR: SMTP 认证失败，请检查 user/password", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[email] ERROR: {e}", file=sys.stderr)
        return False


# ── 向后兼容别名 ──
send_qq_email = send_email  # 旧调用仍可用
