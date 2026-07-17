#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-beacon 本地 Dashboard:纯标准库 http.server,零依赖。

展示:Auto-CLI 实时状态(mac 专注 / 镜像旗标 / 菜单栏手动覆盖)、最近审批记录、
按风险等级/决策的简单统计。只监听 127.0.0.1,不对外暴露。

用法:python3 beacon_dashboard.py [--port 8787]
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beacon_core as bc

_PAGE_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>agent-beacon Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 900px; margin: 2rem auto; padding: 0 1rem;
         background: Canvas; color: CanvasText; }
  h1 { font-size: 1.3rem; display: flex; align-items: center; gap: .5rem; }
  .status-row { display: flex; gap: .75rem; flex-wrap: wrap; margin: 1rem 0 1.5rem; }
  .pill { padding: .3rem .7rem; border-radius: 999px; font-size: .85rem;
          border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); }
  .pill.on { background: color-mix(in srgb, #22c55e 20%, transparent); border-color: #22c55e; }
  .pill.off { opacity: .55; }
  table { width: 100%; border-collapse: collapse; font-size: .88rem; }
  th, td { text-align: left; padding: .45rem .5rem;
           border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }
  th { opacity: .6; font-weight: 600; }
  .badge { display: inline-block; }
  .stats { display: flex; gap: 1.5rem; margin: 1rem 0; font-size: .9rem; opacity: .85; }
  #override-btn { padding: .35rem .8rem; border-radius: 8px; border: 1px solid #888;
                   background: transparent; color: inherit; cursor: pointer; font-size: .85rem; }
</style>
<h1>🔔 agent-beacon</h1>
<div class="status-row" id="status-row">加载中…</div>
<div class="stats" id="stats"></div>
<table>
  <thead><tr><th>时间</th><th>Agent</th><th>风险</th><th>目标</th><th>决策</th><th>来源</th></tr></thead>
  <tbody id="rows"><tr><td colspan="6">加载中…</td></tr></tbody>
</table>
<script>
async function refresh() {
  const [status, decisions] = await Promise.all([
    fetch('/api/status').then(r => r.json()),
    fetch('/api/decisions').then(r => r.json()),
  ]);
  const sr = document.getElementById('status-row');
  sr.innerHTML = `
    <span class="pill ${status.auto_cli ? 'on' : 'off'}">Auto-CLI: ${status.auto_cli ? '开(自动驾驶)' : '关'}</span>
    <span class="pill ${status.mac_focus ? 'on' : 'off'}">iPhone 专注: ${status.mac_focus ? '开' : '关'}</span>
    <button id="override-btn">${status.override === true ? '关闭手动覆盖' : status.override === false ? '开启手动覆盖' : '手动覆盖(切换)'}</button>
  `;
  document.getElementById('override-btn').onclick = async () => {
    await fetch('/api/override', {method: 'POST', body: JSON.stringify({on: !(status.override === true)})});
    refresh();
  };
  const badge = {dangerous: '🟥', sensitive: '🟧', safe: '🟩'};
  document.getElementById('rows').innerHTML = decisions.length ? decisions.map(d => `
    <tr>
      <td>${new Date(d.ts * 1000).toLocaleTimeString()}</td>
      <td>${d.agent || ''}</td>
      <td><span class="badge">${badge[d.risk_level] || ''}</span> ${d.risk_label || d.risk_level || ''}</td>
      <td>${(d.target || '').slice(0, 40)}</td>
      <td>${d.decision || ''}</td>
      <td>${d.source || ''}</td>
    </tr>`).join('') : '<tr><td colspan="6">暂无记录</td></tr>';
  const s = document.getElementById('stats');
  const parts = [];
  for (const [level, byDecision] of Object.entries(status.stats || {})) {
    const total = Object.values(byDecision).reduce((a, b) => a + b, 0);
    parts.push(`${level}: ${total}`);
  }
  s.textContent = '最近 24h — ' + (parts.join(' · ') || '暂无数据');
}
refresh();
setInterval(refresh, 3000);
</script>
"""


def render_index_html():
    return _PAGE_TEMPLATE


def status_payload():
    override_path = os.path.join(bc.beacon_home(), "menubar-override.flag")
    override = None
    try:
        with open(override_path, "r", encoding="utf-8") as f:
            override = f.read().strip().lower().startswith("on")
    except Exception:
        override = None
    return {
        "auto_cli": bc.auto_focus_on(),
        "mac_focus": bc.mac_focus_active(),
        "override": override,
        "stats": bc.decision_stats(hours=24),
    }


def decisions_payload(limit=50):
    return bc.recent_decisions(limit=limit)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 别刷终端

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send_html(render_index_html())
        elif self.path.startswith("/api/status"):
            self._send_json(status_payload())
        elif self.path.startswith("/api/decisions"):
            self._send_json(decisions_payload())
        else:
            self._send_json({"error": "not found"}, code=404)

    def do_POST(self):
        if self.path.startswith("/api/override"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            bc.set_menubar_override(bool(body.get("on")))
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, code=404)


def run_server(port=8787, host="127.0.0.1", background=False):
    server = ThreadingHTTPServer((host, port), Handler)
    if background:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server
    print("agent-beacon dashboard: http://%s:%d" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return server


def main():
    argv = sys.argv[1:]
    port = 8787
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    run_server(port=port)


if __name__ == "__main__":
    main()
