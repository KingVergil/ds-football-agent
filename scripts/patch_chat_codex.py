#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chat-Codex 本地补丁一键重打脚本。

在 `npm i -g chat-codex` 升级/重装后执行，恢复以下 4 处本地增强：
  1. feishu-feature-flags.js   → 开启飞书群聊接收总开关
  2. bridge/formatters.js      → 群消息前缀带上 open_id（身份不可伪造）
  3. bridge/bridge.js          → 群聊 /stop 仅限超级管理员
  4. channels/feishu/feishu-adapter.js → BRIDGE_SEND_FEISHU_CARD 协议（发飞书卡片）

幂等：已打过的补丁会自动跳过。用法：
    python3 scripts/patch_chat_codex.py
"""

import pathlib
import shutil
import subprocess
import sys


def pkg_dir() -> pathlib.Path:
    """定位全局安装的 chat-codex 包目录。"""
    root = subprocess.run(
        ["npm", "root", "-g"], capture_output=True, text=True, check=True
    ).stdout.strip()
    candidate = pathlib.Path(root) / "chat-codex"
    if not candidate.exists():
        raise SystemExit(f"未找到 chat-codex 包目录: {candidate}")
    return candidate


def patch_file(path: pathlib.Path, old: str, new: str, label: str) -> bool:
    s = path.read_text(encoding="utf-8")
    if new in s:
        print(f"  · {label}: 已是最新，跳过")
        return False
    if old not in s:
        raise SystemExit(f"  ✗ {label}: 未找到原始内容，请人工检查 {path}")
    path.write_text(s.replace(old, new), encoding="utf-8")
    print(f"  ✓ {label}: 已补丁")
    return True


def append_text(path: pathlib.Path, text: str, label: str) -> bool:
    s = path.read_text(encoding="utf-8")
    if text.strip() in s:
        print(f"  · {label}: 已是最新，跳过")
        return False
    path.write_text(s.rstrip() + "\n" + text, encoding="utf-8")
    print(f"  ✓ {label}: 已补丁")
    return True


def main() -> int:
    root = pkg_dir()
    dist = root / "dist" / "src"
    print(f"Chat-Codex 包目录: {root}")
    print(f"安装版本: {(root / 'package.json').read_text(encoding='utf-8').split('\"version\":')[1].split(',')[0].strip()}")

    changed = False

    # ── 1. 飞书群聊总开关 ──
    flags = dist / "channels" / "feishu" / "feishu-feature-flags.js"
    changed |= patch_file(
        flags,
        "export const FEISHU_GROUP_RECEIVE_PUBLICLY_ENABLED = false;",
        "export const FEISHU_GROUP_RECEIVE_PUBLICLY_ENABLED = true;",
        "飞书群聊总开关",
    )

    # ── 2. 群消息前缀带 open_id ──
    formatters = dist / "bridge" / "formatters.js"
    old_fn = """function formatGroupSpeakerForPrompt(message) {
    return normalizeGroupSpeakerLabel(message.sender.displayName)
        ?? normalizeGroupSpeakerLabel(message.sender.id)
        ?? "群成员";
}"""
    new_fn = """function formatGroupSpeakerForPrompt(message) {
    const name = normalizeGroupSpeakerLabel(message.sender.displayName);
    const id = normalizeGroupSpeakerLabel(message.sender.id);
    if (!name && !id)
        return "群成员";
    if (name && id)
        return `${name}(${id})`;
    return name ?? id;
}"""
    changed |= patch_file(formatters, old_fn, new_fn, "群消息 open_id 前缀")

    # ── 3. 群聊 /stop 仅限超级管理员 ──
    bridge = dist / "bridge" / "bridge.js"
    old_stop = """                stop: (message, target) => handleStopCommand({
                    state: this.state,
                    codex: this.codex,
                    approvals: this.approvals,
                    pendingMedia: this.pendingMedia,
                    delivery: this.delivery,
                    routeQueue: this.routeQueue,
                    routeSteering: this.routeSteering,
                    clearPendingInput: (routeKey) => this.pendingInput.clearRoute(routeKey),
                }, message, target),"""
    new_stop = """                stop: (message, target) => this.handleStopCommand(message, target),"""
    changed |= patch_file(bridge, old_stop, new_stop, "/stop 超级管理员限制")

    method = """    async handleStopCommand(message, target) {
        if (isFeishuGroupMessage(message) && this.state.isRouteTrusted(message.routeKey)) {
            const role = this.groupAccess.roleForSender(message.routeKey, message.sender.id);
            if (role !== "super_admin") {
                await this.delivery.sendText(target, "只有群超级管理员可以停止当前任务。");
                return;
            }
        }
        await handleStopCommand({
            state: this.state,
            codex: this.codex,
            approvals: this.approvals,
            pendingMedia: this.pendingMedia,
            delivery: this.delivery,
            routeQueue: this.routeQueue,
            routeSteering: this.routeSteering,
            clearPendingInput: (routeKey) => this.pendingInput.clearRoute(routeKey),
        }, message, target);
    }
"""
    anchor = "    async waitForIdle() {"
    s = bridge.read_text(encoding="utf-8")
    if "async handleStopCommand(message, target)" in s:
        print("  · /stop 处理方法: 已是最新，跳过")
    else:
        if s.count(anchor) != 1:
            raise SystemExit("  ✗ /stop 处理方法: 未找到插入锚点")
        bridge.write_text(s.replace(anchor, method + anchor, 1), encoding="utf-8")
        print("  ✓ /stop 处理方法: 已补丁")
        changed = True

    # ── 4. 飞书卡片协议 ──
    adapter = dist / "channels" / "feishu" / "feishu-adapter.js"
    old_import = 'import { AppType, Client, Domain, EventDispatcher, LoggerLevel, WSClient, } from "@larksuiteoapi/node-sdk";'
    new_import = old_import + '\nimport { readFileSync } from "node:fs";'
    changed |= patch_file(adapter, old_import, new_import, "adapter node:fs import")

    old_send = """    async sendText(target, text, options) {
        return this.sendFeishuMessage(target, "post", buildFeishuPostContent(text), options);
    }"""
    new_send = """    async sendText(target, text, options) {
        const cardPath = feishuCardPathFromText(text);
        if (cardPath) {
            try {
                const card = JSON.parse(readFileSync(cardPath, "utf8"));
                if (card && typeof card === "object" && !Array.isArray(card)) {
                    await this.sendFeishuMessage(target, "interactive", JSON.stringify(card), options);
                    return;
                }
            }
            catch (error) {
                // 卡片读取/解析失败时退回普通文本（去掉协议行）
            }
        }
        return this.sendFeishuMessage(target, "post", buildFeishuPostContent(textWithoutFeishuCardRef(text)), options);
    }"""
    changed |= patch_file(adapter, old_send, new_send, "adapter 卡片发送")

    helpers = """
const FEISHU_CARD_REF_PATTERN = /^\\s*BRIDGE_SEND_FEISHU_CARD:\\s*(.+?)\\s*$/gim;
export function feishuCardPathFromText(text) {
    if (!text)
        return undefined;
    const match = text.match(FEISHU_CARD_REF_PATTERN);
    if (!match)
        return undefined;
    return match[0].replace(/^\\s*BRIDGE_SEND_FEISHU_CARD:\\s*/i, "").trim();
}
export function textWithoutFeishuCardRef(text) {
    if (!text)
        return "";
    return text.replace(FEISHU_CARD_REF_PATTERN, "").trim();
}
"""
    changed |= append_text(adapter, helpers, "adapter 卡片协议辅助函数")

    # ── 语法校验 ──
    for path in (flags, formatters, bridge, adapter):
        subprocess.run(["node", "--check", str(path)], check=True)
    print("✓ 全部语法校验通过")
    print("完成。" if changed else "无需变更（补丁都已就位）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
