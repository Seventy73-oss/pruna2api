#!/usr/bin/env python3
"""用 CDP 远程控制 headless Chrome，切换模型并截图对比字段显隐。

为什么需要它：headless 截图**无法交互**，看不到「切换模型后字段显隐」这类
状态。CDP 的 Runtime.evaluate 可以直接调页面里的函数（如 onModelChange），
再 captureScreenshot，就能验证动态交互结果。

用法：
    python cdp_shot.py <url> <输出前缀>
    CHROME_PATH=/path/to/chrome python cdp_shot.py http://127.0.0.1:3020/?tab=gen shot

依赖：pip install websocket-client
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket


def find_chrome() -> str:
    """按常见位置找一个 Chrome/Edge。可用 CHROME_PATH 覆盖。"""
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("chrome", "chromium", "google-chrome", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    raise SystemExit("找不到 Chrome，请设置 CHROME_PATH 环境变量")


CHROME = find_chrome()
PORT = int(os.environ.get("CDP_PORT", "9223"))
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3020/?tab=gen&theme=dark"
PREFIX = sys.argv[2] if len(sys.argv) > 2 else "cdp"


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.id = 0

    def call(self, method: str, params: dict | None = None, timeout: int = 30):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.id:
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    def shot(self, path: Path):
        r = self.call("Page.captureScreenshot", {"format": "png"}, timeout=60)
        path.write_bytes(base64.b64decode(r["data"]))
        return path.stat().st_size


def main() -> int:
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", f"--remote-debugging-port={PORT}",
        # Chrome 111+ 默认拒绝非同源的 WebSocket 连接，必须显式放行
        "--remote-allow-origins=*",
        "--window-size=1500,1150",
        f"--user-data-dir={Path.home()}/AppData/Local/Temp/cr-cdp",
        URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        time.sleep(6)
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=15))
        page = next(t for t in tabs if t.get("type") == "page")
        c = CDP(page["webSocketDebuggerUrl"])
        c.call("Page.enable")
        c.call("Runtime.enable")
        time.sleep(3)   # 等页面 JS 跑完（loadModels 是异步的）

        results = []
        for model, tag in (("p-image", "img"), ("p-video-2", "vid")):
            ok = c.js(f"(function(){{var s=document.querySelector('#f-model');"
                      f"if(!s)return 'no-select';"
                      f"s.value='{model}';"
                      f"if(typeof onModelChange==='function')onModelChange();"
                      f"return s.value;}})()")
            time.sleep(2)
            # 读出各字段的可见性
            vis = c.js(
                "JSON.stringify(Object.fromEntries("
                "['#f-ar','#f-res','#f-dur','#f-mode','#f-ups','#f-seed'].map(function(s){"
                "var e=document.querySelector(s);"
                "if(!e)return [s,'missing'];"
                "var f=e.closest('.field');"
                "return [s, f && f.style.display==='none' ? 'hidden' : 'visible'];"
                "})))"
            )
            out = Path(f"{PREFIX}_{tag}.png")
            size = c.shot(out)
            results.append((model, ok, vis, out.name, size))
            print(f"  {model:14} 切换={ok}  截图={out.name} ({size} B)")
            print(f"    字段可见性: {vis}")

        print()
        for model, ok, vis, name, _ in results:
            d = json.loads(vis) if vis and vis.startswith("{") else {}
            hidden = [k for k, v in d.items() if v == "hidden"]
            shown = [k for k, v in d.items() if v == "visible"]
            print(f"  {model:14} 显示: {shown}")
            print(f"  {'':14} 隐藏: {hidden}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
