"""
pruna2api —— Pruna Playground 反代服务（OpenAI 风格）

部署在飞牛 OS NAS 上，把 playground.pruna.ai 的图片/视频生成包装成标准 API，
并自动处理出口 IP 轮换（每个出口 IP 在 Pruna 侧是独立配额）。

接口：
  POST /v1/videos/generations   —— 提交视频生成（也兼容图片模型）
  POST /v1/images/generations   —— 提交图片生成（OpenAI 风格别名）
  GET  /v1/tasks/{task_id}      —— 查任务状态
  GET  /v1/tasks                —— 列任务
  GET  /v1/models               —— 列模型
  GET  /v1/exits                —— 列出口池与冷却状态
  GET  /files/{name}            —— 下载成品（转存后的本地文件）
  GET  /health
  GET  /                        —— 简易面板

设计要点：
  * 同步阻塞式提交 + 后台线程轮询（Pruna 视频任务本身就是异步的）
  * 配额 429 自动切换出口并重试，全程对调用方透明
  * 成品下载后落 NAS 本地盘，返回本地 URL
  * 任务状态 SQLite 落盘，重启不丢
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from pruna_client import (
    ALL_MODELS,
    DEFAULT_QUOTA,
    IMAGE_MODELS,
    MIHOMO_API,
    MIHOMO_GROUP,
    MIHOMO_PROXY,
    MODEL_QUOTA,
    VIDEO_MODELS,
    DeterministicError,
    Exit,
    ExitPool,
    PrunaClient,
    QuotaExhausted,
    TaskFailedAfterSubmit,
    fetch_model_status,
    is_model_disabled,
    load_image_bytes,
    load_subscription_exits,
    quota_of,
)

# ---------------------------------------------------------------- 配置

DATA_DIR = os.environ.get("PRUNA_DATA_DIR", "/opt/pruna2api/data")
DB_PATH = os.path.join(DATA_DIR, "tasks.db")
MEDIA_DIR = os.environ.get("PRUNA_MEDIA_DIR", "/opt/pruna2api/media")
API_KEY = os.environ.get("PRUNA_API_KEY", "")          # 空 = 不校验
PUBLIC_BASE = os.environ.get("PRUNA_PUBLIC_BASE", "")  # 空 = 按请求 Host 拼
POLL_INTERVAL = int(os.environ.get("PRUNA_POLL_INTERVAL", "5"))
MAX_WORKERS = int(os.environ.get("PRUNA_MAX_WORKERS", "4"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("pruna2api")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

# ---------------------------------------------------------------- DB

DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    model         TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'video',
    status        TEXT NOT NULL DEFAULT 'queued',
    prompt        TEXT,
    params_json   TEXT,
    n_images      INTEGER DEFAULT 0,
    exit_name     TEXT,
    egress_ip     TEXT,
    remote_url    TEXT,
    local_file    TEXT,
    local_url     TEXT,
    error         TEXT,
    job_id        TEXT,
    attempts      INTEGER DEFAULT 0,
    created_at    REAL,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def db_init():
    with db() as c:
        c.executescript(DDL)


def task_save(task_id: str, **kw):
    if not kw:
        return
    kw["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*kw.values(), task_id))


def task_get(task_id: str) -> dict | None:
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def task_create(task_id: str, model: str, kind: str, prompt: str, params: dict, n_images: int):
    now = time.time()
    with db() as c:
        c.execute(
            """INSERT INTO tasks (id, model, kind, status, prompt, params_json, n_images,
                                  attempts, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,0,?,?)""",
            (task_id, model, kind, "queued", prompt, json.dumps(params, ensure_ascii=False),
             n_images, now, now),
        )


# ---------------------------------------------------------------- 出口池 / 客户端

pool = ExitPool()
client = PrunaClient(pool=pool)

# 提交锁：Pruna 侧的节点切换是全局的（改的是 clash selector），
# 并发切换会互相打架 —— 提交阶段串行化，生成/下载阶段放开。
submit_lock = threading.Lock()


# ---------------------------------------------------------------- 请求模型


class VideoGenRequest(BaseModel):
    model: str = Field(default="p-video-2-pro", description="模型 id")
    prompt: str | None = Field(default=None)
    # 参考图：URL / 本地路径 / dataURL / 裸 base64，可多张
    image: str | None = Field(default=None, description="单张参考图（URL/base64/路径）")
    images: list[str] | None = Field(default=None, description="多张参考图")
    video: str | None = Field(default=None, description="视频转视频的源视频")
    # 通用参数（透传给 Pruna）
    aspect_ratio: str | None = None
    resolution: str | None = None
    duration: float | None = None
    mode: str | None = None
    prompt_upsampler: str | None = None
    target_fps: int | None = None
    seed: int | None = None
    last_frame: str | None = None
    # 控制
    wait: bool = Field(default=True, description="true=同步等到完成；false=立即返回 task_id")


class ImageGenRequest(BaseModel):
    model: str = Field(default="p-image")
    prompt: str | None = None
    image: str | None = None
    images: list[str] | None = None
    aspect_ratio: str | None = None
    seed: int | None = None
    target: int | None = None          # upscale 用
    garments: list[str] | None = None  # try-on 用
    turbo: bool | None = None
    # ---- OpenAI 标准参数（让官方 SDK / 现成客户端能直接接）----
    size: str | None = Field(default=None, description='OpenAI 尺寸，如 "1024x1024"、"1792x1024"')
    n: int = Field(default=1, ge=1, le=4, description="生成张数（OpenAI n；上限 4，每张各扣一次配额）")
    response_format: str = Field(default="url", description='"url" 或 "b64_json"')
    wait: bool = True


# ---- OpenAI size → Pruna 参数映射 -----------------------------------------

# Pruna 只认这几档宽高比
_PRUNE_RATIOS = [(16 / 9, "16:9"), (3 / 2, "3:2"), (4 / 3, "4:3"), (1.0, "1:1"),
                 (3 / 4, "3:4"), (2 / 3, "2:3"), (9 / 16, "9:16")]


def ratio_from_size(size: str | None) -> str | None:
    """把 OpenAI 的 "宽x高" 转成 Pruna 的 aspect_ratio。

    精确比例（如 1792x1024 = 7:4）不在 Pruna 支持列表里时，按最接近的档位归类。
    只映射比例、不猜分辨率 —— 分辨率交给模型默认值，避免猜错档位报 400。
    """
    m = re.match(r"^\s*(\d+)\s*[xX*×✕]\s*(\d+)\s*$", size or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    g = math.gcd(w, h)
    a, b = w // g, h // g
    exact = {(16, 9): "16:9", (9, 16): "9:16", (4, 3): "4:3",
             (3, 4): "3:4", (3, 2): "3:2", (2, 3): "2:3", (1, 1): "1:1"}
    if (a, b) in exact:
        return exact[(a, b)]
    r = w / h
    return min(_PRUNE_RATIOS, key=lambda x: abs(x[0] - r))[1]


# OpenAI 任务状态 → 我们的内部状态归一化（对外只暴露 OpenAI 的四个值）
_OAI_STATUS = {
    "queued": "queued",
    "switching_exit": "in_progress",
    "running": "in_progress",
    "downloading": "in_progress",
    "completed": "completed",
    "failed": "failed",
}


def oai_status(internal: str) -> str:
    return _OAI_STATUS.get(internal or "", "in_progress")


# ---------------------------------------------------------------- 认证


def auth(request: Request):
    if not API_KEY:
        return
    hdr = request.headers.get("authorization") or ""
    token = hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("x-api-key") or ""
    if token != API_KEY:
        raise HTTPException(status_code=401, detail={"error": {"message": "Invalid API key"}})


# ---------------------------------------------------------------- 请求校验

# 各模型对输入的硬性要求（均为实测确认，见 README「多图支持」一节）
#   p-image-upscale 无图时上游返回 HTTP 400 {"error":"image is required"}
NEEDS_VIDEO = {"p-video-animate", "p-video-replace", "p-video-edit"}
NEEDS_IMAGE = {"p-video-animate", "p-video-replace", "p-image-upscale"}


def validate_request(model: str, prompt: str | None, n_imgs: int, has_video: bool) -> None:
    """提交前的本地防呆校验。

    两个目的：
      1. **不浪费出口配额** —— 配额在「提交时」就被上游扣掉，参数错了也照扣；
      2. **不让确定性错误进入重试循环** —— 否则一轮重试会给所有出口打上冷却标记。

    只校验实测确认过的硬性约束，不做过度推测（宁可放过、不误杀）。
    """
    def bad(msg: str, param: str | None = None):
        raise HTTPException(status_code=400, detail={
            "error": {
                "message": msg,
                "type": "invalid_request_error",
                "param": param,
                "code": "invalid_request",
            }
        })

    if model in NEEDS_VIDEO and not has_video:
        bad(f"模型 {model} 需要源视频（video 字段）", "video")
    if model in NEEDS_IMAGE and n_imgs < 1:
        bad(f"模型 {model} 需要至少 1 张参考图（image 或 images 字段）", "image")
    if not (prompt or "").strip() and n_imgs == 0 and not has_video:
        bad("至少要提供 prompt、一张参考图或一段源视频中的一个", "prompt")
    # 上游临时下线的模型（如 2026-09 的 p-video-2-pro，generation-status 里 disabled:true）
    # 提前拦掉，免得白跑一个出口、白扣一次配额
    if is_model_disabled(model):
        raise HTTPException(status_code=400, detail={
            "error": {
                "message": f"模型 {model} 当前被上游下线（temporarily unavailable），请换其他模型",
                "type": "invalid_request_error",
                "param": "model",
                "code": "model_disabled",
            }
        })


# ---------------------------------------------------------------- 任务执行


def _public_base(request: Request | None) -> str:
    if PUBLIC_BASE:
        return PUBLIC_BASE.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1:3020"


def run_task(task_id: str, model: str, kind: str, payload: dict, base_url: str):
    """后台线程：提交 → 轮询 → 下载转存 → 更新 DB。"""
    def on_switch(ex, attempt):
        task_save(task_id, exit_name=ex.name, egress_ip=ex.egress_ip,
                  attempts=attempt + 1, status="switching_exit")

    try:
        task_save(task_id, status="running")
        images = payload.get("images") or []
        if payload.get("image") and payload["image"] not in images:
            images = [payload["image"]] + images
        if payload.get("garments"):
            images = images + list(payload["garments"])

        params = {k: v for k, v in (payload.get("params") or {}).items() if v is not None}

        with submit_lock:
            res = client.generate(
                model=model,
                prompt=payload.get("prompt"),
                images=images,
                video=payload.get("video"),
                last_frame=payload.get("last_frame"),
                params=params,
                on_exit_switch=on_switch,
                poll_timeout=900,
            )

        task_save(task_id, status="downloading", remote_url=res.url,
                  job_id=res.job_id, exit_name=res.exit_name, egress_ip=res.egress_ip)

        # 下载转存到 NAS
        ext = ".png" if kind == "image" else ".mp4"
        for cand in (".jpg", ".jpeg", ".webp", ".png", ".mp4", ".webm", ".mov"):
            if cand in res.url.lower():
                ext = cand
                break
        fname = f"{task_id}{ext}"
        dest = os.path.join(MEDIA_DIR, fname)
        proxies = None
        if res.exit_name:
            for e in pool.exits:
                if e.name == res.exit_name and e.proxy:
                    proxies = {"http": e.proxy, "https": e.proxy}
                    break
        client.download(res.url, dest, proxies=proxies)

        size = os.path.getsize(dest)
        task_save(
            task_id,
            status="completed",
            local_file=dest,
            local_url=f"{base_url}/files/{fname}",
        )
        log.info("任务 %s 完成 (%s, %.1f KB) 出口=%s", task_id, model, size / 1024, res.exit_name)

    except QuotaExhausted as e:
        log.error("任务 %s 配额耗尽: %s", task_id, e)
        task_save(task_id, status="failed", error=f"所有出口配额耗尽: {e.detail}")
    except DeterministicError as e:
        # 参数 / 端点类错误：对外只给干净的信息，不带内部异常类名
        log.error("任务 %s 确定性错误: %s", task_id, e)
        task_save(task_id, status="failed", error=str(e))
    except TaskFailedAfterSubmit as e:
        log.error("任务 %s 已提交后失败: %s", task_id, e)
        task_save(task_id, status="failed", error=str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("任务 %s 失败", task_id)
        task_save(task_id, status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        pool.restore_node()


def spawn(task_id: str, model: str, kind: str, payload: dict, base_url: str):
    t = threading.Thread(
        target=run_task, args=(task_id, model, kind, payload, base_url), daemon=True
    )
    t.start()


def wait_task(task_id: str, timeout: float = 1800, interval: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = task_get(task_id)
        if t and t["status"] in ("completed", "failed"):
            return t
        time.sleep(interval)
    raise HTTPException(status_code=504, detail={"error": {"message": "等待超时"}})


# ---------------------------------------------------------------- FastAPI

app = FastAPI(title="pruna2api", version="1.0.0")


# ---- OpenAI 风格错误体 ----------------------------------------------------
# FastAPI 默认把错误包成 {"detail": ...}，OpenAI SDK / 客户端认不出来。
# OpenAI 规范是顶层 {"error": {"message","type","param","code"}}，这里统一掉。

@app.exception_handler(HTTPException)
async def _oai_http_error(request: Request, exc: HTTPException):
    d = exc.detail
    if isinstance(d, dict) and isinstance(d.get("error"), dict):
        err = dict(d["error"])          # 已经是 error 形状，只补缺失字段
    else:
        err = {"message": str(d)}
    err.setdefault("type", "invalid_request_error" if exc.status_code < 500 else "api_error")
    err.setdefault("param", None)
    err.setdefault("code", None)
    return JSONResponse(status_code=exc.status_code, content={"error": err})


@app.exception_handler(RequestValidationError)
async def _oai_validation_error(request: Request, exc: RequestValidationError):
    errs = exc.errors()
    first = errs[0] if errs else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
    msg = f"参数错误: {loc} {first.get('msg', '')}".strip()
    return JSONResponse(status_code=400, content={"error": {
        "message": msg,
        "type": "invalid_request_error",
        "param": loc or None,
        "code": "invalid_request",
    }})


@app.on_event("startup")
def _startup():
    db_init()
    # 启动时把僵尸任务标为失败（进程重启前未完成的）
    with db() as c:
        c.execute(
            "UPDATE tasks SET status='failed', error='服务重启中断', updated_at=? "
            "WHERE status IN ('queued','running','downloading','switching_exit')",
            (time.time(),),
        )
    log.info("pruna2api 启动，数据目录 %s，媒体目录 %s，出口池 %d 个",
             DATA_DIR, MEDIA_DIR, len(pool.exits))

    # 后台探测一次出口 IP（不阻塞启动；失败无所谓，提交时会再探）
    def _bg_probe():
        # 订阅模式下出口可能几十个，全量探测要好几分钟 —— 只预探前 8 个，
        # 其余等真正轮到时懒探测。
        try:
            got = pool.refresh_all_ips(limit=8)
            log.info("出口 IP 预探测完成（前 8 个）: %s", got)
        except Exception:  # noqa: BLE001
            log.warning("出口 IP 预探测失败（不影响服务）", exc_info=True)

    threading.Thread(target=_bg_probe, daemon=True).start()


# ---- 健康 / 元信息 ----

@app.get("/health")
def health():
    with db() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {
        "status": "ok",
        "exits_total": len(pool.exits),
        "exits_available": len(pool.healthy_exits()),
        "tasks": counts,
        "media_dir": MEDIA_DIR,
    }


@app.get("/v1/models")
def list_models(refresh: bool = False):
    """列模型。

    配额与可用性取自上游 /api/generation-status（60s 缓存，`?refresh=1` 强制刷新），
    查不到时回落到本地 MODEL_QUOTA 常量。并发查询避免逐个串行拖慢响应。
    """
    with ThreadPoolExecutor(max_workers=6) as ex:
        sts = list(ex.map(
            lambda m: fetch_model_status(m, use_cache=not refresh), ALL_MODELS))

    data = []
    for m, st in zip(ALL_MODELS, sts):
        item = {
            "id": m,
            "object": "model",
            "owned_by": "pruna",
            "kind": "image" if m in IMAGE_MODELS else "video",
            "free_quota_per_ip": (
                int(st["max"]) if st.get("max")
                else MODEL_QUOTA.get(m, DEFAULT_QUOTA)),
        }
        if st:
            item["remaining_per_ip"] = st.get("remaining")
            item["disabled"] = bool(st.get("disabled"))
            item["can_generate"] = bool(st.get("canGenerate"))
        data.append(item)
    return {"object": "list", "data": data}


@app.get("/v1/exits")
def list_exits():
    return {
        "mode": pool.mode,              # "mihomo"（自带订阅节点）或 "system"
        "total": len(pool.exits),
        "exits": [
            {
                "name": e.name,
                "node": e.node,
                "proxy": e.proxy,
                "egress_ip": e.egress_ip,
                "available": e.available,
                "fail_count": e.fail_count,
                "cooldown_remaining": max(0, round(e.cooldown_until - time.time(), 1)),
            }
            for e in pool.exits
        ],
    }


@app.post("/v1/exits/probe")
def probe_exits(limit: int = 0):
    """逐个切换并探测出口的真实出口 IP。

    订阅模式下出口可能有几十个，全量探测要好几分钟 —— 用 `?limit=N` 只探前 N 个。
    """
    ips = pool.refresh_all_ips(limit=limit or None)
    return {"probed": ips, "count": len(ips), "total": len(pool.exits)}                      


# ---- 生成接口 ----

# ---------------------------------------------------------------- mihomo 订阅管理

MIHOMO_DIR = os.environ.get("PRUNA_MIHOMO_DIR", "/opt/pruna2api/mihomo")
MIHOMO_PROVIDER = os.environ.get("PRUNA_MIHOMO_PROVIDER", "sub")
SUB_FILE = os.path.join(MIHOMO_DIR, "subscription.txt")
CONF_FILE = os.path.join(MIHOMO_DIR, "config.yaml")
SUB_UA = os.environ.get("PRUNA_SUB_UA", "clash.meta")

_MIHOMO_PORT = (urlparse(MIHOMO_PROXY).port or 7891)
_MIHOMO_CTRL = urlparse(MIHOMO_API)
MIHOMO_CTRL = f"{_MIHOMO_CTRL.hostname}:{_MIHOMO_CTRL.port}"

GOST_EXIT = Exit("gost", None, "http://127.0.0.1:8082")


def render_mihomo_config(sub_url: str, ua: str = SUB_UA) -> str:
    """生成自带 mihomo 的配置。

    - 端口刻意错开系统 clash（它占 7890/9090）
    - **不写 `tun:` 段** —— 系统 clash 已用 TUN 接管全局，再开会冲突
    - `exclude-filter` 排掉订阅里夹带的「剩余流量 / 套餐到期」伪节点
    """
    return f"""# pruna2api 自带的 mihomo 实例（systemd 单元 mihomo-pruna）
# ⚠️ 本文件由 /v1/mihomo/subscription 接口自动生成，手工改动会在下次保存订阅时被覆盖
mixed-port: {_MIHOMO_PORT}
bind-address: 127.0.0.1
allow-lan: false
mode: rule
log-level: warning
ipv6: false
unified-delay: true
tcp-concurrent: true
external-controller: {MIHOMO_CTRL}
secret: ""
geodata-mode: false
geo-auto-update: false
find-process-mode: "off"

profile:
  store-selected: true
  store-fake-ip: false

proxy-providers:
  {MIHOMO_PROVIDER}:
    type: http
    url: "{sub_url}"
    interval: 3600
    path: ./providers/{MIHOMO_PROVIDER}.yaml
    header:
      User-Agent: ["{ua}"]
    health-check:
      enable: false

proxy-groups:
  - name: {MIHOMO_GROUP}
    type: select
    use: [{MIHOMO_PROVIDER}]
    exclude-filter: "剩余流量|套餐到期|重置|官网|订阅|到期|流量"

rules:
  - MATCH,{MIHOMO_GROUP}
"""


def _mask_sub_url(u: str) -> dict:
    """脱敏展示 —— 订阅链接含 token，**绝不回显完整内容**。"""
    try:
        p = urlparse(u)
        return {
            "host": p.netloc,
            "scheme": p.scheme,
            "length": len(u),
            "masked": f"{p.scheme}://{p.netloc}/…（共 {len(u)} 字符，已隐藏）",
        }
    except Exception:  # noqa: BLE001
        return {"host": "", "scheme": "", "length": len(u), "masked": "（无法解析）"}


def _read_sub_url() -> str:
    try:
        with open(SUB_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _fetch_sub(url: str, ua: str, timeout: int = 30) -> tuple[bool, str]:
    """拉一次订阅验证可用性：先直连，失败再走自带 mihomo 代理。"""
    last = "未知错误"
    for tag, proxies in (("直连", {"http": None, "https": None}),
                         ("经 mihomo", {"http": MIHOMO_PROXY, "https": MIHOMO_PROXY})):
        try:
            r = requests.get(url, timeout=timeout, proxies=proxies,
                             headers={"User-Agent": ua})
            if r.status_code == 200 and "proxies:" in r.text[:8000]:
                n = r.text.count("- name:") or r.text.count("name:")
                return True, f"{tag}成功，{len(r.content)} 字节，约 {n} 个节点"
            last = f"HTTP {r.status_code}（返回内容不是 clash 配置？）"
        except requests.RequestException as e:
            last = f"{tag}失败: {str(e)[:120]}"
    return False, last


def _reload_mihomo_config() -> str:
    """让 mihomo 热重载配置文件；失败则回退重启 systemd 单元。"""
    try:
        r = requests.put(f"{MIHOMO_API}/configs?force=true",
                         json={"path": CONF_FILE}, timeout=20,
                         proxies={"http": None, "https": None})
        if r.status_code in (200, 204):
            time.sleep(3)
            return "hot-reload"
        log.warning("mihomo 热重载返回 HTTP %s", r.status_code)
    except requests.RequestException as e:
        log.warning("mihomo 热重载失败: %s", e)
    try:
        subprocess.run(["systemctl", "restart", "mihomo-pruna"],
                       timeout=40, check=False, capture_output=True)
        time.sleep(5)
        return "systemd-restart"
    except Exception as e:  # noqa: BLE001
        log.error("重启 mihomo-pruna 也失败: %s", e)
        return "failed"


def _pool_with_gost(exits: list[Exit]) -> list[Exit]:
    if any(e.name == "gost" for e in exits):
        return exits
    return exits + [GOST_EXIT]


def _rebuild_pool(reason: str = "") -> int:
    """重新枚举订阅节点并替换出口池。返回出口数。"""
    new_exits = load_subscription_exits()
    if not new_exits:
        log.warning("重建出口池失败（未取到节点），保持原池 %d 个", len(pool.exits))
        return len(pool.exits)
    with pool._lock:
        pool.exits = _pool_with_gost(new_exits)
        pool.mode = "mihomo"
        pool._rr = 0
        pool._current_node = None
    log.info("出口池已重建（%s）: %d 个出口", reason or "手动", len(pool.exits))
    return len(pool.exits)


@app.get("/v1/mihomo/subscription")
def get_subscription():
    """当前订阅信息（**脱敏**，不回显完整链接）。"""
    u = _read_sub_url()
    info: dict[str, Any] = {
        "configured": bool(u),
        "source": pool.mode,
        "exits": len(pool.exits),
        "api": MIHOMO_API,
        "proxy": MIHOMO_PROXY,
        "group": MIHOMO_GROUP,
        "provider": MIHOMO_PROVIDER,
        "user_agent": SUB_UA,
        "sub_file": SUB_FILE,
        "config_file": CONF_FILE,
    }
    if u:
        info.update(_mask_sub_url(u))

    # 顺带报告节点数 + 订阅自带的套餐信息。
    #   这类订阅常把「剩余流量：493.77 GB / 套餐到期：2026-09-28」当**伪节点**塞进
    #   proxies 里（而不是走 subscriptionInfo 字段），所以从节点名里解析最可靠。
    try:
        r = requests.get(f"{MIHOMO_API}/providers/proxies/{MIHOMO_PROVIDER}",
                         timeout=8, proxies={"http": None, "https": None})
        prov = r.json().get("proxies") or []
        info["node_count"] = len(prov)
        meta: dict[str, Any] = {}
        for p in prov:
            nm = p.get("name") or ""
            m = re.search(r"剩余流量[：:]\s*([\d.]+\s*[KMGTP]?B)", nm)
            if m:
                meta["remain"] = m.group(1).strip()
            m = re.search(r"套餐到期[：:]\s*([\d\-/]+)", nm)
            if m:
                meta["expire"] = m.group(1)
            m = re.search(r"重置剩余[：:]\s*(\d+)\s*天", nm)
            if m:
                meta["reset_days"] = int(m.group(1))
        if meta:
            info["sub_meta"] = meta
    except Exception:  # noqa: BLE001
        pass
    return info


class SubUpdate(BaseModel):
    url: str
    user_agent: str | None = None


@app.post("/v1/mihomo/subscription")
def set_subscription(req: SubUpdate):
    """写入订阅链接 → 重新生成配置 → 热重载 mihomo → 重建出口池。"""
    url = (req.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, {"error": {
            "message": "订阅链接必须以 http:// 或 https:// 开头",
            "type": "invalid_request_error", "param": "url",
            "code": "invalid_request"}})
    ua = (req.user_agent or SUB_UA).strip()

    # 先验证能拉取，避免写入坏链接把 mihomo 弄挂
    ok, msg = _fetch_sub(url, ua)
    if not ok:
        raise HTTPException(400, {"error": {
            "message": f"订阅不可用：{msg}",
            "type": "invalid_request_error", "param": "url",
            "code": "subscription_unreachable"}})

    try:
        os.makedirs(MIHOMO_DIR, exist_ok=True)
        tmp = SUB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(url)
        os.chmod(tmp, 0o600)          # 含 token，只给属主读写
        os.replace(tmp, SUB_FILE)
        with open(CONF_FILE, "w", encoding="utf-8") as f:
            f.write(render_mihomo_config(url, ua))
    except OSError as e:
        raise HTTPException(500, {"error": {
            "message": f"写文件失败: {e}", "type": "api_error", "code": "io_error"}})

    mode = _reload_mihomo_config()
    n = _rebuild_pool("换订阅")
    return {"ok": True, "subscription": _mask_sub_url(url),
            "fetched": msg, "reload": mode, "exits": n}


@app.post("/v1/mihomo/reload")
def reload_mihomo():
    """重新拉取订阅节点并重建出口池（不重启进程）。"""
    provider_ok = False
    try:
        r = requests.put(f"{MIHOMO_API}/providers/proxies/{MIHOMO_PROVIDER}",
                         timeout=90, proxies={"http": None, "https": None})
        provider_ok = r.status_code in (200, 204)
    except requests.RequestException as e:
        log.warning("刷新 provider 失败: %s", e)
    n = _rebuild_pool("刷新节点")
    return {"ok": True, "provider_refreshed": provider_ok, "exits": n}


@app.post("/v1/videos/generations")
def videos_generations(req: VideoGenRequest, request: Request):
    auth(request)
    if req.model not in ALL_MODELS:
        raise HTTPException(400, {"error": {"message": f"未知模型 {req.model}",
                                            "type": "invalid_request_error",
                                            "code": "model_not_found"}})
    kind = "image" if req.model in IMAGE_MODELS else "video"
    task_id = f"task_{uuid.uuid4().hex[:20]}"

    params = {
        "aspect_ratio": req.aspect_ratio,
        "resolution": req.resolution,
        "duration": req.duration,
        "mode": req.mode,
        "prompt_upsampler": req.prompt_upsampler,
        "target_fps": req.target_fps,
        "seed": req.seed,
    }
    imgs = list(req.images or [])
    payload = {
        "prompt": req.prompt,
        "image": req.image,
        "images": imgs,
        # 首尾帧：p-video-2-pro 支持 首帧(image) + 尾帧(last_frame_image) 两个槽位
        "last_frame": req.last_frame,
        "video": req.video,
        "params": params,
    }
    n_imgs = len(imgs) + (1 if req.image else 0) + (1 if req.last_frame else 0)
    # 校验放在 task_create / spawn 之前：失败就退出，不建任务、不碰出口、不扣配额
    validate_request(req.model, req.prompt, n_imgs, bool(req.video))
    task_create(task_id, req.model, kind, req.prompt or "", params, n_imgs)

    base = _public_base(request)
    spawn(task_id, req.model, kind, payload, base)

    if not req.wait:
        return {"id": task_id, "object": "video.generation.task", "status": "queued",
                "model": req.model}

    t = wait_task(task_id)
    if t["status"] == "failed":
        raise HTTPException(500, {"error": {"message": t["error"], "task_id": task_id}})
    return {
        "id": task_id,
        "object": "video.generation",
        "created": int(t["created_at"]),
        "model": t["model"],
        "status": "completed",
        "data": [{"url": t["local_url"], "remote_url": t["remote_url"]}],
        "exit": {"name": t["exit_name"], "egress_ip": t["egress_ip"]},
    }


@app.post("/v1/videos")
def videos_alias(req: VideoGenRequest, request: Request):
    """OpenAI Sora 风格的提交入口别名（`POST /v1/videos`）。

    与 `/v1/videos/generations` 完全同义 —— 有些客户端只认 Sora 那个路径。
    """
    return videos_generations(req, request)


@app.post("/v1/images/generations")
def images_generations(req: ImageGenRequest, request: Request):
    """OpenAI 风格的图片生成入口。

    兼容 OpenAI 标准参数：`size`（映射成 Pruna 的宽高比）、`n`（张数）、
    `response_format`（`url` 或 `b64_json`）。
    """
    auth(request)
    model = req.model or "p-image"
    if model not in ALL_MODELS:
        raise HTTPException(400, {"error": {"message": f"未知模型 {model}",
                                            "type": "invalid_request_error",
                                            "code": "model_not_found"}})

    imgs = list(req.images or [])
    if req.image and req.image not in imgs:
        imgs = [req.image] + imgs
    if req.garments:
        imgs = imgs + list(req.garments)

    # OpenAI 的 "1024x1024" 这类尺寸 → Pruna 的 aspect_ratio（显式传了 aspect_ratio 则以其为准）
    aspect = req.aspect_ratio or ratio_from_size(req.size)

    # 校验必须在建任务 / 占出口之前 —— 失败直接退出，不扣任何配额
    validate_request(model, req.prompt, len(imgs), False)

    base = _public_base(request)
    params = {"aspect_ratio": aspect, "seed": req.seed,
              "target": req.target, "turbo": req.turbo}
    payload = {"prompt": req.prompt, "images": imgs, "params": params}

    # n 张 = 提交 n 次（Pruna 的配额按次扣，省不掉）
    ids: list[str] = []
    for _ in range(req.n):
        tid = f"task_{uuid.uuid4().hex[:20]}"
        task_create(tid, model, "image", req.prompt or "", params, len(imgs))
        spawn(tid, model, "image", payload, base)
        ids.append(tid)

    if not req.wait:
        return {"id": ids[0], "ids": ids, "object": "image.generation",
                "status": "queued", "model": model}

    data: list[dict] = []
    errors: list[str] = []
    created = int(time.time())
    last_exit: dict = {}
    for tid in ids:
        t = wait_task(tid)
        if t["status"] == "failed":
            errors.append(t["error"])
            continue
        created = int(t["created_at"])
        last_exit = {"name": t["exit_name"], "egress_ip": t["egress_ip"]}
        data.append({"url": t["local_url"], "remote_url": t["remote_url"]})

    if not data:
        raise HTTPException(500, {"error": {"message": "; ".join(errors) or "生成失败",
                                            "type": "api_error",
                                            "code": "generation_failed"}})

    if req.response_format == "b64_json":
        # OpenAI 在 b64_json 模式下不返回 url，只给 base64
        for item in data:
            fname = str(item.get("url") or "").rsplit("/", 1)[-1]
            try:
                with open(os.path.join(MEDIA_DIR, fname), "rb") as fh:
                    item["b64_json"] = base64.b64encode(fh.read()).decode()
                item.pop("url", None)
                item.pop("remote_url", None)
            except OSError as e:  # noqa: PERF203
                errors.append(f"读取 {fname} 失败: {e}")

    out: dict[str, Any] = {"created": created, "model": model,
                           "status": "completed", "data": data}
    if last_exit:
        out["exit"] = last_exit
    if errors:
        out["partial_errors"] = errors
    return out


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: str):
    t = task_get(task_id)
    if not t:
        raise HTTPException(404, {"error": {"message": "task not found",
                                            "type": "invalid_request_error",
                                            "code": "task_not_found"}})
    kind = t["kind"] or "video"
    out = {
        "id": t["id"],
        "object": "video.generation" if kind == "video" else "image.generation",
        "created": int(t["created_at"]),
        "model": t["model"],
        "kind": kind,
        # OpenAI 语义：queued / in_progress / completed / failed
        "status": oai_status(t["status"]),
        # 内部细粒度状态（switching_exit / downloading …），前端展示用
        "status_detail": t["status"],
        "created_at": t["created_at"],
        "exit": {"name": t["exit_name"], "egress_ip": t["egress_ip"]},
        "attempts": t["attempts"],
    }
    if t["status"] == "completed":
        out["data"] = [{"url": t["local_url"], "remote_url": t["remote_url"]}]
    if t["error"]:
        out["error"] = t["error"]
    return out


@app.get("/v1/tasks")
def list_tasks(limit: int = 50, status: str | None = None):
    q = "SELECT * FROM tasks"
    args: list[Any] = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        rows = c.execute(q, args).fetchall()
    return {"data": [dict(r) for r in rows]}


@app.get("/files/{name}")
def get_file(name: str):
    # 防目录穿越
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    path = os.path.join(MEDIA_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


@app.get("/_selftest", response_class=HTMLResponse)
def selftest():
    """UI 自检页（与本服务同源，便于 iframe 驱动）。文件不存在则 404。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ui_e2e.html")
    if not os.path.isfile(p):
        raise HTTPException(404, "selftest page missing")
    with open(p, encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ---- 简易面板 ----

# ---- Web 控制台 ----

UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
_ui_cache: dict[str, Any] = {"mtime": 0.0, "html": ""}


@app.get("/", response_class=HTMLResponse)
def index():
    """读 ui.html（带 mtime 缓存，改完刷新即生效）；文件缺失时给出提示。"""
    try:
        mt = os.path.getmtime(UI_FILE)
    except OSError:
        return HTMLResponse(
            "<h1>ui.html 缺失</h1><p>把 ui.html 放到 " + UI_FILE + " 旁边即可。</p>",
            status_code=500,
        )
    if mt != _ui_cache["mtime"]:
        with open(UI_FILE, encoding="utf-8") as f:
            _ui_cache["html"] = f.read()
        _ui_cache["mtime"] = mt
    return HTMLResponse(_ui_cache["html"])


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PRUNA_PORT", "3020"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
