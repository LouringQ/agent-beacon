#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-beacon: Claude Code / Codex CLI 的 Stop / StopFailure hook。

任务跑完(或因 API 错误终止)推一条无按钮的纯提醒到手表/手机,并在 SQLite 日志里留一条
"task_done" 记录,供 Dashboard 的活动流展示。fire-and-forget:配置缺失/网络失败一律
静默退出,绝不阻塞 agent、绝不触发 Stop 循环。

已知范围:不解析 Codex 的额度遥测(rollout 文件里的 rate_limits)—— 那个 schema 没有
公开文档、容易读错,与其猜一个可能错的实现,不如先不做;仍然覆盖 Claude 的 StopFailure
限额/异常提醒(从 hook 传入的错误文本里提取)。
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beacon_core as bc

AGENT_ICON = {"claude": "🦀", "codex": "🤖"}
AGENT_LABEL = {"claude": "Claude", "codex": "Codex"}

_RESET_RE = re.compile(r"resets?\s+([0-9:apmshoursmin\s]+(?:\([^)]+\))?)", re.IGNORECASE)


def _detect_agent():
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--agent" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--agent="):
            return a.split("=", 1)[1].strip().lower()
    return os.environ.get("BEACON_AGENT", "").strip().lower() or "claude"


def main():
    agent = _detect_agent()
    if agent not in ("claude", "codex"):
        agent = "claude"

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    if not bc.PUSHCUT_KEY or not bc.NTFY_TOPIC:
        return  # 配置缺失,静默退出(fire-and-forget)

    hook_event = str(data.get("hook_event_name") or "Stop")
    icon = AGENT_ICON.get(agent, "🔔")
    label = AGENT_LABEL.get(agent, agent)

    title = "%s %s 任务已完成" % (icon, label)
    text = "%s 已完成当前任务" % label
    outcome = "done"

    if hook_event == "StopFailure":
        error_text = str(
            data.get("error") or data.get("reason") or data.get("message") or ""
        )
        if re.search(r"rate.?limit|quota|额度", error_text, re.IGNORECASE):
            m = _RESET_RE.search(error_text)
            title = "🚦 %s 额度已用完" % label
            text = "重置时间:%s" % m.group(1).strip() if m else "订阅额度已用完"
            outcome = "quota_exhausted"
        else:
            title = "⚠️ %s 任务异常终止" % label
            text = error_text[:120] if error_text else "回合因错误终止"
            outcome = "error"

    cwd = str(data.get("cwd") or os.getcwd())
    if os.environ.get("BEACON_SHOW_CWD", "1").strip() != "0":
        text = text + "\n📁 " + (os.path.basename(cwd.rstrip("/\\")) or cwd)

    opener = bc.make_opener()
    sound = "problem" if outcome != "done" else None
    try:
        bc.send_notification(opener, title, text, with_actions=False, sound=sound, retries=6)
    except Exception:
        pass

    bc.log_decision(agent, hook_event, "", "", "", "", outcome, "done_hook", None, cwd)


if __name__ == "__main__":
    main()
