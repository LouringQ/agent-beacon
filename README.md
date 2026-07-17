<div align="center">

# 🔔 agent-beacon

**AI 在干活,你在摸鱼;危险操作抬腕一点,任务跑完手表喊你——外加一个真正的自动驾驶开关。**

给 **Claude Code** 和 **Codex** 的危险操作接一条到 **Apple Watch / iPhone** 的审批通道,
任务完成自动通知。核心差异化:**Auto-CLI** —— 用 iPhone 的「专注模式」当自动驾驶总闸,
专注开着时全程静默放行,不用来回切配置;外加本地 SQLite 审批日志、网页 Dashboard、
macOS 菜单栏小应用。

</div>

---

## 这是什么

一个 Claude Code / Codex CLI 的 hook 脚本:每次工具调用触发时,先判断风险等级,
危险/敏感操作推一条 Pushcut 通知到你的 Apple Watch,带 ✅ 允许 / ❌ 拒绝 / 🖥️ 终端查看
按钮,点一下,决策秒回 agent。任务跑完(或额度用完、异常终止)也会推一条完成/预警通知。

跟简单的「转发通知」工具不一样的地方:

1. **⌚ 三档风险分级,不是非黑即白** —— `dangerous`(`rm -rf`、强推、提权……)、
   `sensitive`(碰 `.env`/`.ssh`/CI 配置这类敏感路径,但本身不算破坏性操作)、`safe`。
   `sensitive` 可以单独配置要不要升级到手表审批。
2. **🤖 Auto-CLI:iPhone 专注当自动驾驶总闸** —— 在 iPhone 上开一个专注(比如叫
   `Auto-CLI`)+「跨设备共享」,macOS 会同步这个状态;开着的时候,`safe`/`sensitive`
   操作全部静默放行,`dangerous` 操作按你的策略处理(全自动 / 仍推手表 / 直接拒绝)。
   出门喝杯咖啡前开一下,回来关掉,不用碰任何配置文件。
3. **📒 本地 SQLite 审批日志** —— 每次决策(谁、什么操作、什么风险等级、怎么决定的、
   花了多久)都落一条记录,不联网、不上传,纯本地。
4. **📊 网页 Dashboard** —— `python3 beacon_dashboard.py` 起一个本地服务,看审批历史、
   Auto-CLI 实时状态、最近 24 小时统计,零依赖(纯标准库)。
5. **🖥️ macOS 菜单栏小应用** —— 常驻图标,一眼看到 Auto-CLI 是否在自动驾驶、最近几条
   审批记录,一键手动开关、一键打开 Dashboard(唯一需要 `pip install rumps` 的组件,
   hook 主流程不受影响)。

两个 hook 脚本(`beacon_approve.py` / `beacon_done.py`)**只用标准库、零依赖**,
**全程 fail-safe**:配置缺失、网络挂了、超时,一律退回 agent 自己的终端审批,
绝不静默放行、绝不卡死你的任务。

## 前置条件

- **Claude Code** 和/或 **Codex CLI**
- **Python 3**(标准库即可;菜单栏 App 额外需要 `pip install rumps`)
- **[Pushcut](https://www.pushcut.io/)** 账号(动态按钮需要 **Pro**),iPhone **和 Apple Watch 都装上 app**
- 一个 **[ntfy](https://ntfy.sh/)** topic(公共 ntfy.sh 即可,topic 名就是密码,取长随机串)
- 想用 Auto-CLI:iPhone 建一个专注 + 开「跨设备共享」;宿主 app(跑 hook 的那个,比如
  Claude.app)需要「完全磁盘访问」权限才能读到 macOS 的专注状态

## 快速上手

```bash
git clone https://github.com/LouringQ/agent-beacon.git
cd agent-beacon
cp beacon.env.example beacon.env   # 填入 PUSHCUT_KEY / NTFY_TOPIC
python3 beacon_approve.py --doctor  # 自检 + 发一条测试通知
```

自检通过后接线:

- **Claude Code**:把 [`examples/claude/settings.example.json`](./examples/claude/settings.example.json)
  的内容(改成你的绝对路径)合并进 `~/.claude/settings.json`,重启 Claude Code。
- **Codex**:同理合并 [`examples/codex/hooks.example.json`](./examples/codex/hooks.example.json)
  到 `~/.codex/hooks.json`,在 Codex TUI 里跑 `/hooks` 信任这两条 hook。

跑条危险命令(比如 `rm -rf /tmp/some-test-dir`)测试,手表应该会震。

## Auto-CLI:自动驾驶怎么开

1. iPhone「设置 → 专注模式」新建一个专注,名字随意(默认识别 `Auto-CLI`,也可以用系统
   自带的睡眠/游戏/勿扰),打开「跨设备共享」。
2. 给运行 hook 的宿主 app(通常是 Claude.app 或 ChatGPT.app)在 macOS 系统设置里授予
   「完全磁盘访问」权限(用来读 `~/Library/DoNotDisturb/DB`)。
3. 专注一开,`beacon_approve.py` 自动检测到、静默放行(不打扰手表);关掉专注,
   立刻退回正常审批流程。危险操作怎么处理由 `BEACON_AUTO_FOCUS_DANGER` 决定
   (`allow` 全自动 / `notify` 仍推手表 / `deny` 直接拒绝)。
4. 没权限或没设置专注也没关系:菜单栏 App 里有个「手动覆盖」开关,效果等价,
   本地文件驱动,不依赖任何系统权限。

## 网页 Dashboard

```bash
python3 beacon_dashboard.py --port 8787
```

打开 `http://127.0.0.1:8787`,能看到:Auto-CLI 是否在自动驾驶、iPhone 专注实时状态、
最近的审批记录(风险等级 / 目标 / 决策 / 来源)、最近 24 小时统计。只监听 127.0.0.1,
不对外暴露。页面上的「手动覆盖」按钮和菜单栏 App 共用同一个开关。

## macOS 菜单栏小应用

```bash
pip install rumps
python3 beacon_menubar.py
```

常驻菜单栏,Auto-CLI 在跑时图标会变(🟢🔔),下拉菜单看最近 5 条审批、切换手动覆盖、
一键打开 Dashboard。这是唯一需要额外装包的部分——hook 主流程不受影响,不想装 rumps
完全不影响 `beacon_approve.py` / `beacon_done.py` 正常工作。

## 风险分级细节

`beacon_core.classify_risk(text)` 返回 `(level, label)`:

- **dangerous**:`rm -rf`、`sudo`、`git push --force`、`git reset --hard`、`dd`、
  `chmod 777`、`DROP/TRUNCATE TABLE`、`DELETE FROM`、`curl|sh`、`docker rm/prune`、
  `terraform destroy`、`kubectl delete`、`aws s3 rm --recursive`、`crontab -r` 等。
  `BEACON_DANGER_EXTRA` 追加,`BEACON_DANGER_REGEX` 整体替换。
- **sensitive**:命令/路径碰到 `.env`、`.ssh/`、`id_rsa`、`*.pem`、`.aws/credentials`、
  `.npmrc`/`.pypirc`、`.git/config`、`Dockerfile`/`docker-compose.yml`、
  `.github/workflows/`、`*.tf`。`BEACON_ESCALATE_SENSITIVE=0` 可以让这一档不强制上手表。
- **safe**:其余一切。

## 配置参考(节选,完整看 [`beacon.env.example`](./beacon.env.example))

| 变量 | 默认 | 说明 |
|------|------|------|
| `PUSHCUT_KEY` / `NTFY_TOPIC` | — | 必填 |
| `BEACON_DANGER_ONLY` | `1` | `1`=只有 dangerous/sensitive 上手表 |
| `BEACON_NONDANGER_DECISION` | `ask` | safe 操作怎么处理:`ask`/`allow`/`deny` |
| `BEACON_ESCALATE_SENSITIVE` | `1` | sensitive 是否和 dangerous 一样强制上手表 |
| `BEACON_RENOTIFY_INTERVAL` | `120` | 等待期间每隔 N 秒重发,`0`=只发一次 |
| `BEACON_MISSED_ALERT` | `1` | 超时补一条「你错过了」提醒 |
| `AUTO_FOCUS_NAMES` | `Auto-CLI,…` | 触发自动驾驶的专注名/modeIdentifier |
| `BEACON_AUTO_FOCUS_DANGER` | `allow` | 自动驾驶下 dangerous 操作策略 |
| `BEACON_HOME` | `~/.agent-beacon` | SQLite 日志 + 旗标文件存放目录 |

## 安全

- 密钥只从环境变量 / `beacon.env` 读取,不硬编码;`.gitignore` 已排除 `*.env`。
- SQLite 审批日志(`~/.agent-beacon/decisions.db`)可能含命令片段/文件路径,
  **不会**被这个仓库的 `.gitignore` 意外提交(`*.db` 已排除),但你自己 clone 后
  也别手贱 `git add -f` 它。
- `BEACON_NONDANGER_DECISION=allow` / `BEACON_AUTO_FOCUS_DANGER=allow` 等于把放行权
  交给危险正则,启用前想清楚、按需扩充 `BEACON_DANGER_EXTRA`。
- Dashboard 只监听 `127.0.0.1`,不做鉴权——同一台机器上的其他本地进程能读到审批历史,
  多用户共享的机器上请不要长期挂着跑。

## 已知范围(暂不支持)

- Codex 的额度遥测预警(读 rollout 文件里的 `rate_limits`)—— schema 没有公开文档,
  与其猜一个可能错的实现,先不做。Claude 侧的 `StopFailure` 限额提醒正常支持。
- 安卓 / Wear OS(ntfy 原生通知按钮)—— 目前只做了 Pushcut(苹果)一条链路,
  欢迎 PR。

## License

MIT —— 见 [LICENSE](./LICENSE)。
