#!/usr/bin/env python3
"""ui.html 部署前自检 —— 纯静态检查，不联网、不起服务。

改完前端先跑这个，能一次性抓出绝大多数低级错误：
  标签不平衡 / 关键 id 丢失 / id 重复（最隐蔽）/ 页面数不对 / JS 语法错。

用法：
    python check_ui.py            # 检查 ui.html
    python check_ui.py other.html
退出码 0 = 通过，1 = 有问题。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 每个页面必须存在的 id（改 UI 时同步维护）
REQUIRED_IDS = [
    # 侧边栏 / 顶栏
    "dot", "f-health", "f-models", "p-ts", "p-exits", "p-done", "p-run",
    "ph-title", "ph-sub",
    # 五个页面容器
    "page-home", "page-gen", "page-tasks", "page-exits", "page-api",
    # 概览页
    "h-exits", "h-exits-sub", "h-done", "h-fail", "h-fail-sub", "h-run",
    "h-exgrid", "h-recent", "h-quick",
    # 生成页
    "f-model", "f-model-hint", "f-prompt", "f-ar", "f-res", "f-dur",
    "f-mode", "f-ups", "f-seed", "f-async", "btn-run", "result",
    "wrap-img", "wrap-video", "drop", "thumbs", "f-img-hint", "f-img-req",
    # 任务页 / 出口页
    "t-filter", "t-auto", "t-reload", "taskgrid",
    "exlist", "ex-probe", "ex-probeall", "ex-stat", "ex-src", "exdetail",
    "sub-url", "sub-save", "sub-refresh", "sub-info",
    # 界面控制
    "theme-ctl", "zoom-ctl",
    # 其他
    "toast",
]

EXPECTED_PAGES = ["page-home", "page-gen", "page-tasks", "page-exits", "page-api"]


def find_node() -> str | None:
    for c in (
        Path.home() / ".workbuddy/binaries/node/versions",
        Path("C:/Program Files/nodejs"),
    ):
        if c.is_dir():
            for exe in sorted(c.rglob("node.exe"), reverse=True):
                return str(exe)
    return None


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "ui.html")
    if not path.is_file():
        print(f"找不到 {path}")
        return 1
    src = path.read_text(encoding="utf-8")
    fails: list[str] = []
    warns: list[str] = []

    print(f"检查 {path}  ({len(src)} 字符)\n")

    # 1) 标签平衡
    for tag in ("div", "section", "aside", "nav", "main", "table"):
        o = len(re.findall(rf"<{tag}\b", src))
        c = len(re.findall(rf"</{tag}>", src))
        status = "OK  " if o == c else "FAIL"
        if o != c:
            fails.append(f"<{tag}> 不平衡: {o} 开 / {c} 闭")
        print(f"  [{status}] <{tag}>  {o} 开 / {c} 闭")

    # 2) 关键 id
    ids = re.findall(r'id="([^"]+)"', src)
    missing = [i for i in REQUIRED_IDS if i not in ids]
    if missing:
        fails.append(f"缺 id: {missing}")
    print(f"  [{'OK  ' if not missing else 'FAIL'}] 关键 id  {len(REQUIRED_IDS) - len(missing)}/{len(REQUIRED_IDS)}")
    for m in missing:
        print(f"        缺 {m}")

    # 3) id 唯一性 —— 重复 id 会让 querySelector 静默拿错元素，最难查
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        fails.append(f"id 重复: {dup}")
    print(f"  [{'OK  ' if not dup else 'FAIL'}] id 唯一性  {len(ids)} 个 id")
    for d in dup:
        print(f"        重复 {d} × {ids.count(d)}")

    # 4) 页面清单
    pages = re.findall(r'<section class="page[^"]*" id="(page-[a-z]+)"', src)
    if pages != EXPECTED_PAGES:
        fails.append(f"页面清单不符: {pages}")
    print(f"  [{'OK  ' if pages == EXPECTED_PAGES else 'FAIL'}] 页面  {pages}")

    # 5) 默认只应有一个 .page.on
    on = len(re.findall(r'<section class="page on"', src))
    if on != 1:
        (fails if on == 0 else warns).append(f"page on 数量 = {on}（应恰好 1）")
    print(f"  [{'OK  ' if on == 1 else 'WARN'}] 默认显示页  {on} 个")

    # 6) 导航项与页面一一对应
    navs = re.findall(r'data-page="([a-z]+)"', src)
    want = [p.replace("page-", "") for p in EXPECTED_PAGES]
    if sorted(navs) != sorted(want):
        fails.append(f"导航项 {navs} 与页面 {want} 不匹配")
    print(f"  [{'OK  ' if sorted(navs) == sorted(want) else 'FAIL'}] 导航项  {navs}")

    # 7) JS 语法 —— 页面上可能有多段 <script>（如 head 里的防闪烁主题预设），全都得查
    blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
    if not blocks:
        fails.append("没找到 <script> 块")
        print("  [FAIL] JS  未找到 script")
    else:
        node = find_node()
        if not node:
            warns.append("找不到 node，跳过 JS 语法检查")
            print("  [WARN] JS  找不到 node，跳过")
        else:
            total = sum(len(b) for b in blocks)
            bad = 0
            tmp = Path("_ui_syntax_check.js")
            for i, body in enumerate(blocks, 1):
                if not body.strip():
                    continue
                tmp.write_text(body, encoding="utf-8")
                try:
                    r = subprocess.run([node, "--check", str(tmp)],
                                       capture_output=True, text=True)
                    if r.returncode != 0:
                        bad += 1
                        print(f"        第 {i} 段语法错误：{(r.stderr or '')[:300]}")
                except OSError as e:
                    warns.append(f"node 执行失败: {e}")
                    break
            tmp.unlink(missing_ok=True)
            if bad:
                fails.append(f"{bad} 段 JS 语法错误")
            print(f"  [{'OK  ' if not bad else 'FAIL'}] JS 语法  "
                  f"{len(blocks)} 段 / 共 {total} 字符")

    print()
    for w in warns:
        print(f"  ⚠ {w}")
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\n❌ 未通过（{len(fails)} 项）")
        return 1
    print("✅ 全部通过，可以部署")
    return 0


if __name__ == "__main__":
    sys.exit(main())
