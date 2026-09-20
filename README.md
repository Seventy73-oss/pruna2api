# pruna2api

把 [Pruna Playground](https://playground.pruna.ai) 的图片 / 视频生成能力包装成 **OpenAI 兼容 API** 的反代服务。

内置**多出口 IP 轮换**：上游的免费额度按出口 IP 独立计算，所以「换出口 IP」等价于「重置额度」。
本服务自带一个 mihomo 实例，直接用你的代理订阅节点做出口池 —— 订阅里有几个节点，就有几份额度。

> ⚠️ 仅供学习和自用。请遵守上游服务条款，不要用于滥用或商业转售。

---

## 特性

- **OpenAI 兼容**：图片接口完整兼容（`size` / `n` / `response_format`），视频走 Sora 风格路径，错误体统一为 OpenAI 顶层形状
- **自带 mihomo 出口池**：systemd 独立单元，节点从订阅自动拉取，换节点即换出口 IP
- **错误分层**：区分「确定性错误 / 编码错误 / 配额耗尽 / 瞬时故障」，**一次坏请求不会打瘫整个出口池**
- **请求体编码自适应**：上游对不同模型要求 JSON 或 multipart，用错会被拒 —— 服务会自动换编码重试
- **成品本地转存**：上游 URL 会过期，服务下载后返回自己的 URL
- **Web 控制台**：5 页（概览 / 生成 / 任务日志 / 出口池 / API），亮暗双主题，可调字号
- **首尾帧支持**：`p-video-2-pro` 的首帧 + 尾帧双槽位
- 无前端构建步骤：控制台是**单个 `ui.html`**，改了刷新即生效

---

## 快速开始

### 1. 依赖

- Linux（x86_64）+ systemd
- Python 3.11+
- FFmpeg（可选，仅用于自检脚本抽帧）

### 2. 部署服务

```bash
# 拉代码
git clone https://github.com/<you>/pruna2api.git /opt/pruna2api
cd /opt/pruna2api

# 建 venv 装依赖
python3 -m venv venv
./venv/bin/pip install fastapi "uvicorn[standard]" requests pydantic

# 建数据目录
mkdir -p data media log

# 装 systemd 单元（按需修改 User / 路径）
sudo cp pruna2api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pruna2api
```

打开 `http://<你的主机>:3020/` 即可看到控制台。

### 3. 配置出口池（可选但强烈建议）

没有出口池也能跑，但只能吃单 IP 的免费额度。配上订阅后额度按节点数倍增。

```bash
# 放 mihomo 二进制（从 https://github.com/MetaCubeX/mihomo/releases 下载 linux-amd64 版）
mkdir -p /opt/pruna2api/mihomo/{log,providers}
cp mihomo /opt/pruna2api/mihomo/ && chmod +x /opt/pruna2api/mihomo/mihomo

# 装 mihomo 单元
sudo cp mihomo-pruna.service /etc/systemd/system/
sudo cp pruna2api-mihomo.conf /etc/systemd/system/pruna2api.service.d/mihomo.conf
sudo systemctl daemon-reload
sudo systemctl enable --now mihomo-pruna
```

然后在控制台的「出口池」页把**订阅链接**粘进去，点「保存并重载」—— 服务会自己生成 mihomo 配置、热重载、重建出口池。

> 订阅需要是 **clash 格式**（多数机场的订阅带上 `&flag=clash` 参数即是）。
> mihomo 的 `proxy-providers` 只会拉取 clash yaml，base64 分享链接需要先转换。

---

## API

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/videos/generations` | 提交生成（视频 / 图片模型通用） |
| POST | `/v1/videos` | 同上（OpenAI Sora 风格别名） |
| POST | `/v1/images/generations` | 图片生成（完整 OpenAI 参数） |
| GET | `/v1/tasks/{id}` | 查单个任务（status 为 OpenAI 语义） |
| GET | `/v1/tasks?limit=&status=` | 列任务 |
| GET | `/v1/models` | 列模型与各自免费额度 |
| GET | `/v1/exits` | 出口池状态（IP / 冷却 / 失败次数） |
| POST | `/v1/exits/probe?limit=N` | 探测出口真实 IP |
| GET | `/v1/mihomo/subscription` | 订阅状态（**脱敏**，不回显完整链接） |
| POST | `/v1/mihomo/subscription` | 写入订阅 → 重建配置 → 热重载 |
| POST | `/v1/mihomo/reload` | 重新拉取订阅节点 |
| GET | `/files/{name}` | 下载成品 |
| GET | `/health` | 健康检查 |
| GET | `/` | Web 控制台 |

### 请求体

```jsonc
{
  "model": "p-video-2-pro",     // 见 GET /v1/models
  "prompt": "文本提示词",
  "image": "…",                 // 单张参考图；p-video-2-pro 下同时是「首帧」
  "last_frame": "…",            // 尾帧（首尾帧生成，见下节）
  "images": ["…", "…"],         // 多张参考图（仅 p-image-edit / p-video-edit 有效）
  "video": "…",                 // 视频转视频的源视频
  "aspect_ratio": "16:9",
  "resolution": "768p",
  "duration": 5,
  "mode": "speed",              // speed | quality
  "prompt_upsampler": "turbo",  // off | turbo | max
  "seed": 42,
  "wait": true                  // true=阻塞到完成；false=立刻返回 task_id
}
```

**图片来源支持 4 种形式**（任意混用）：dataURL、远程 URL、裸 base64、服务器本地路径。

### OpenAI 兼容

```bash
# 图片接口支持 OpenAI 标准参数
curl -X POST http://127.0.0.1:3020/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"p-image","prompt":"a wide cinematic landscape",
       "size":"1792x1024","n":1,"response_format":"url"}'
```

```
size              → 自动映射成上游的宽高比（1792x1024 → 16:9）
n                 → 生成张数（上限 4，每张各扣一次额度）
response_format   → "url" 或 "b64_json"
```

响应是 OpenAI 形状：

```jsonc
{
  "created": 1789787002,
  "model": "p-image",
  "status": "completed",
  "data": [{"url": "http://127.0.0.1:3020/files/task_xxx.jpg"}],
  "exit": {"name": "hk01", "egress_ip": "203.0.113.10"}   // 非标准，额外信息
}
```

错误统一为顶层 `{"error": {"message", "type", "param", "code"}}`。

### 首尾帧（`p-video-2-pro`）

首尾帧**不是多图**，是上游的两个独立槽位：

| 槽位 | 上游字段 |
|---|---|
| 首帧 | `image` |
| 尾帧 | `last_frame_image` |

```bash
curl -X POST http://127.0.0.1:3020/v1/videos/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"p-video-2-pro","prompt":"smooth transition",
       "image":"data:image/png;base64,<首帧>",
       "last_frame":"data:image/png;base64,<尾帧>",
       "duration":5,"resolution":"480p"}'
```

> ⚠️ 尾帧**不能**塞进 `images` 数组 —— 该数组只有第一张会被消费，尾帧会静默丢失。

---

## 架构

```
        ┌──────────────────────────────────────────────┐
        │  Web 控制台 (ui.html)  ·  5 页 · 亮暗双主题   │
        └───────────────────┬──────────────────────────┘
                            │  REST
        ┌───────────────────▼──────────────────────────┐
        │  pruna2api  (FastAPI :3020)                  │
        │   ├─ 任务队列 + SQLite 持久化                 │
        │   ├─ 出口池轮换 / 冷却 / 错误分层              │
        │   └─ 成品转存到本地盘                          │
        └───┬──────────────────────────────┬───────────┘
            │ 提交 / 轮询                   │ 切节点
            ▼                              ▼
   ┌──────────────────┐        ┌──────────────────────┐
   │ mihomo :7891     │◄───────│ 控制面 :9091          │
   │ (proxy-providers)│        │ PUT /proxies/<group>  │
   └────────┬─────────┘        └──────────────────────┘
            │
            ▼  你的订阅节点（N 个 = N 份独立额度）
   ┌──────────────────┐
   │ playground.pruna │
   └──────────────────┘
```

**出口池规模**：订阅里有多少可用节点，就有多少份额度。
例如 50 个节点、单节点 5 次/天的模型 → 理论 250 次/天。

---

## 逆向要点（改代码前必读）

接入这个上游踩了几个坑，都固化在代码里了：

### 1. 请求体编码分两套，用错必挂

| 模型 | 编码 |
|---|---|
| `p-image` / `p-image-ideogram` / `p-image-edit` / `p-image-upscale` | **JSON body**（图片走 dataURL） |
| `p-image-try-on` + 全部视频模型 | **multipart/form-data** |

用错时上游返回 **HTTP 500**，错误体是：

```json
{"error":"No number after minus sign in JSON at position 1 (line 1 column 2)"}
```

这是上游把 multipart 的 `--`（boundary 前缀）当 JSON 数字解析了。
代码做了两层防护：`JSON_MODELS` 静态映射定默认编码，捕获 `PayloadFormatError` 后**在同一出口换编码重试**。

### 2. 错误分类决定出口池会不会被一次坏请求打瘫

早期版本把所有异常都当「瞬时故障」→ 冷却出口 + 换下一个重试。
但里面混着**确定性错误**（4xx 参数非法、响应解析失败）—— 换 8 个出口结果一样，
却把 8 个出口全打上冷却，**一个空 prompt 请求能让整个服务瘫痪几分钟**。

现在的分层：

| 异常 | 触发 | 处理 |
|---|---|---|
| `QuotaExhausted` | HTTP 429 | 冷却该出口 1 小时，切下一个 |
| `PayloadFormatError` | 上游说请求体解析失败 | **同出口换编码重试**，不冷却 |
| `DeterministicError` | 4xx（非 429）/ 响应解析失败 | **不冷却任何出口**，直接失败 |
| `TaskFailedAfterSubmit` | 已提交但轮询失败 | **不重试**（重试 = 再烧一次额度） |
| 瞬时故障 | 网络异常 / 5xx / 超时 | 冷却该出口 3 分钟 |

⚠️ `DeterministicError` / `TaskFailedAfterSubmit` **绝不能继承 `RuntimeError`**，
否则会被 `except RuntimeError` 抢先捕获，分层失效。

### 3. 字段名不统一

- 提示词：多数是 `prompt`，但 `p-video-animate` / `p-video-replace` 用 `instruction_prompt`
- 参考图：`p-video-replace` 必须**单数** `image`；`p-video-edit` / `p-image-edit` 用**复数** `images`；`p-image-try-on` 用 `garment_images`
- 尾帧：`last_frame_image`（见上）

### 4. 多图 ≠ 首尾帧

真正消费多图的只有 `p-image-edit` 和 `p-image-try-on`。
**所有视频模型的 `images` 数组只取第一张**。首尾帧是独立能力，走两个专属槽位。

### 5. 订阅里的伪节点

不少机场把「剩余流量：xxx GB / 套餐到期：xxx / 距离下次重置」当作**节点**塞进 `proxies`。
它们会被当成出口且连不通，必须用 `exclude-filter` 排掉：

```yaml
proxy-groups:
  - name: PROXY
    type: select
    use: [sub]
    exclude-filter: "剩余流量|套餐到期|重置|官网|订阅|到期|流量"
```

> Go 的 regexp **不支持负向先行断言** `(?!...)`，所以只能用 `exclude-filter`，不能用 `filter` 反向排除。

---

## 配置

所有配置走环境变量（见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRUNA_PORT` | `3020` | 监听端口 |
| `PRUNA_DATA_DIR` | `/opt/pruna2api/data` | SQLite 与状态 |
| `PRUNA_MEDIA_DIR` | `/opt/pruna2api/media` | 成品目录 |
| `PRUNA_API_KEY` | 空 | 空 = 不校验；**对公网开放时必须设** |
| `PRUNA_PUBLIC_BASE` | 空 | 空 = 按请求 Host 拼 |
| `PRUNA_MAX_WORKERS` | `4` | 并发任务数 |
| `PRUNA_EXIT_SOURCE` | `mihomo` | `mihomo` = 订阅节点池；`system` = 固定出口 |
| `PRUNA_MIHOMO_API` | `http://127.0.0.1:9091` | 自带 mihomo 控制面 |
| `PRUNA_MIHOMO_PROXY` | `http://127.0.0.1:7891` | 自带 mihomo 代理入口 |
| `PRUNA_MIHOMO_GROUP` | `PRUNA` | 切换用的 proxy-group 名 |

> 自带 mihomo 拿不到节点时会**自动回退**到 `system` 模式，并把内部 `mode` 一并切换
> —— 保证切节点不会打到错误的控制面。

---

## 开发

```bash
# 改完前端先自检（标签平衡 / id 唯一性 / 页面清单 / JS 语法）
./venv/bin/python check_ui.py

# 端到端首尾帧验证（需要 FFmpeg）
python test_firstlast.py http://127.0.0.1:3020
```

`check_ui.py` 会兜住六类低级错误：标签不平衡、关键 id 缺失、**id 重复**（最难查）、
页面清单不符、导航与页面不匹配、JS 语法。

---

## 已知限制

- `p-video-edit` 可能长时间卡在 `processing`，服务侧 15 分钟超时后标失败
- 视频模型传多张参考图只消费第一张（上游行为）
- 免费额度随上游策略变化，`MODEL_QUOTA` 只是快照
- 成品文件无自动清理，长期使用需自行归档 `media/`

---

## License

MIT
