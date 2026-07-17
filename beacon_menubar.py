#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-beacon macOS 菜单栏小应用。

常驻菜单栏图标,一眼看到 Auto-CLI 是否在自动驾驶、最近几条审批记录,一键打开本地 Dashboard,
一键手动开关自动驾驶(不用去 iPhone 上翻专注设置)。

依赖 rumps(唯一需要 pip install 的组件——菜单栏 UI 没有纯标准库方案;hook 主流程
beacon_approve.py / beacon_done.py 仍然零依赖,不受影响)。

用法:pip install rumps && python3 beacon_menubar.py
"""

import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beacon_core as bc

try:
    import rumps
except ImportError:
    sys.stderr.write(
        "agent-beacon 菜单栏 App 需要 rumps(hook 主流程不需要,只有这个可选 UI 需要):\n"
        "  pip install rumps\n"
    )
    sys.exit(1)

DASHBOARD_PORT = int(os.environ.get("BEACON_DASHBOARD_PORT", "8787"))


class BeaconMenuBarApp(rumps.App):
    def __init__(self):
        super().__init__("🔔", quit_button="退出")
        self._dashboard_server = None
        self.menu = [
            rumps.MenuItem("Auto-CLI: 检测中…", callback=None),
            rumps.MenuItem("手动覆盖:开启", callback=self.toggle_override),
            None,
            rumps.MenuItem("最近审批", callback=None),
            None,
            rumps.MenuItem("打开 Dashboard", callback=self.open_dashboard),
        ]
        self.timer = rumps.Timer(self.refresh, 5)
        self.timer.start()
        self.refresh(None)

    def refresh(self, _sender):
        auto_on = bc.auto_focus_on()
        mac_focus = bc.mac_focus_active()
        self.title = "🟢🔔" if auto_on else "🔔"
        self.menu["Auto-CLI: 检测中…"].title = (
            "Auto-CLI: %s(专注 %s)" % ("自动驾驶中" if auto_on else "关闭", "开" if mac_focus else "关")
        )

        override_path = os.path.join(bc.beacon_home(), "menubar-override.flag")
        override_on = False
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                override_on = f.read().strip().lower().startswith("on")
        except Exception:
            pass
        self.menu["手动覆盖:开启"].title = "手动覆盖:%s" % ("关闭" if override_on else "开启")

        recent = bc.recent_decisions(limit=5)
        submenu = self.menu["最近审批"]
        for key in list(submenu.keys()) if hasattr(submenu, "keys") else []:
            del submenu[key]
        if not recent:
            submenu.add(rumps.MenuItem("(暂无记录)", callback=None))
        else:
            badge = {"dangerous": "🟥", "sensitive": "🟧", "safe": "🟩"}
            for row in recent:
                text = "%s %s %s" % (
                    badge.get(row.get("risk_level"), "⬜"),
                    row.get("decision", "?"),
                    (row.get("target") or row.get("tool_name") or "")[:30],
                )
                submenu.add(rumps.MenuItem(text, callback=None))

    def toggle_override(self, sender):
        currently_on = sender.title.endswith("关闭")
        bc.set_menubar_override(not currently_on)
        self.refresh(None)

    def open_dashboard(self, _sender):
        if self._dashboard_server is None:
            import beacon_dashboard
            self._dashboard_server = beacon_dashboard.run_server(
                port=DASHBOARD_PORT, background=True
            )
        webbrowser.open("http://127.0.0.1:%d" % DASHBOARD_PORT)


def main():
    BeaconMenuBarApp().run()


if __name__ == "__main__":
    main()
