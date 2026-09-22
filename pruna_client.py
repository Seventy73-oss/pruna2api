"""
Pruna Playground 客户端：模型调用 + 出口 IP 自动轮换。

出口策略（NAS 环境）：
  NAS 无直连外网，全部流量走 clash（TUN 透明劫持）。clash 提供 mixed-port 7890，
  且通过控制 API http://127.0.0.1:9090 可以切换「良心云」Selector 的当前节点，
  从而改变 7890 的出口 IP。每个出口 IP 在 Pruna 侧是独立配额，所以换节点 == 重置配额。

出口池来自实测（2026-09-19，NAS 上逐个切换验证）：
  美区高速 01-05、英区伦敦 01-02 —— 均为独立 IP，且均可直连 Pruna。
  日/新/港节点在 NAS 上连不通（超时），不纳入池。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

log = logging.getLogger("pruna.client")

BASE = "https://playground.pruna.ai"
# ⚠️ 真实端点是 /api/{model}/generate 与 /api/{model}/status/{id}
#    （从 chunk 里 grep 到 "/api/p-video-2-pro/generate" 等字面量实测确认；
#      裸 /generate 返回 404，/api/p-image/generate 的 GET 返回 405）
GENERATE_PATH = "/api/{model}/generate"
STATUS_PATH = "/api/{model}/status/{job_id}"


def generate_url(model: str) -> str:
    return BASE + GENERATE_PATH.format(model=model)


def status_url(model: str, job_id: str) -> str:
    return BASE + STATUS_PATH.format(model=model, job_id=job_id)


# ------------------------------------------------- 实时模型状态（上游新端点）
# 2026-09 上游新增 /api/generation-status?model=X，返回：
#   {"count":4,"max":5,"remaining":1,"canGenerate":true,"disabled":false,"model":"p-video-2"}
# 用它替代硬编码配额，并能提前发现「被下线的模型」（如 p-video-2-pro 的 disabled:true）。
MODEL_STATUS_TTL = 60
_model_status_cache: dict[str, tuple[float, dict]] = {}
_model_status_lock = threading.Lock()


def fetch_model_status(model: str, proxies=None, timeout: int = 12,
                       use_cache: bool = True) -> dict:
    """查询单个模型的实时配额与可用性（带 TTL 缓存）。

    返回 `{}` 表示查询失败 —— 调用方应回落到 `MODEL_QUOTA` 常量。
    """
    now = time.time()
    if use_cache:
        with _model_status_lock:
            hit = _model_status_cache.get(model)
        if hit and now - hit[0] < MODEL_STATUS_TTL:
            return hit[1]

    try:
        r = requests.get(
            f"{BASE}/api/generation-status?model={model}",
            timeout=timeout,
            proxies=proxies or {"http": None, "https": None},
        )
        if r.status_code != 200:
            return {}
        d = r.json()
    except (requests.RequestException, ValueError):
        return {}

    if not isinstance(d, dict) or "max" not in d:
        return {}

    with _model_status_lock:
        _model_status_cache[model] = (now, d)
    return d


def quota_of(model: str) -> int:
    """该模型每出口的免费额度：优先用实时值，失败回落到常量。"""
    st = fetch_model_status(model)
    if st.get("max"):
        return int(st["max"])
    return MODEL_QUOTA.get(model, DEFAULT_QUOTA)


def is_model_disabled(model: str) -> bool:
    """模型是否被上游下线（查不到状态时视为可用，不误伤）。"""
    return bool(fetch_model_status(model).get("disabled"))


# ---------------------------------------------------------------- 模型定义

IMAGE_MODELS = ["p-image", "p-image-ideogram", "p-image-edit", "p-image-upscale", "p-image-try-on"]
VIDEO_MODELS = [
    "p-video",
    "p-video-avatar",
    "p-video-animate",
    "p-video-replace",
    "p-video-edit",
    "p-video-2",
    "p-video-2-pro",
]
ALL_MODELS = IMAGE_MODELS + VIDEO_MODELS

# 各模型免费额度（每出口 IP 独立计数）
# ⚠️ 2026-09-22 实测修正：**所有视频模型的 max 都是 5**（此前 p-video-2 记的是 10，是错的）；
#    图片模型 10。真实值可用 /api/generation-status?model=X 实时查询，
#    这里的常量只作为查不到时的兜底。
MODEL_QUOTA = {
    "p-video-2-pro": 5,
    "p-video-2": 5,
    "p-video": 5,
    "p-video-avatar": 5,
    "p-video-animate": 5,
    "p-video-replace": 5,
    "p-video-edit": 5,
}
DEFAULT_QUOTA = 10          # 图片类模型

# 需要图片输入的模型及其字段名（单数 image / 复数 images / 专用字段）
# None 表示可选
IMAGE_FIELD = {
    "p-image-edit": "images",       # 复数，服务端不限张数
    "p-image-try-on": "garment_images",  # 服装图（重复字段），另有 person 图
    "p-image-upscale": "image",
    "p-video": "image",
    "p-video-avatar": "image",
    "p-video-animate": "image",
    "p-video-replace": "image",     # ⚠️ 单数，用 images 会 400
    "p-video-edit": "images",       # 复数
    "p-video-2": "image",
    "p-video-2-pro": "image",
}
# 提示词字段名（少数模型用 instruction_prompt）
PROMPT_FIELD = {
    "p-video-animate": "instruction_prompt",
    "p-video-replace": "instruction_prompt",
}

# ⚠️ 请求体编码方式：搞错会被上游 500 拒绝
#      {"error":"No number after minus sign in JSON at position 1 (line 1 column 2)"}
#    —— 上游把 multipart 的 "--" 前缀当 JSON 解析了。
#    这 4 个 p-image 系模型要 JSON body（图片以 dataURL 传）；
#    p-image-try-on 与全部视频模型要 multipart。
JSON_MODELS = {"p-image", "p-image-ideogram", "p-image-edit", "p-image-upscale"}

# ---------------------------------------------------------------- 出口 IP 池


@dataclass
class Exit:
    """一个可切换的出口。proxy 为 None 表示直连。"""
    name: str                 # 内部标识
    node: str | None          # clash 节点名；None = 不切节点（用当前节点）
    proxy: str | None         # HTTP 代理地址；None = 不走显式代理
    egress_ip: str = ""       # 最近一次探测到的出口 IP
    fail_count: int = 0
    cooldown_until: float = 0.0

    @property
    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def penalize(self, seconds: float = 180.0) -> None:
        self.fail_count += 1
        self.cooldown_until = time.time() + seconds


# ---------------------------------------------------------------- 出口来源
#
#   mihomo  = 用 pruna2api 自带的 mihomo 实例（systemd 单元 mihomo-pruna），
#             出口池 = 订阅里的全部节点，换节点即换出口 IP。【默认】
#   system  = 用系统 clash（容器里那个）里固定的那批节点（旧行为，兜底）
EXIT_SOURCE = os.environ.get("PRUNA_EXIT_SOURCE", "mihomo").strip().lower()

# 自带 mihomo：控制面 + 代理入口（端口刻意错开系统 clash 的 9090/7890）
MIHOMO_API = os.environ.get("PRUNA_MIHOMO_API", "http://127.0.0.1:9091").rstrip("/")
MIHOMO_PROXY = os.environ.get("PRUNA_MIHOMO_PROXY", "http://127.0.0.1:7891")
MIHOMO_GROUP = os.environ.get("PRUNA_MIHOMO_GROUP", "PRUNA")

# 系统 clash（容器内 mihomo）
CLASH_API = os.environ.get("PRUNA_CLASH_API", "http://127.0.0.1:9090").rstrip("/")
CLASH_SELECTOR = os.environ.get("PRUNA_CLASH_SELECTOR", "良心云")

# 系统 clash 上实测可用的固定出口
# ⚠️ 不含 direct：NAS 本身无直连外网出口（clash TUN 接管），直连必失败
SYSTEM_EXITS = [
    Exit("us01", "🇺🇸 US-01", "http://127.0.0.1:7890"),
    Exit("us02", "🇺🇸 US-02", "http://127.0.0.1:7890"),
    Exit("us03", "🇺🇸 US-03", "http://127.0.0.1:7890"),
    Exit("us04", "🇺🇸 US-04", "http://127.0.0.1:7890"),
    Exit("us05", "🇺🇸 US-05", "http://127.0.0.1:7890"),
    Exit("uk01", "🇬🇧 UK-01", "http://127.0.0.1:7890"),
    Exit("uk02", "🇬🇧 UK-02", "http://127.0.0.1:7890"),
    Exit("gost", None, "http://127.0.0.1:8082"),   # gost 独立出口（示例）
]
NAS_EXITS = SYSTEM_EXITS          # 向后兼容别名

# 国旗 emoji → 地区码，用于把 `🇭🇰香港高速01` 这类节点名压成短 ID
_FLAG_CODES = {
    "🇭🇰": "hk", "🇹🇼": "tw", "🇯🇵": "jp", "🇰🇷": "kr", "🇸🇬": "sg", "🇲🇴": "mo",
    "🇺🇸": "us", "🇬🇧": "uk", "🇩🇪": "de", "🇫🇷": "fr", "🇳🇱": "nl", "🇨🇭": "ch",
    "🇨🇦": "ca", "🇦🇺": "au", "🇷🇺": "ru", "🇮🇳": "in", "🇹🇷": "tr", "🇮🇹": "it",
    "🇪🇸": "es", "🇸🇪": "se", "🇻🇳": "vn", "🇹🇭": "th", "🇲🇾": "my", "🇵🇭": "ph",
}

# mihomo 内置的非节点条目，枚举时要排掉
_BUILTIN_PROXIES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE",
                    "GLOBAL", "COMPATIBLE", "DIRECT-URL", "DIRECT-IP"}


def _region_of(node: str) -> str:
    for flag, code in _FLAG_CODES.items():
        if node.startswith(flag):
            return code
    return ""


def load_subscription_exits(timeout: int = 10) -> list[Exit]:
    """从自带 mihomo 的 proxy-group 枚举订阅节点，构建出口池。

    节点名形如 `🇭🇰香港高速01`，压成 `hk01` 这样的短 ID 便于展示；
    完整节点名留在 `Exit.node` 里，用于控制面切换。
    """
    try:
        r = requests.get(f"{MIHOMO_API}/proxies/{MIHOMO_GROUP}",
                         timeout=timeout, proxies={"http": None, "https": None})
        r.raise_for_status()
        nodes = r.json().get("all") or []
    except Exception as e:  # noqa: BLE001
        log.warning("从 mihomo(%s) 枚举节点失败: %s", MIHOMO_API, e)
        return []

    out: list[Exit] = []
    seen: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, str) or node in _BUILTIN_PROXIES or node == MIHOMO_GROUP:
            continue
        cc = _region_of(node) or "n"
        seen[cc] = seen.get(cc, 0) + 1
        out.append(Exit(f"{cc}{seen[cc]:02d}", node, MIHOMO_PROXY))
    return out


def load_default_exits() -> tuple[list[Exit], str]:
    """决定默认出口池，返回 `(出口列表, 实际生效的模式)`。

    模式必须跟着实际结果走 —— 若 mihomo 不可用而回退到了系统 clash 的出口，
    模式就得是 "system"，否则切节点会打到错误的控制面。
    """
    if EXIT_SOURCE == "mihomo":
        subs = load_subscription_exits()
        if subs:
            # gost 是独立出口（另一个端口），追加进池作为补充
            return subs + [Exit("gost", None, "http://127.0.0.1:8082")], "mihomo"
        log.warning("自带 mihomo 未返回任何节点，回退到系统 clash 固定出口")
    return list(SYSTEM_EXITS), "system"


class QuotaExhausted(Exception):
    """该出口的模型配额已用尽（HTTP 429）。"""

    def __init__(self, model: str, detail: str = ""):
        self.model = model
        self.detail = detail
        super().__init__(f"quota exhausted for {model}: {detail}")


class DeterministicError(Exception):
    """确定性错误：参数非法 / 端点不存在 / 上游响应无法解析。

    这类错误**换任何出口都会得到同样的结果**，所以绝不能冷却出口、
    更不能重试 —— 否则一轮无脑重试会把整个出口池全部打上冷却标记，
    让一次坏请求瘫痪整池。
    """


class PayloadFormatError(Exception):
    """上游拒绝了请求体的**编码方式**（该用 JSON 却发了 multipart，或反之）。

    这不是确定性失败 —— 换一种编码通常立刻就能成功。所以调用方应当在
    **同一个出口**上换编码重试：既不该冷却出口，也不该切换出口。
    """


class TaskFailedAfterSubmit(Exception):
    """任务已成功提交到上游，但轮询阶段失败 / 超时 / 任务本身失败。

    此时**配额已经消耗**，重试等于再烧一次配额，所以直接失败不重试。
    """


class AllExitsCooling(Exception):
    """所有出口都在冷却中，短期内无法继续。"""


class ExitPool:
    """出口池：按顺序取可用出口；配额耗尽 / 网络失败时切换并冷却。"""

    def __init__(self, exits: Iterable[Exit] | None = None,
                 switch_node: bool = True, mode: str | None = None):
        if exits is not None:
            self.exits = list(exits)
            auto_mode = "system"          # 显式传入（测试/自定义池）按 system 处理
        else:
            self.exits, auto_mode = load_default_exits()
        self.mode = mode or auto_mode
        self.switch_node = switch_node
        self._lock = threading.Lock()
        self._rr = 0
        self._current_node: str | None = None
        log.info("出口池初始化: mode=%s, %d 个出口", self.mode, len(self.exits))

    # -- clash 节点切换 --------------------------------------------------

    def _set_node(self, node: str) -> bool:
        if not self.switch_node or not node:
            return True
        if self._current_node == node:
            return True
        if self.mode == "mihomo":
            api, selector, delay = MIHOMO_API, MIHOMO_GROUP, 1.2   # 本机进程，切换快
        else:
            api, selector, delay = CLASH_API, CLASH_SELECTOR, 2.5
        try:
            r = requests.put(
                f"{api}/proxies/{selector}",
                json={"name": node},
                timeout=8,
                proxies={"http": None, "https": None},  # 回环请求绝不走代理
            )
            if r.status_code in (200, 204):
                self._current_node = node
                time.sleep(delay)  # 给连接池/握手一点时间
                return True
            log.warning("切节点失败 [%s] %s: HTTP %s %s",
                        selector, node, r.status_code, r.text[:120])
        except Exception as e:  # noqa: BLE001
            log.warning("切节点异常 [%s] %s: %s", selector, node, e)
        return False

    def restore_node(self) -> None:
        """把 selector 还原为「自动选择」。

        自带 mihomo 是**本服务独占**的实例，还原没有意义（也不影响别的服务），
        所以 mihomo 模式下直接跳过。
        """
        if not self.switch_node or self.mode == "mihomo":
            return
        try:
            requests.put(
                f"{CLASH_API}/proxies/{CLASH_SELECTOR}",
                json={"name": "自动选择"},
                timeout=8,
                proxies={"http": None, "https": None},
            )
            self._current_node = "自动选择"
        except Exception:  # noqa: BLE001
            pass

    # -- 出口选择 --------------------------------------------------------

    def pick(self, exclude: set[str] | None = None) -> Exit:
        """
        轮转取一个可用出口。

        allow_cooldown=False（默认）时，如果所有出口都在冷却则抛 QuotaExhausted，
        不再强行复用 —— 避免配额耗尽后白跑一轮重试。
        """
        exclude = exclude or set()
        with self._lock:
            n = len(self.exits)
            for i in range(n):
                idx = (self._rr + i) % n
                ex = self.exits[idx]
                if ex.name in exclude:
                    continue
                if ex.available:
                    self._rr = (idx + 1) % n
                    return ex
        raise AllExitsCooling(
            f"所有 {len(self.exits)} 个出口都在冷却中，最早恢复需 "
            f"{max(0, round(min(e.cooldown_until for e in self.exits) - time.time()))}s"
        )

    def activate(self, ex: Exit) -> bool:
        """让指定出口生效（切换 clash 节点 + 探测出口 IP）。"""
        if ex.node:
            self._set_node(ex.node)
        # 探测出口 IP，失败重试一次（换节点后握手可能没就绪）
        for attempt in range(2):
            try:
                ip = self.probe_ip(ex, timeout=25 if attempt else 15)
                if ip:
                    ex.egress_ip = ip
                    return True
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    log.debug("出口 %s 探测重试: %s", ex.name, e)
        return False

    def probe_ip(self, ex: Exit, timeout: int = 20) -> str:
        proxies = {"http": ex.proxy, "https": ex.proxy} if ex.proxy else {"http": None, "https": None}
        for url in ("https://api.ipify.org", "https://ipinfo.io/ip"):
            try:
                r = requests.get(url, timeout=timeout, proxies=proxies)
                if r.status_code == 200:
                    ip = r.text.strip()
                    # ipinfo 返回纯 IP，ipify 也是；过滤掉 HTML 之类
                    if ip and len(ip) <= 64 and "\n" not in ip:
                        return ip
            except Exception:  # noqa: BLE001
                continue
        return ""

    def refresh_all_ips(self, limit: int | None = None) -> dict[str, str]:
        """逐个激活并探测出口的真实出口 IP（供 /v1/exits 展示）。

        订阅模式下出口动辄几十个，全量探测要好几分钟；因此支持 `limit`
        —— 只探前 N 个，其余保持「未探测」，等真正轮到时再懒探测。
        """
        targets = self.exits if limit is None else self.exits[:limit]
        out: dict[str, str] = {}
        for e in targets:
            if self.activate(e):
                out[e.name] = e.egress_ip
            else:
                out[e.name] = ""
        self.restore_node()
        return out

    def healthy_exits(self) -> list[Exit]:
        return [e for e in self.exits if e.available]


# ---------------------------------------------------------------- 图片工具


def _mime_of(path: str) -> str:
    m = mimetypes.guess_type(path)[0]
    return m or "application/octet-stream"


def to_data_url(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def load_image_bytes(src: str | bytes, timeout: int = 60, proxies=None) -> tuple[bytes, str]:
    """图片输入统一成 (bytes, mime)。src 支持本地路径 / http(s) URL / dataURL / 裸 base64。"""
    if isinstance(src, bytes):
        return src, "image/jpeg"
    s = src.strip()
    if s.startswith("data:"):
        head, _, b64 = s.partition(",")
        mime = head[5:].split(";")[0] or "image/jpeg"
        return base64.b64decode(b64), mime
    if s.startswith("http://") or s.startswith("https://"):
        r = requests.get(s, timeout=timeout, proxies=proxies)
        r.raise_for_status()
        mime = r.headers.get("Content-Type", "").split(";")[0] or _mime_of(s)
        return r.content, mime
    if os.path.exists(s):
        with open(s, "rb") as f:
            return f.read(), _mime_of(s)
    # 当作裸 base64
    try:
        return base64.b64decode(s), "image/jpeg"
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"无法解析图片输入: {type(src)}") from e


# ---------------------------------------------------------------- 核心调用


@dataclass
class GenResult:
    model: str
    kind: str                  # "image" | "video"
    url: str
    job_id: str = ""
    exit_name: str = ""
    egress_ip: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PrunaClient:
    def __init__(self, pool: ExitPool | None = None, verbose: bool = False):
        self.pool = pool or ExitPool()
        self.verbose = verbose

    # ---- 底层 POST：multipart 强制 ----

    def _post(self, url: str, *, fields: dict, files: list, proxies,
              timeout: int = 180, session: requests.Session | None = None):
        """
        Pruna 的 /generate 走 multipart。
        ⚠️ 没有图片时也必须塞一个占位文件字段，否则服务端按 JSON 解析并 500。
        ⚠️ 传入 session 时复用它（cookie 要延续到后续的 status 查询）。
        """
        files = list(files)
        if not files:
            files.append(("__force_multipart", ("", b"", "application/octet-stream")))
        if session is not None:
            return session.post(url, data=fields, files=files, timeout=timeout)
        return requests.post(url, data=fields, files=files, proxies=proxies, timeout=timeout)

    @staticmethod
    def _check_limit(r: requests.Response) -> None:
        if r.status_code == 429:
            detail = ""
            try:
                d = r.json()
                detail = str(d.get("details") or d.get("error") or "")
            except Exception:  # noqa: BLE001
                detail = r.text[:200]
            raise QuotaExhausted("?", detail)
        if r.status_code >= 500:
            txt = r.text[:300]
            # 上游把我们的请求体按错误的方式解析了（multipart ⇄ JSON）。
            # 这是编码问题，换编码就能好 —— 绝不能当成瞬时故障去冷却出口。
            if any(s in txt for s in ("not valid JSON", "Unexpected token",
                                      "in JSON at position", "after minus sign")):
                raise PayloadFormatError(
                    "请求体编码方式被上游拒绝（multipart/JSON 用错了）。原始响应: " + txt)

    #: 上游把「参数/模型问题」也包装成 HTTP 500，这些特征串说明问题不在出口
    _PARAM_ERROR_MARKERS = (
        "input validation failed",
        "matches none of the enum values",
        "invalid property",
        "MODEL_DISABLED",
        "This model is temporarily unavailable",
    )

    @staticmethod
    def _is_param_error(text: str) -> bool:
        """5xx 响应体是否其实是「参数/模型问题」。

        实测（2026-09）：给 p-video-2 传非法 resolution，上游返回的是
        **HTTP 500** + `{"error":"property input validation failed: ... matches none of
        the enum values"}`。若按「5xx=瞬时故障」处理，会在每个出口上重试一轮，
        把整个出口池冷却掉 —— 这类错误必须判为确定性错误。
        """
        t = (text or "")[:600]
        return any(m in t for m in PrunaClient._PARAM_ERROR_MARKERS)

    @staticmethod
    def _http_error(r: requests.Response, tag: str = "") -> Exception:
        """按状态码把 HTTP 错误分类。

        - **4xx（429 已单独处理）** → `DeterministicError`：参数非法 / 端点不存在 /
          模型不存在。换出口重试结果完全相同，必须直接失败。
        - **5xx** → `RuntimeError`：上游瞬时故障，值得换一个出口重试。
          ⚠️ 例外：上游把「参数校验失败」「模型已下线」也包成 500，
             这类要判成 `DeterministicError`，否则会重试到把出口池冷却光。
        """
        msg = f"HTTP {r.status_code}{' ' + tag if tag else ''}: {r.text[:300]}"
        if 400 <= r.status_code < 500:
            return DeterministicError(msg)
        if PrunaClient._is_param_error(r.text):
            return DeterministicError(msg)
        return RuntimeError(msg)

    @staticmethod
    def _extract_url(payload: Any) -> str:
        """从 status 的 output 字段抽视频/图片 URL，形态 str/dict/list 不定。"""
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload if payload.startswith("http") else ""
        if isinstance(payload, list):
            for item in payload:
                u = PrunaClient._extract_url(item)
                if u:
                    return u
            return ""
        if isinstance(payload, dict):
            for key in ("generation_url", "url", "video", "image", "output", "result", "file"):
                if key in payload:
                    u = PrunaClient._extract_url(payload[key])
                    if u:
                        return u
            for v in payload.values():
                u = PrunaClient._extract_url(v)
                if u:
                    return u
        return ""

    def _poll(self, model: str, job_id: str, proxies, poll_interval: int = 5,
              timeout: int = 900, session: requests.Session | None = None) -> dict:
        """轮询任务状态。

        ⚠️ 必须复用提交时那个 session —— 上游要求「提交」与「查询」共享会话，
           否则 status 一律返回 404（表现为一直轮询到超时）。
        ⚠️ 非 200 响应**不能静默跳过**（旧实现就是这么写的，导致 404 被无视、
           `last` 始终为 {}，白等到 900s）。这里会记录并做有限容错。
        """
        deadline = time.time() + timeout
        last: dict = {}
        misses = 0
        caller = session or requests
        kw: dict = {} if session is not None else {"proxies": proxies}

        while time.time() < deadline:
            r = caller.get(status_url(model, job_id), timeout=60, **kw)

            if r.status_code == 200:
                misses = 0
                last = r.json()
                if last.get("error"):
                    # 刚提交完的瞬间可能查不到，重试几次再放弃
                    if "Status request failed" in str(last.get("error")):
                        time.sleep(poll_interval)
                        continue
                    raise RuntimeError(f"查询任务失败: {last['error']}")
                st = (last.get("status") or "").lower()
                if st in ("completed", "succeeded", "success", "done"):
                    return last
                if st in ("failed", "error", "canceled", "cancelled"):
                    raise RuntimeError(f"生成失败: {json.dumps(last, ensure_ascii=False)[:300]}")
            else:
                misses += 1
                body = (r.text or "")[:200]
                last = {"http_status": r.status_code, "body": body}
                if r.status_code == 404:
                    # 会话丢失的典型信号：连续 404 就别再空耗到超时了
                    if misses >= 6:
                        raise RuntimeError(
                            f"任务 {job_id} 状态查询连续 {misses} 次 404 —— 上游要求提交与查询"
                            f"共享会话(cookie)，持续出现说明会话没保持住。body={body}"
                        )
                elif r.status_code >= 500:
                    log.warning("status 查询 %s 异常: HTTP %s %s", job_id, r.status_code, body)
                else:
                    log.warning("status 查询 %s: HTTP %s %s", job_id, r.status_code, body)

            time.sleep(poll_interval)

        raise TimeoutError(f"轮询超时（{timeout}s），最后状态: {json.dumps(last, ensure_ascii=False)[:200]}")

    # ---- 生成主入口 ----

    def generate(
        self,
        model: str,
        prompt: str | None = None,
        images: list[str | bytes] | None = None,
        video: str | bytes | None = None,
        params: dict[str, Any] | None = None,
        last_frame: str | bytes | None = None,
        *,
        on_exit_switch=None,
        poll_timeout: int = 900,
    ) -> GenResult:
        """
        提交一次生成，自动处理出口切换：
          1. 从池中取出口 → 激活（切 clash 节点）
          2. POST /generate
          3. 429 → 标记该出口配额耗尽，切下一个出口重试
          4. 200 → 轮询到完成，返回结果 URL
        """
        if model not in ALL_MODELS:
            raise ValueError(f"未知模型 {model}，可用: {', '.join(ALL_MODELS)}")

        images = list(images or [])
        params = dict(params or {})

        # 预先把图片读成 bytes 并做 dataURL（图片类模型用 dataURL 传）
        img_bytes: list[tuple[bytes, str]] = []
        for src in images:
            img_bytes.append(load_image_bytes(src))

        kb_bytes: list[tuple[bytes, str]] = []
        if video is not None:
            kb_bytes.append(load_image_bytes(video))

        # 首尾帧：尾帧单独走 last_frame_image 槽位（不能混进 images，否则只会取第一张）
        lf_bytes: tuple[bytes, str] | None = None
        if last_frame is not None:
            lf_bytes = load_image_bytes(last_frame)

        tried: set[str] = set()
        last_err: Exception | None = None
        max_tries = max(3, len(self.pool.exits) * 2)

        for attempt in range(max_tries):
            try:
                # 排除已试过的；若都排除完了（即所有出口都试过一轮），
                # 就放开排除集重来（可能已过冷却）
                excl = tried if len(tried) < len(self.pool.exits) else None
                ex = self.pool.pick(exclude=excl)
            except AllExitsCooling as e:
                raise RuntimeError(
                    f"所有出口配额均已耗尽或不可用，请稍后重试（{e}）"
                ) from e
            tried.add(ex.name)
            ok = self.pool.activate(ex)
            proxies = {"http": ex.proxy, "https": ex.proxy} if ex.proxy else {
                "http": None, "https": None}
            log.info("→ 出口 %s (node=%s, ip=%s, activated=%s)", ex.name, ex.node, ex.egress_ip, ok)
            if not ok:
                # 切节点或探测出口 IP 失败 —— 这个出口大概率用不了，直接跳过，
                # 省掉一次注定失败的提交（也就省了一次配额风险）。
                log.warning("出口 %s 激活/探测失败，跳过并冷却 60s", ex.name)
                ex.penalize(60)
                last_err = RuntimeError(f"出口 {ex.name} 激活失败")
                continue
            if on_exit_switch:
                try:
                    on_exit_switch(ex, attempt)
                except Exception:  # noqa: BLE001
                    pass

            try:
                # 请求体编码：先按模型默认，若上游拒收就在**同一个出口**换另一种
                # （PayloadFormatError 是编码问题，跟出口无关，不该冷却或切换出口）
                default_json = model in JSON_MODELS
                res = None
                for use_json in (None, not default_json):
                    try:
                        res = self._submit_once(model, prompt, img_bytes, kb_bytes, params,
                                                proxies, use_json=use_json, lf_bytes=lf_bytes)
                        break
                    except PayloadFormatError as e:
                        eff = default_json if use_json is None else use_json
                        log.warning("出口 %s 请求体用 %s 被拒，换编码重试: %s",
                                    ex.name, "JSON" if eff else "multipart", e)
                        last_err = e
                if res is None:
                    # 两种编码都被拒 —— 没法继续，但依然不冷却出口
                    raise DeterministicError(f"请求体两种编码都被上游拒绝: {last_err}")
                res.exit_name = ex.name
                res.egress_ip = ex.egress_ip
                return res
            except QuotaExhausted as e:
                log.warning("出口 %s 配额耗尽（%s），切换到下一个", ex.name, e.detail[:80])
                ex.penalize(3600)  # 配额类：冷却 1 小时
                last_err = e
                continue
            except DeterministicError as e:
                # 参数非法 / 端点不存在 / 响应无法解析 —— 换出口结果完全相同。
                # ⚠️ 关键：**不冷却任何出口**。否则一轮无脑重试会把 8 个出口
                #    全部打上冷却标记，一次坏请求就能瘫痪整个池。
                log.error("确定性错误（不冷却出口、不重试）: %s", e)
                raise
            except TaskFailedAfterSubmit as e:
                # 任务已经提交到上游，配额已消耗；重试 = 再烧一次额度
                log.error("任务已提交但失败，不重试（避免二次消耗配额）: %s", e)
                raise
            except (requests.RequestException, RuntimeError, TimeoutError) as e:
                log.warning("出口 %s 瞬时故障（%s），冷却 180s 并切换", ex.name, e)
                ex.penalize(180)
                last_err = e
                continue

        raise RuntimeError(f"所有出口均失败，最后错误: {last_err}")

    def _build_multipart(self, model, prompt, img_bytes, kb_bytes, params, lf_bytes=None):
        """构造 multipart 字段（视频模型 + p-image-try-on 用）。"""
        fields: dict[str, str] = {"model": model}
        files: list[tuple] = []

        pf = PROMPT_FIELD.get(model, "prompt")
        if prompt:
            fields[pf] = prompt

        for k, v in params.items():
            if v is None:
                continue
            fields[k] = ("true" if v else "false") if isinstance(v, bool) else str(v)

        # ---- 图片传参 ----
        field_name = IMAGE_FIELD.get(model)
        if field_name == "garment_images":
            # 试穿：第一张是模特，其余是服装
            # ⚠️ 2026-09 上游把模特图字段从 image 改成了 person_image ——
            #    仍传 image 会报 "person_image is required"
            for i, (b, m) in enumerate(img_bytes[1:]):
                files.append(("garment_images", (f"g_{i}.{m.split('/')[-1]}", b, m)))
            if img_bytes:
                b, m = img_bytes[0]
                files.append(("person_image", (f"person.{m.split('/')[-1]}", b, m)))
        elif field_name == "images":
            for i, (b, m) in enumerate(img_bytes):
                files.append(("images", (f"img_{i}.{m.split('/')[-1]}", b, m)))
        elif field_name == "image":
            if img_bytes:
                b, m = img_bytes[0]
                files.append(("image", (f"img_0.{m.split('/')[-1]}", b, m)))

        # 尾帧（首尾帧生成）—— 独立槽位，必须叫 last_frame_image
        if lf_bytes:
            b, m = lf_bytes
            files.append(("last_frame_image", (f"last.{m.split('/')[-1]}", b, m)))

        if kb_bytes:
            b, m = kb_bytes[0]
            files.append(("video", (f"src.{m.split('/')[-1] or 'mp4'}", b, m)))
        return fields, files

    def _build_json(self, model, prompt, img_bytes, kb_bytes, params, lf_bytes=None) -> dict:
        """构造 JSON body（4 个 p-image 系模型用，图片以 dataURL 传）。"""
        body: dict[str, Any] = {"model": model}
        pf = PROMPT_FIELD.get(model, "prompt")
        if prompt:
            body[pf] = prompt
        body.update({k: v for k, v in params.items() if v is not None})

        field_name = IMAGE_FIELD.get(model) or "image"
        if img_bytes:
            if field_name == "garment_images":
                # 试穿：模特图字段是 person_image（2026-09 由 image 改名）
                body["garment_images"] = [to_data_url(b, m) for b, m in img_bytes[1:]]
                body["person_image"] = to_data_url(*img_bytes[0])
            elif field_name == "images":
                body["images"] = [to_data_url(b, m) for b, m in img_bytes]
            else:
                body["image"] = to_data_url(*img_bytes[0])
        if lf_bytes:
            body["last_frame_image"] = to_data_url(*lf_bytes)
        if kb_bytes:
            body["video"] = to_data_url(*kb_bytes[0])
        return body

    def _submit_once(self, model, prompt, img_bytes, kb_bytes, params, proxies,
                     use_json: bool | None = None, lf_bytes=None) -> GenResult:
        """提交一次。

        `use_json=None` 时按模型默认编码：4 个 p-image 系走 JSON body
        （图片以 dataURL 传），其余模型走 multipart。调用方可以显式指定，
        用于在收到 `PayloadFormatError` 后换编码重试。
        """
        is_image = model in IMAGE_MODELS
        if use_json is None:
            use_json = model in JSON_MODELS

        # ⚠️ 上游要求「提交」与「状态查询」共享会话（cookie）——
        #    用裸 requests 调用会让 status 一律返回 404。用一个 Session 贯穿提交+轮询。
        session = requests.Session()
        if proxies:
            session.proxies.update({k: v for k, v in proxies.items() if v})

        if use_json:
            body = self._build_json(model, prompt, img_bytes, kb_bytes, params, lf_bytes)
            r = session.post(generate_url(model), json=body, timeout=180)
            tag = f"{model} JSON"
        else:
            fields, files = self._build_multipart(model, prompt, img_bytes, kb_bytes,
                                                  params, lf_bytes)
            r = self._post(generate_url(model), fields=fields, files=files,
                           proxies=proxies, session=session)
            tag = f"{model} multipart"

        self._check_limit(r)
        if r.status_code >= 400:
            raise self._http_error(r, tag)
        d = r.json()

        # 图片模型：同步返回
        if "imageUrl" in d or (is_image and "jobId" not in d and "id" not in d):
            url = d.get("imageUrl") or self._extract_url(d)
            if not url:
                raise DeterministicError(
                    f"图片响应无 URL: {json.dumps(d, ensure_ascii=False)[:300]}")
            return GenResult(model, "image", url, raw=d)

        # 视频模型：拿 job id 轮询
        job_id = d.get("jobId") or d.get("id") or d.get("job_id") or ""
        if not job_id:
            url = self._extract_url(d)
            if url:
                return GenResult(model, "video", url, raw=d)
            raise DeterministicError(
                f"响应无 jobId: {json.dumps(d, ensure_ascii=False)[:300]}")

        try:
            st = self._poll(model, job_id, proxies, timeout=900, session=session)
        except (RuntimeError, TimeoutError) as e:
            # 已经提交成功、配额已消耗 —— 绝不重试，否则会再烧一次额度
            raise TaskFailedAfterSubmit(f"任务 {job_id} 提交后失败: {e}") from e
        url = self._extract_url(st.get("output")) or self._extract_url(st)
        if not url:
            raise TaskFailedAfterSubmit(
                f"任务 {job_id} 完成但没有 URL: {json.dumps(st, ensure_ascii=False)[:300]}")
        return GenResult(model, "video", url, job_id=job_id, raw=st)

    # ---- 下载成品 ----

    @staticmethod
    def download(url: str, dest: str, proxies=None, timeout: int = 600) -> str:
        """把成品下载到本地路径。沿用能访问 Pruna 的那个出口。"""
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with requests.get(url, stream=True, timeout=timeout, proxies=proxies) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        return dest
