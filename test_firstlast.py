#!/usr/bin/env python3
"""首尾帧（image + last_frame_image）端到端验证。

思路：纯红图当**首帧**、纯蓝图当**尾帧**，生成视频后抽首末帧比色。
  - 末帧 ≈ 蓝  → 首尾帧生效（last_frame_image 字段名正确、后端透传链路通）
  - 末帧 ≈ 红  → 尾帧被忽略（字段名错或没传上去）

用法：python test_firstlast.py [服务地址]
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3020"
HERE = Path(__file__).parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True)


def make_solid(path: Path, color: str) -> None:
    """用 ffmpeg 生成一张纯色图。"""
    r = run("ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=512x288",
            "-frames:v", "1", str(path))
    if not path.exists():
        raise RuntimeError(f"生成 {color} 图失败: {r.stderr.decode()[-300:]}")


def data_url(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def post_json(path: str, body: dict, timeout: int = 600):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def avg_rgb(png: Path) -> tuple[int, int, int]:
    """缩到 1x1 取平均色。"""
    r = run("ffmpeg", "-y", "-i", str(png), "-vf", "scale=1:1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    b = r.stdout[-3:]
    return (b[0], b[1], b[2]) if len(b) == 3 else (-1, -1, -1)


def near(c: tuple[int, int, int], target: str) -> bool:
    r, g, b = c
    if target == "red":
        return r > 150 and g < 110 and b < 110
    if target == "blue":
        return b > 150 and r < 110 and g < 110
    return False


def main() -> int:
    red, blue = HERE / "_sf_red.png", HERE / "_sf_blue.png"
    make_solid(red, "red")
    make_solid(blue, "blue")
    print(f"已生成纯色图: {red.name} / {blue.name}")

    body = {
        "model": "p-video-2-pro",
        "prompt": "smooth transition from a solid red frame to a solid blue frame",
        "image": data_url(red),          # 首帧
        "last_frame": data_url(blue),    # 尾帧
        "duration": 5,
        "resolution": "480p",
        "wait": True,
    }
    print("提交首尾帧任务（首帧=红，尾帧=蓝）…")
    code, d = post_json("/v1/videos/generations", body)
    print(f"HTTP {code}")
    if code != 200:
        print(json.dumps(d, ensure_ascii=False, indent=2)[:600])
        return 1

    print(f"  状态={d.get('status')} 出口={d.get('exit')}")
    url = d["data"][0]["url"]
    print(f"  成品={url}")

    mp4 = HERE / "_sf_out.mp4"
    with OPENER.open(url, timeout=180) as r:
        mp4.write_bytes(r.read())
    print(f"  已下载 {mp4.stat().st_size / 1024:.1f} KB")

    first, last = HERE / "_sf_first.png", HERE / "_sf_last.png"
    run("ffmpeg", "-y", "-i", str(mp4), "-frames:v", "1", str(first))
    run("ffmpeg", "-y", "-sseof", "-0.4", "-i", str(mp4), "-frames:v", "1", str(last))

    cf, cl = avg_rgb(first), avg_rgb(last)
    print()
    print("=" * 62)
    print(f"  首帧平均色 {cf}   期望 ≈ 红 (255,0,0)   {'✅' if near(cf, 'red') else '❌'}")
    print(f"  末帧平均色 {cl}   期望 ≈ 蓝 (0,0,255)   {'✅' if near(cl, 'blue') else '❌'}")
    print("=" * 62)
    if near(cf, "red") and near(cl, "blue"):
        print("✅ 首尾帧生效：首帧锁红、末帧锁蓝")
        rc = 0
    elif near(cf, "red") and not near(cl, "blue"):
        print("❌ 只有首帧生效，尾帧被忽略 —— last_frame_image 字段名可能不对")
        rc = 1
    else:
        print("⚠ 首帧都没锁住，可能模型不吃纯色图（换更复杂的图重试）")
        rc = 1

    print(f"\n产物保留供人工确认: {first.name} / {last.name} / {mp4.name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
