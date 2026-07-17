#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-beacon: Claude Code / Codex CLI 的 PreToolUse / PermissionRequest hook。

危险操作(或触发的敏感路径)推送到 Pushcut(iPhone/Apple Watch),带 ✅/❌/🖥️ 按钮;
Auto-CLI 专注开着时自动放行,不打扰手表。每次决策写本地 SQLite 日志,供 Dashboard /
菜单栏 App 读取。fail-safe:任何配置缺失、网络故障都退回 agent 自己的终端审批。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beacon_core as bc

APPROVE_WAIT = int(float(os.environ.get("BEACON_APPROVE_WAIT", "240") or 240))
TIMEOUT_DECISION = os.environ.get("BEACON_APPROVE_TIMEOUT_DECISION", "ask").strip().lower()
if TIMEOUT_DECISION not in ("allow", "deny", "ask"):
    TIMEOUT_DECISION = "ask"

DANGER_ONLY = os.environ.get("BEACON_DANGER_ONLY", "1").strip() == "1"
NONDANGER_DECISION = os.environ.get("BEACON_NONDANGER_DECISION", "ask").strip().lower()
ESCALATE_SENSITIVE = os.environ.get("BEACON_ESCALATE_SENSITIVE", "1").strip() != "0"

AUTO_FOCUS_DANGER = os.environ.get("BEACON_AUTO_FOCUS_DANGER", "allow").strip().lower()
if AUTO_FOCUS_DANGER not in ("allow", "notify", "deny"):
    AUTO_FOCUS_DANGER = "allow"

AGENT_ICON = {"claude": "🦀", "codex": "🤖"}
AGENT_LABEL = {"claude": "Claude", "codex": "Codex"}


def emit(event, decision, reason, updated_input=None):
    if event["is_perm_request"]:
        behavior = {"allow": "allow", "deny": "deny"}.get(decision, "no_decision")
        out = {"decision": {"behavior": behavior}}
        if behavior == "deny":
            out["decision"]["message"] = reason
    else:
        pd = {"allow": "allow", "deny": "deny"}.get(decision, "ask")
        hook_out = {"hookEventName": "PreToolUse", "permissionDecision": pd,
                    "permissionDecisionReason": reason}
        if updated_input is not None:
            hook_out["updatedInput"] = updated_input
        out = {"hookSpecificOutput": hook_out}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.exit(0)


def card(event, risk_level, risk_label, target):
    icon = AGENT_ICON.get(event["agent"], "🔔")
    badge = bc.RISK_BADGE.get(risk_level, "")
    title = "%s %s %s待批准" % (badge, icon, AGENT_LABEL.get(event["agent"], event["agent"]))
    parts = []
    if risk_label:
        parts.append(risk_label)
    if target:
        parts.append(bc.short_target(target))
    if os.environ.get("BEACON_SHOW_CWD", "1").strip() != "0":
        cwd_name = os.path.basename(event["cwd"].rstrip("/\\")) or event["cwd"]
        parts.append("📁 " + cwd_name)
    text = "\n".join(parts) if parts else "(无详情)"
    return title, text


def _say(msg, err=False):
    stream = sys.stderr if err else sys.stdout
    try:
        stream.write(msg + "\n")
    except Exception:
        enc = getattr(stream, "encoding", None) or "utf-8"
        stream.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")


def run_doctor():
    ok = True
    _say("agent-beacon --doctor")
    _say("=" * 40)

    if not bc.PUSHCUT_KEY or bc.PUSHCUT_KEY.startswith("REPLACE_WITH"):
        _say("✗ PUSHCUT_KEY 未配置", err=True)
        ok = False
    else:
        _say("✓ PUSHCUT_KEY 已配置(%s...)" % bc.PUSHCUT_KEY[:6])

    if not bc.NTFY_TOPIC or bc.NTFY_TOPIC.startswith("REPLACE_WITH"):
        _say("✗ NTFY_TOPIC 未配置", err=True)
        ok = False
    else:
        _say("✓ NTFY_TOPIC 已配置")

    opener = bc.make_opener()

    if bc.PUSHCUT_KEY and not bc.PUSHCUT_KEY.startswith("REPLACE_WITH"):
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.pushcut.io/v1/devices",
                headers={"API-Key": bc.PUSHCUT_KEY},
            )
            with opener.open(req, timeout=8) as resp:
                devices = json.loads(resp.read())
            names = [d.get("name") for d in devices] if isinstance(devices, list) else []
            _say("✓ Pushcut key 有效,设备:%s" % (", ".join(names) or "(无)"))
            if bc.PUSHCUT_DEVICES:
                missing = [d for d in bc.PUSHCUT_DEVICES if d not in names]
                if missing:
                    _say("✗ PUSHCUT_DEVICES 里有账号没有的设备名:%s" % missing, err=True)
                    ok = False
        except Exception as e:
            _say("✗ Pushcut 连接失败:%s" % e, err=True)
            ok = False

    _say("")
    _say("Auto-CLI 状态:mac 专注=%s / 自动驾驶总闸=%s" % (bc.mac_focus_active(), bc.auto_focus_on()))
    _say("SQLite 日志:%s" % bc._DB_PATH)

    if ok and bc.PUSHCUT_KEY and bc.NTFY_TOPIC:
        _say("")
        _say("发送一条测试通知…")
        try:
            bc.send_notification(opener, "🔔 agent-beacon 自检", "配置正常,能收到这条就算通过。",
                                  with_actions=False, retries=3)
            _say("✓ 测试通知已发送,去手机/手表看看")
        except Exception as e:
            _say("✗ 测试通知发送失败:%s" % e, err=True)
            ok = False

    _say("")
    _say("结果:%s" % ("全部通过 ✓" if ok else "有问题,看上面的 ✗"))
    sys.exit(0 if ok else 1)


def main():
    argv = sys.argv[1:]
    if "--doctor" in argv:
        run_doctor()
        return
    agent_hint = ""
    for i, a in enumerate(argv):
        if a == "--agent" and i + 1 < len(argv):
            agent_hint = argv[i + 1].strip().lower()
        elif a.startswith("--agent="):
            agent_hint = a.split("=", 1)[1].strip().lower()
    if not agent_hint:
        agent_hint = os.environ.get("BEACON_AGENT", "").strip().lower()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    event = bc.normalize_event(data, agent_hint)

    if not bc.PUSHCUT_KEY or not bc.NTFY_TOPIC:
        emit(event, "ask", "agent-beacon: 配置缺失(PUSHCUT_KEY/NTFY_TOPIC),退回正常审批。")
        return

    if event["hook_event_name"] not in ("PreToolUse", "PermissionRequest"):
        emit(event, "ask", "agent-beacon: 非受理事件,退回正常审批。")
        return

    tool_name, tool_input = event["tool_name"], event["tool_input"]

    # 会被 Claude Code 强制弹终端的路径(hook 拦不住)-> 只提醒、不做手表审批
    if not event["is_perm_request"] and bc.is_terminal_forced(tool_name, tool_input):
        opener = bc.make_opener()
        try:
            bc.send_notification(
                opener, "🖥️ 去终端确认", "%s 正在改 agent 配置,需要你在终端确认。" % AGENT_LABEL.get(event["agent"], ""),
                with_actions=False,
            )
        except Exception:
            pass
        emit(event, "ask", "agent-beacon: 该路径会被强制弹终端,已推送提醒,退回终端审批。")
        return

    level, label = bc.classify_risk(event["target_text"])

    # Auto-CLI 专注开着 -> 自动驾驶,不打扰手表(危险操作按 AUTO_FOCUS_DANGER 策略)
    if bc.auto_focus_on():
        if level == "dangerous" and AUTO_FOCUS_DANGER != "allow":
            if AUTO_FOCUS_DANGER == "deny":
                bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                                 level, label, event["target_text"], "deny", "auto_focus_danger")
                emit(event, "deny", "agent-beacon: Auto-CLI 自动模式下危险操作按策略拒绝。")
                return
            # notify:危险操作仍然走下面的手表审批流程(不在此处提前返回)
        else:
            bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                             level, label, event["target_text"], "allow", "auto_focus")
            emit(event, "allow", "agent-beacon: Auto-CLI 自动模式,静默放行(未打扰手表)。")
            return

    # danger-only:非危险(且不升级敏感路径,或没命中敏感)的操作走本地策略,不上手表
    escalate = level == "dangerous" or (level == "sensitive" and ESCALATE_SENSITIVE)
    if DANGER_ONLY and not escalate:
        decision = NONDANGER_DECISION
        bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                         level, label, event["target_text"], decision, "nondanger_auto")
        emit(event, decision, "agent-beacon: 非危险操作,按配置返回 %s(未打扰手表)。" % decision)
        return

    # ---- 走手表审批 ----
    opener = bc.make_opener()
    reply_topic = bc.make_reply_topic()
    t0 = int(time.time())
    t0_mono = time.monotonic()
    deadline = t0_mono + APPROVE_WAIT

    title, text = card(event, level, label, event["target_text"])
    try:
        bc.send_notification(opener, title, text, reply_topic=reply_topic)
    except Exception as e:
        bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                         level, label, event["target_text"], "ask", "send_failed")
        emit(event, "ask", "agent-beacon: 推送失败(%s),退回正常审批。" % type(e).__name__)
        return

    try:
        decision = bc.wait_with_renotify(
            opener, t0, deadline, reply_topic,
            lambda: bc.send_notification(opener, title, text, reply_topic=reply_topic),
        )
    except Exception:
        decision = None

    latency_ms = int((time.monotonic() - t0_mono) * 1000)

    if decision == "allow":
        bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                         level, label, event["target_text"], "allow", "watch", latency_ms)
        emit(event, "allow", "agent-beacon: 已在手表上批准。")
    elif decision == "deny":
        bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                         level, label, event["target_text"], "deny", "watch", latency_ms)
        emit(event, "deny", "agent-beacon: 已在手表上拒绝。")
    elif decision == "term":
        bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                         level, label, event["target_text"], "ask", "watch_term", latency_ms)
        emit(event, "ask", "agent-beacon: 已选择「终端查看」,退回终端审批。")
    else:
        bc.send_missed_alert(opener, text)
        bc.log_decision(event["agent"], event["hook_event_name"], tool_name,
                         level, label, event["target_text"], TIMEOUT_DECISION, "timeout", latency_ms)
        emit(event, TIMEOUT_DECISION,
             "agent-beacon: %ss 内无回应,按超时策略返回 %s。" % (APPROVE_WAIT, TIMEOUT_DECISION))


if __name__ == "__main__":
    main()
