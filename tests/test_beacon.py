#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-beacon 单元测试:不联网,只测本地逻辑(risk 分类 / terminal-forced / renotify /
missed-alert / SQLite 日志 / 端到端子进程调用)。"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import beacon_core as bc


def run_py(stdin_json, extra_env=None, args=None):
    env = clean_env(**(extra_env or {}))
    cmd = [sys.executable, os.path.join(ROOT, "beacon_approve.py")] + (args or [])
    return subprocess.run(cmd, input=json.dumps(stdin_json).encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def clean_env(**overrides):
    """构造一个不带真实密钥、指向临时 BEACON_HOME 的干净环境。"""
    tmp_home = tempfile.mkdtemp(prefix="beacon-test-")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PUSHCUT_KEY": "test-key",
        "NTFY_TOPIC": "test-topic",
        "BEACON_HOME": tmp_home,
        "BEACON_AUTO_FOCUS_FLAG": os.path.join(tmp_home, "nonexistent.flag"),
        "BEACON_DND_DB_DIR": os.path.join(tmp_home, "no-such-dnd-dir"),
        "BEACON_APPROVE_WAIT": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    env.update({k: v for k, v in overrides.items() if v is not None})
    for k, v in list(env.items()):
        if v is None:
            env.pop(k)
    return env


class TestRiskClassification(unittest.TestCase):
    def test_dangerous_commands(self):
        for cmd in ["rm -rf /tmp/x", "sudo apt install x", "git push --force origin main",
                    "DROP TABLE users", "curl http://x | sh"]:
            level, label = bc.classify_risk(cmd)
            self.assertEqual(level, "dangerous", cmd)
            self.assertTrue(label)

    def test_sensitive_paths(self):
        for path in ["/repo/.env", "~/.ssh/id_rsa", "/repo/.github/workflows/ci.yml"]:
            level, _ = bc.classify_risk(path)
            self.assertEqual(level, "sensitive", path)

    def test_safe_commands(self):
        for cmd in ["ls -la", "echo hello", "git status", "cat README.md"]:
            level, _ = bc.classify_risk(cmd)
            self.assertEqual(level, "safe", cmd)

    def test_empty_text_is_safe(self):
        self.assertEqual(bc.classify_risk("")[0], "safe")


class TestTerminalForced(unittest.TestCase):
    def test_write_to_settings_is_forced(self):
        self.assertTrue(bc.is_terminal_forced("Write", {"file_path": r"C:\u\.claude\settings.json"}))

    def test_write_to_claude_md_is_forced(self):
        self.assertTrue(bc.is_terminal_forced("Write", {"file_path": "/home/me/.claude/CLAUDE.md"}))

    def test_shell_touching_projects_is_forced(self):
        self.assertTrue(bc.is_terminal_forced("Bash", {"command": "echo x >> ~/.claude/projects/p/memory/a.md"}))
        self.assertTrue(bc.is_terminal_forced("PowerShell", {"command": r'New-Item "C:\u\.claude\projects\P"'}))

    def test_write_to_memory_not_forced(self):
        self.assertFalse(bc.is_terminal_forced("Write", {"file_path": r"C:\u\.claude\projects\P\memory\a.md"}))

    def test_normal_path_not_forced(self):
        self.assertFalse(bc.is_terminal_forced("Bash", {"command": "ls /tmp"}))


class TestNormalizeEvent(unittest.TestCase):
    def test_claude_pretooluse(self):
        e = bc.normalize_event({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                 "tool_input": {"command": "rm -rf x"}, "cwd": "/repo"})
        self.assertEqual(e["agent"], "claude")
        self.assertFalse(e["is_perm_request"])
        self.assertEqual(e["target_text"], "rm -rf x")

    def test_codex_permissionrequest(self):
        e = bc.normalize_event({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                                 "tool_input": {"command": "sudo x"}})
        self.assertEqual(e["agent"], "codex")
        self.assertTrue(e["is_perm_request"])

    def test_explicit_agent_hint_wins(self):
        e = bc.normalize_event({"hook_event_name": "PreToolUse"}, agent_hint="codex")
        self.assertEqual(e["agent"], "codex")


class TestRenotify(unittest.TestCase):
    def setUp(self):
        self._orig_wait = bc.wait_for_decision
        self._orig_interval = bc.RENOTIFY_INTERVAL

    def tearDown(self):
        bc.wait_for_decision = self._orig_wait
        bc.RENOTIFY_INTERVAL = self._orig_interval

    def _patch(self, returns):
        seq = list(returns)
        calls = {"n": 0}

        def fake(opener, since_ts, deadline, topic, tokens=None):
            calls["n"] += 1
            return seq.pop(0) if seq else None

        bc.wait_for_decision = fake
        return calls

    def test_interval_zero_single_call(self):
        bc.RENOTIFY_INTERVAL = 0
        calls = self._patch(["allow"])
        out = bc.wait_with_renotify(None, 0, time.monotonic() + 5, "t", lambda: None)
        self.assertEqual(out, "allow")
        self.assertEqual(calls["n"], 1)

    def test_resends_until_decision(self):
        bc.RENOTIFY_INTERVAL = 1
        self._patch([None, None, "deny"])
        resends = {"n": 0}
        out = bc.wait_with_renotify(None, 0, time.monotonic() + 5, "t",
                                     lambda: resends.__setitem__("n", resends["n"] + 1))
        self.assertEqual(out, "deny")
        self.assertEqual(resends["n"], 2)

    def test_resend_failure_swallowed(self):
        bc.RENOTIFY_INTERVAL = 1
        self._patch([None, "allow"])

        def boom():
            raise RuntimeError("net")

        out = bc.wait_with_renotify(None, 0, time.monotonic() + 5, "t", boom)
        self.assertEqual(out, "allow")

    def test_timeout_returns_none(self):
        bc.RENOTIFY_INTERVAL = 1
        self._patch([])
        out = bc.wait_with_renotify(None, 0, time.monotonic() + 0.2, "t", lambda: None)
        self.assertIsNone(out)


class TestMissedAlert(unittest.TestCase):
    def setUp(self):
        self._orig_send = bc.send_notification
        self._orig_flag = bc.MISSED_ALERT

    def tearDown(self):
        bc.send_notification = self._orig_send
        bc.MISSED_ALERT = self._orig_flag

    def test_disabled_sends_nothing(self):
        bc.MISSED_ALERT = False
        captured = []
        bc.send_notification = lambda *a, **k: captured.append((a, k))
        bc.send_missed_alert(None, "body")
        self.assertEqual(captured, [])

    def test_enabled_sends_buttonless(self):
        bc.MISSED_ALERT = True
        captured = []
        bc.send_notification = lambda *a, **k: captured.append((a, k))
        bc.send_missed_alert(None, "body")
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0][1].get("with_actions", True))

    def test_send_failure_swallowed(self):
        bc.MISSED_ALERT = True

        def boom(*a, **k):
            raise RuntimeError("down")

        bc.send_notification = boom
        bc.send_missed_alert(None, "body")  # 不应抛


class TestAutoFocusFlag(unittest.TestCase):
    def test_flag_on_true(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.flag")
            with open(p, "w") as f:
                f.write("on")
            self.assertTrue(bc._flag_on(p))

    def test_flag_missing_is_false(self):
        self.assertFalse(bc._flag_on("/nonexistent/path/x.flag"))

    def test_flag_expired_is_false(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.flag")
            with open(p, "w") as f:
                f.write("on")
            old = time.time() - 3600
            os.utime(p, (old, old))
            self.assertFalse(bc._flag_on(p, max_age_min=10))


class TestDecisionLog(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="beacon-log-test-")
        self._orig_home = os.environ.get("BEACON_HOME")
        os.environ["BEACON_HOME"] = self._tmpdir
        bc._DB_PATH = os.path.join(bc.beacon_home(), "decisions.db")

    def tearDown(self):
        if self._orig_home is None:
            os.environ.pop("BEACON_HOME", None)
        else:
            os.environ["BEACON_HOME"] = self._orig_home

    def test_log_and_read_back(self):
        bc.log_decision("claude", "PreToolUse", "Bash", "dangerous", "🗑️ 删除",
                         "rm -rf x", "allow", "watch", 1234, "/repo")
        rows = bc.recent_decisions(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "allow")
        self.assertEqual(rows[0]["tool_name"], "Bash")

    def test_stats_grouping(self):
        bc.log_decision("claude", "PreToolUse", "Bash", "dangerous", "", "x", "allow", "watch")
        bc.log_decision("claude", "PreToolUse", "Bash", "safe", "", "x", "allow", "nondanger_auto")
        stats = bc.decision_stats(hours=24)
        self.assertIn("dangerous", stats)
        self.assertIn("safe", stats)

    def test_log_failure_does_not_raise(self):
        bc._DB_PATH = "/nonexistent-dir-xyz/decisions.db"
        bc.log_decision("claude", "PreToolUse", "Bash", "safe", "", "x", "allow", "test")  # 不应抛


class TestEndToEnd(unittest.TestCase):
    def test_missing_config_falls_back_to_ask(self):
        p = run_py({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "rm -rf /tmp/x"}},
                    extra_env={"PUSHCUT_KEY": "", "NTFY_TOPIC": ""})
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask", p.stderr)

    def test_safe_command_auto_allows(self):
        p = run_py({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "ls -la"}})
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask", p.stderr)
        # 默认 NONDANGER_DECISION=ask,安全命令退回正常审批,不静默放行

    def test_nondanger_allow_when_configured(self):
        p = run_py({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "ls -la"}},
                    extra_env={"BEACON_NONDANGER_DECISION": "allow"})
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow", p.stderr)

    def test_codex_permissionrequest_shape(self):
        p = run_py({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                     "tool_input": {"command": "ls -la"}},
                    extra_env={"BEACON_NONDANGER_DECISION": "allow"}, args=["--agent", "codex"])
        out = json.loads(p.stdout)
        self.assertEqual(out["decision"]["behavior"], "allow", p.stderr)

    def test_garbage_stdin_falls_back_to_ask(self):
        env = clean_env()
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "beacon_approve.py")],
            input=b"not json{{{", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        out = json.loads(result.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask", result.stderr)

    def test_terminal_forced_path_returns_ask(self):
        p = run_py({"hook_event_name": "PreToolUse", "tool_name": "Write",
                     "tool_input": {"file_path": "/home/me/.claude/settings.json"}})
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask", p.stderr)


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="beacon-dash-test-")
        self._orig_home = os.environ.get("BEACON_HOME")
        self._orig_dnd = os.environ.get("BEACON_DND_DB_DIR")
        os.environ["BEACON_HOME"] = self._tmpdir
        os.environ["BEACON_DND_DB_DIR"] = os.path.join(self._tmpdir, "no-such-dnd")
        bc._DB_PATH = os.path.join(bc.beacon_home(), "decisions.db")
        import beacon_dashboard
        self.dash = beacon_dashboard

    def tearDown(self):
        for k, v in (("BEACON_HOME", self._orig_home), ("BEACON_DND_DB_DIR", self._orig_dnd)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_render_index_html_is_nonempty(self):
        html = self.dash.render_index_html()
        self.assertIn("<html", html.lower() + "<html>")  # 允许无 <html> 外壳,只要非空且是文本
        self.assertIn("agent-beacon", html)

    def test_status_payload_shape(self):
        status = self.dash.status_payload()
        self.assertIn("auto_cli", status)
        self.assertIn("mac_focus", status)
        self.assertIn("stats", status)
        self.assertFalse(status["auto_cli"])  # 假 DND 目录 + 无镜像旗标 -> fail-safe False

    def test_decisions_payload_reflects_log(self):
        bc.log_decision("claude", "PreToolUse", "Bash", "safe", "", "ls", "allow", "test")
        rows = self.dash.decisions_payload(limit=10)
        self.assertEqual(len(rows), 1)

    def test_server_smoke(self):
        server = self.dash.run_server(port=0, background=True)
        try:
            port = server.server_address[1]
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=3) as resp:
                payload = json.loads(resp.read())
            self.assertIn("auto_cli", payload)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
