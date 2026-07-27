"""邮件发送工具。

支持通过 mail_cfg 配置不同邮箱服务商，也兼容环境变量（QQ 邮箱）。
mail_cfg 格式:
  {
    "host": "smtp.163.com",
    "port": 25,
    "user": "mybotlg022@163.com",
    "password": "授权码或密码",
    "use_ssl": false,
    "from_addr": "mybotlg022@163.com",   # 可选，默认同 user
    "to_addrs": ["a@qq.com", "b@qq.com"], # 可选，默认读 QQ_EMAIL_RECIPIENTS_FILE 或自己
    "valid_from": "2026-01-01 00:00:00",  # 可选，生效开始时间（超出窗口则跳过发送）
    "valid_until": "2027-12-31 23:59:59", # 可选，生效截止时间
  }
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
        "user": "mybotlg022@163.com",
        "password": "TLBKOKKQVGNRBPJU",
        "use_ssl": False,
        "valid_from": "2026-01-01 00:00:00",
        "valid_until": "2027-12-31 23:59:59",
    },
}

DEFAULT_RECIPIENTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lota_data", "email_recipients.txt",
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
        cfg = BUILTIN_CFG.get(mail_cfg)
        if cfg is None:
            print(f"[email] 未知内置配置: {mail_cfg}", file=sys.stderr)
            return None
        return dict(cfg)  # 浅拷贝

    return dict(mail_cfg)


def _resolve_recipients(cfg: dict) -> list[str]:
    """解析收件人列表，支持按行登记有效时间窗口。

    文件格式: 邮箱[, 生效开始时间, 失效时间]
    时间格式: YYYY-MM-DD HH:MM:SS，可省略一个或两个都省略。
    """
    # 1. cfg 里直接指定的
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


def send_email(subject: str, body: str, mail_cfg: dict | str | None = None, *, is_html: bool = False) -> bool:
    """发送邮件。mail_cfg=None 则尝试 QQ 环境变量。"""
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
    to_addrs = _resolve_recipients(cfg)

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
