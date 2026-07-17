#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-beacon 共享核心库:被 beacon_approve.py / beacon_done.py / beacon_dashboard.py 共同 import。

三层风险分级(比二元的「危险/不危险」更细):
  SAFE       - 正常操作,自动放行(自动驾驶模式下)
  SENSITIVE  - 碰敏感路径(.env / .ssh / CI 配置……)但不算破坏性操作,可单独配置是否升级上手表
  DANGEROUS  - 破坏性操作(rm -rf、强推、提权……),始终推手表(除非 Auto-CLI 危险策略覆盖)

设计原则:只用 Python 3 标准库,零依赖;所有 IO 失败都 fail-safe(退回人工审批,不静默放行、
不阻塞 agent)。这是 hook 场景下唯一安全的默认值 —— 配置缺失或网络挂了,永远不能变成
「反正放行就好」。
"""

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------- 兜底配置文件 beacon.env ----------
# Claude Code 会把 settings.json 的 env 注入 hook 子进程;Codex 不会把 config.toml 里的
# env 传给 hook。为了让脚本在任何宿主下都拿得到配置,读脚本同目录的 beacon.env
# (KEY=VALUE 每行一条,# 开头是注释),只填补【缺失】的环境变量,真实环境变量永远优先。
def load_env_file():
    path = os.environ.get("BEACON_ENV_FILE", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "beacon.env"
    )
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


load_env_file()


def beacon_home():
    """本地状态目录(SQLite 日志、菜单栏覆盖开关都放这里)。可用 BEACON_HOME 覆盖。"""
    path = os.environ.get("BEACON_HOME", "").strip() or os.path.expanduser("~/.agent-beacon")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


# ================= 风险策略(policy) =================

_DANGER_PATTERNS = [
    (r"\bremove-item\b.*-recurse.*-force", "🗑️ 删除文件夹"),
    (r"\bdel\b\s+/[sf]", "🗑️ 删除文件"),
    (r"\brm\s+-\S*r\S*f|\brm\s+-\S*f\S*r", "🗑️ 删除文件/目录(rm -rf)"),
    (r"\brm\s+-", "🗑️ 删除文件/目录"),
    (r"\bformat\b\s+[a-z]:", "💽 格式化磁盘"),
    (r"\bmkfs\b", "💽 格式化文件系统"),
    (r"\bdd\b.*\bif=", "💽 dd 写盘"),
    (r"\bof=/dev/", "💽 写入磁盘设备"),
    (r">\s*/dev/sd", "💽 写入磁盘设备"),
    (r"\bsudo\b", "🔑 sudo 提权执行"),
    (r"\bgit\s+push\b.*(--force|-f\b|\s\+)", "⚠️ git 强制推送"),
    (r"\bgit\s+reset\s+--hard\b", "↩️ git 丢弃改动(reset --hard)"),
    (r"\bgit\s+clean\s+-[a-z]*f", "🧹 git 清理未跟踪文件"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "🔌 关机/重启"),
    (r"\b(kill|pkill|killall)\b\s+-9", "🛑 强制结束进程"),
    (r"\bchmod\s+(-r\s+|.*\b777\b)", "🔓 修改权限(chmod)"),
    (r"\bchown\s+-r\b", "👤 修改属主(chown -R)"),
    (r":\(\)\s*\{", "💥 疑似 fork 炸弹"),
    (r"\b(drop|truncate)\s+(table|database)\b", "🗄️ 删表/清库"),
    (r"\bdelete\s+from\b", "🗄️ 删除数据(DELETE FROM)"),
    (r"(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "📥 下载并执行脚本"),
    (r"\bnpm\s+publish\b", "📦 发布 npm 包"),
    (r"\bdocker\b.*\b(rm|prune|down)\b", "🐳 删除 docker 资源"),
    (r"\bterraform\s+destroy\b", "💥 terraform 销毁资源"),
    (r"\bkubectl\s+delete\b", "☸️ kubectl 删除资源"),
    (r"\baws\s+s3\s+rm\b.*--recursive", "☁️ 批量删除 S3 对象"),
    (r"\b(az|gcloud)\b.*\bdelete\b", "☁️ 云资源删除"),
    (r"\bcrontab\s+-r\b", "⏰ 清空 crontab"),
    (r"\bhistory\s+-c\b", "🕵️ 清空 shell 历史"),
]
_DANGER_RE = [(re.compile(p, re.IGNORECASE), label) for p, label in _DANGER_PATTERNS]

_SENSITIVE_PATTERNS = [
    (r"\.env(\.\w+)?(\s|$|[\"'])", "🔐 .env 环境变量文件"),
    (r"\bcredentials\b", "🔐 凭据文件"),
    (r"\.aws[\\/]credentials", "🔐 AWS 凭据"),
    (r"\.ssh[\\/]", "🔑 SSH 目录"),
    (r"id_rsa|id_ed25519", "🔑 SSH 私钥"),
    (r"\.pem\b|\.key\b", "🔑 证书/密钥文件"),
    (r"\.npmrc|\.pypirc", "🔐 包管理器凭据"),
    (r"\.git[\\/]config|\.git-credentials", "🔐 git 凭据/配置"),
    (r"dockerfile|docker-compose\.ya?ml", "🐳 容器编排文件"),
    (r"\.github[\\/]workflows", "⚙️ CI/CD 流水线"),
    (r"\bterraform\b.*\.tf\b", "🏗️ 基础设施即代码"),
]
_SENSITIVE_RE = [(re.compile(p, re.IGNORECASE), label) for p, label in _SENSITIVE_PATTERNS]


def custom_danger_re():
    """WATCH_DANGER_REGEX 整体替换 / WATCH_DANGER_EXTRA 追加(换行分隔),沿用同名习惯。"""
    override = os.environ.get("BEACON_DANGER_REGEX", "").strip()
    extra = os.environ.get("BEACON_DANGER_EXTRA", "").strip()
    patterns = list(_DANGER_RE)
    if override:
        patterns = []
        for line in override.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                patterns.append((re.compile(line, re.IGNORECASE), "⚠️ 自定义危险规则"))
            except re.error:
                pass
    if extra:
        for line in extra.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                patterns.append((re.compile(line, re.IGNORECASE), "⚠️ 自定义危险规则"))
            except re.error:
                pass
    return patterns


def classify_risk(text):
    """返回 (level, label):level 是 'dangerous' / 'sensitive' / 'safe'。"""
    if not text:
        return "safe", ""
    for rx, label in custom_danger_re():
        if rx.search(text):
            return "dangerous", label
    for rx, label in _SENSITIVE_RE:
        if rx.search(text):
            return "sensitive", label
    return "safe", ""


# ================= Claude / Codex 适配层 =================

def normalize_event(data, agent_hint=""):
    """把 Claude Code / Codex 的 hook JSON 差异抹平成统一的事件字典:
    {tool_name, target_text, cwd, hook_event_name, is_perm_request}

    target_text = 用来做风险分类/展示的文本(shell 命令 或 文件路径),两边字段名不同
    (Claude 用 tool_input.command / file_path,Codex 的 PermissionRequest 结构类似但
    事件名不同),这里统一抽取,上层代码不用再关心是哪个 agent。
    """
    if not isinstance(data, dict):
        data = {}
    hook_event = str(data.get("hook_event_name") or "").strip()
    tool_name = str(data.get("tool_name") or "").strip()
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = str(data.get("cwd") or os.getcwd())

    target_text = (
        str(tool_input.get("command") or "")
        or str(tool_input.get("file_path") or "")
        or str(tool_input.get("notebook_path") or "")
        or str(tool_input.get("path") or "")
    )

    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "target_text": target_text,
        "cwd": cwd,
        "hook_event_name": hook_event,
        "is_perm_request": hook_event == "PermissionRequest",
        "agent": agent_hint or ("codex" if hook_event == "PermissionRequest" else "claude"),
    }


# ================= 强制走终端的路径(hook 拦不住,只提醒不审批) =================

TERMINAL_FORCED_PATHS = [
    p.strip().lower()
    for p in os.environ.get(
        "BEACON_TERMINAL_FORCED_PATHS",
        ".claude\\settings.json,.claude/settings.json,"
        ".claude\\settings.local.json,.claude/settings.local.json,"
        ".claude\\hooks,.claude/hooks,"
        ".claude\\CLAUDE.md,.claude/CLAUDE.md",
    ).split(",")
    if p.strip()
]
_SHELL_ONLY_FORCED_PATHS = (".claude/projects", ".claude\\projects")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def is_terminal_forced(tool_name, tool_input):
    if not TERMINAL_FORCED_PATHS or not isinstance(tool_input, dict):
        return False
    if tool_name in _WRITE_TOOLS:
        text = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        paths = TERMINAL_FORCED_PATHS
    elif tool_name in ("Bash", "PowerShell"):
        text = str(tool_input.get("command") or "")
        paths = TERMINAL_FORCED_PATHS + list(_SHELL_ONLY_FORCED_PATHS)
    else:
        return False
    text = text.lower()
    return bool(text) and any(p in text for p in paths)


# ================= 目标 basename(通知正文用) =================

_PATH_TOKEN_RE = re.compile(r"""[A-Za-z]:[\\/][^\s"']*|[^\s"']*[\\/][^\s"']*""")


def short_target(text, max_len=60):
    """从命令/路径里抠出目标 basename,给通知正文用(手表屏幕小,不堆全路径)。"""
    if not text:
        return ""
    tokens = _PATH_TOKEN_RE.findall(text)
    if tokens:
        names = []
        for t in tokens[:3]:
            base = re.split(r"[\\/]", t.rstrip("\"'"))[-1]
            if base and base not in names:
                names.append(base)
        joined = "、".join(names)
        return joined[:max_len]
    return text.strip()[:max_len]


# ================= SQLite 审批日志 =================

_DB_PATH = os.path.join(beacon_home(), "decisions.db")


def _db():
    conn = sqlite3.connect(_DB_PATH, timeout=3)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            agent TEXT,
            event TEXT,
            tool_name TEXT,
            risk_level TEXT,
            risk_label TEXT,
            target TEXT,
            decision TEXT,
            source TEXT,
            latency_ms INTEGER,
            cwd TEXT
        )"""
    )
    return conn


def log_decision(agent, event, tool_name, risk_level, risk_label, target,
                  decision, source, latency_ms=None, cwd=""):
    """写一条审批日志。任何失败都吞掉 —— 日志是锦上添花,绝不能因为它搞挂主流程。"""
    try:
        conn = _db()
        with conn:
            conn.execute(
                "INSERT INTO decisions "
                "(ts, agent, event, tool_name, risk_level, risk_label, target, "
                " decision, source, latency_ms, cwd) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), agent, event, tool_name, risk_level, risk_label,
                 target, decision, source, latency_ms, cwd),
            )
        conn.close()
    except Exception:
        pass


def recent_decisions(limit=50):
    try:
        conn = _db()
        cur = conn.execute(
            "SELECT ts, agent, event, tool_name, risk_level, risk_label, target, "
            "decision, source, latency_ms, cwd FROM decisions ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def decision_stats(hours=24):
    """最近 N 小时的统计:按 risk_level / decision 分组计数。"""
    try:
        conn = _db()
        since = time.time() - hours * 3600
        cur = conn.execute(
            "SELECT risk_level, decision, COUNT(*) FROM decisions WHERE ts >= ? "
            "GROUP BY risk_level, decision",
            (since,),
        )
        rows = cur.fetchall()
        conn.close()
        out = {}
        for level, decision, n in rows:
            out.setdefault(level or "unknown", {})[decision or "unknown"] = n
        return out
    except Exception:
        return {}


# ================= Auto-CLI:iPhone 专注当自动驾驶总闸(用户原创功能) =================
# 玩法:iPhone 上开一个专注(默认名 Auto-CLI)+ 打开「跨设备共享」,macOS 会把状态同步到
# 本地 DoNotDisturb 数据库;hook 直接读它,专注开着 = 自动放行(不打扰手表)。
# 需要宿主 app(跑 hook 的那个,比如 Claude.app)有「完全磁盘访问」权限,否则读不到,
# fail-safe 返回 False(退回正常审批,不会误放行)。
# 另外支持一个本地镜像旗标文件(给没有 FDA 权限的宿主兜底,也是菜单栏 App 手动开关走的通道)。

_AUTO_FOCUS_DEFAULT_TRIGGERS = (
    "Auto-CLI,"
    "com.apple.donotdisturb.mode.heartfill,"
    "com.apple.focus.gaming,"
    "com.apple.sleep.sleep-mode,"
    "com.apple.donotdisturb.mode.default"
)
AUTO_FOCUS_TRIGGERS = {
    t.strip()
    for t in os.environ.get("BEACON_AUTO_FOCUS_NAMES", _AUTO_FOCUS_DEFAULT_TRIGGERS).split(",")
    if t.strip()
}

def _dnd_db_dir():
    # 调用时动态读取(不是模块级常量),这样测试/运行期改 BEACON_DND_DB_DIR 立即生效。
    return os.environ.get("BEACON_DND_DB_DIR", "").strip() or os.path.expanduser(
        "~/Library/DoNotDisturb/DB"
    )


def mac_focus_active():
    """读 macOS 本地 DoNotDisturb 数据库,判断触发列表里的专注是否正在开启。
    没权限 / 文件不存在 / 解析失败 -> False(fail-safe)。
    """
    try:
        dnd_dir = _dnd_db_dir()
        assertions_path = os.path.join(dnd_dir, "Assertions.json")
        with open(assertions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        active_ids = set()
        for rec in data.get("data", [{}])[0].get("storeAssertionRecords", []) or []:
            mode_id = (
                (rec.get("assertionDetails") or {}).get("assertionDetailsModeIdentifier") or ""
            )
            if mode_id:
                active_ids.add(mode_id)
        if active_ids & AUTO_FOCUS_TRIGGERS:
            return True
        # modeIdentifier 匹配不到时,尝试用 ModeConfigurations.json 把 id 换成显示名再比一次
        cfg_path = os.path.join(dnd_dir, "ModeConfigurations.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            id_to_name = {}
            for mode_id, meta in (cfg.get("data", [{}])[0].get("modeConfigurations") or {}).items():
                name = ((meta.get("mode") or {}).get("name") or {}).get("data") or ""
                if name:
                    id_to_name[mode_id] = name
            names_active = {id_to_name.get(i, "") for i in active_ids}
            if names_active & AUTO_FOCUS_TRIGGERS:
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _flag_on(path, max_age_min=0):
    if not path:
        return False
    try:
        if max_age_min > 0:
            age_min = (time.time() - os.path.getmtime(path)) / 60.0
            if age_min > max_age_min:
                return False
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip().lower()
        return content.startswith("on")
    except Exception:
        return False


def auto_focus_on():
    """自动驾驶总闸:mac 专注 OR 本地镜像/覆盖旗标(任一为 True 即自动放行)。"""
    mirror_flag = os.environ.get("BEACON_AUTO_FOCUS_FLAG", "").strip() or os.path.join(
        beacon_home(), "focus-mirror.flag"
    )
    try:
        max_age = int(os.environ.get("BEACON_AUTO_FOCUS_MAX_AGE_MIN", "10"))
    except ValueError:
        max_age = 10
    override_flag = os.path.join(beacon_home(), "menubar-override.flag")
    return (
        mac_focus_active()
        or _flag_on(mirror_flag, max_age)
        or _flag_on(override_flag, 0)
    )


def set_menubar_override(on):
    """菜单栏 App 的手动开关写这个文件;auto_focus_on() 会一并读取。"""
    path = os.path.join(beacon_home(), "menubar-override.flag")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("on" if on else "off")
        return True
    except Exception:
        return False


# ================= Pushcut 传输 =================

PUSHCUT_KEY = os.environ.get("PUSHCUT_KEY", "").strip()
PUSHCUT_NOTIF = os.environ.get("PUSHCUT_NOTIF", "agent-beacon").strip() or "agent-beacon"
PUSHCUT_DEVICES = [d.strip() for d in os.environ.get("PUSHCUT_DEVICES", "").split(",") if d.strip()]
PUSHCUT_TIME_SENSITIVE = os.environ.get("PUSHCUT_TIME_SENSITIVE", "1").strip() != "0"
NTFY_BASE = (os.environ.get("NTFY_BASE", "").strip() or "https://ntfy.sh/").rstrip("/") + "/"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
PROXY = (
    os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
).strip()

try:
    PUSHCUT_RETRIES = max(1, int(os.environ.get("PUSHCUT_RETRIES", "10")))
except ValueError:
    PUSHCUT_RETRIES = 10
try:
    PUSHCUT_TIMEOUT = max(3, int(os.environ.get("PUSHCUT_TIMEOUT", "6")))
except ValueError:
    PUSHCUT_TIMEOUT = 6

PUSHCUT_URL = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(PUSHCUT_NOTIF, safe="")

# 风险等级 -> 卡片视觉(色块 emoji 当色条,Pushcut 推送不支持自定义色块 UI,这是能做到的
# 最接近「一眼看出风险等级」的效果)。
RISK_BADGE = {"dangerous": "🟥", "sensitive": "🟧", "safe": "🟩"}
RISK_NAME = {"dangerous": "高危", "sensitive": "敏感", "safe": "安全"}


def make_opener():
    if PROXY:
        handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def make_reply_topic():
    base = NTFY_TOPIC or "agentbeacon_" + os.urandom(6).hex()
    if os.environ.get("BEACON_UNIQUE_TOPIC", "1").strip() != "0":
        return base + "_" + os.urandom(4).hex()
    return base


def _pushcut_actions(reply_topic, buttons):
    actions = []
    for label, msg in buttons:
        url = NTFY_BASE + urllib.parse.quote(reply_topic, safe="") + "/publish?message=" + urllib.parse.quote(msg)
        if NTFY_TOKEN:
            url += "&access_token=" + urllib.parse.quote(NTFY_TOKEN)
        actions.append({"name": label, "input": "", "url": url, "keepNotification": False})
    return actions


def default_buttons():
    buttons = [("✅ 允许", "allow"), ("❌ 拒绝", "deny")]
    if os.environ.get("BEACON_TERMINAL_BUTTON", "1").strip() != "0":
        buttons.append(("🖥️ 终端查看", "term"))
    return buttons


def _deliver(opener, url, body, headers, retries, timeout):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with opener.open(req, timeout=timeout) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < retries - 1:
            time.sleep(0.3)
    if last is not None:
        raise last


def send_notification(opener, title, text, with_actions=True, retries=None,
                       reply_topic=None, buttons=None, sound=None):
    btns = buttons if buttons is not None else (default_buttons() if with_actions else None)
    payload = {"title": title, "text": text}
    if btns:
        payload["actions"] = _pushcut_actions(reply_topic or NTFY_TOPIC, btns)
    if PUSHCUT_DEVICES:
        payload["devices"] = PUSHCUT_DEVICES
    if sound and sound.lower() != "none":
        payload["sound"] = sound
    elif sound is None:
        payload["sound"] = os.environ.get("PUSHCUT_SOUND", "default")
    if PUSHCUT_TIME_SENSITIVE:
        payload["isTimeSensitive"] = True
    _deliver(
        opener, PUSHCUT_URL, json.dumps(payload).encode("utf-8"),
        {"API-Key": PUSHCUT_KEY, "Content-Type": "application/json"},
        retries or PUSHCUT_RETRIES, PUSHCUT_TIMEOUT,
    )


def wait_for_decision(opener, since_ts, deadline, topic, tokens=None):
    """轮询 ntfy stream 读回执,到 deadline 没读到返回 None。"""
    url = (
        NTFY_BASE + urllib.parse.quote(topic or NTFY_TOPIC, safe="")
        + "/json?since=" + str(since_ts)
    )
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url)
            if NTFY_TOKEN:
                req.add_header("Authorization", "Bearer " + NTFY_TOKEN)
            resp = opener.open(req, timeout=8)
        except Exception:
            time.sleep(1)
            continue
        try:
            while time.monotonic() < deadline:
                try:
                    raw = resp.readline()
                except Exception:
                    break
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if obj.get("event") != "message":
                    continue
                msg = (obj.get("message") or "").strip().lower()
                if tokens is not None:
                    if msg in tokens:
                        return msg
                    continue
                if msg in ("allow", "approve", "yes", "ok"):
                    return "allow"
                if msg in ("deny", "block", "no"):
                    return "deny"
                if msg == "term":
                    return "term"
        finally:
            try:
                resp.close()
            except Exception:
                pass
    return None


try:
    RENOTIFY_INTERVAL = int(float(os.environ.get("BEACON_RENOTIFY_INTERVAL", "120")))
except ValueError:
    RENOTIFY_INTERVAL = 120
MISSED_ALERT = os.environ.get("BEACON_MISSED_ALERT", "1").strip() != "0"
MISSED_TITLE = os.environ.get("BEACON_MISSED_TITLE", "⏰ 你错过了待处理").strip()


def wait_with_renotify(opener, since_ts, deadline, topic, resend_fn):
    if RENOTIFY_INTERVAL <= 0:
        return wait_for_decision(opener, since_ts, deadline, topic)
    while True:
        now = time.monotonic()
        if now >= deadline:
            return None
        segment_deadline = min(deadline, now + RENOTIFY_INTERVAL)
        result = wait_for_decision(opener, since_ts, segment_deadline, topic)
        if result is not None:
            return result
        if segment_deadline >= deadline:
            return None
        try:
            resend_fn()
        except Exception:
            pass


def send_missed_alert(opener, body):
    if not MISSED_ALERT:
        return
    try:
        send_notification(opener, MISSED_TITLE, body, with_actions=False)
    except Exception:
        pass
