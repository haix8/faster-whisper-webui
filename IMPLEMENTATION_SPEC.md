# Faster-Whisper WebUI 实施目标

## 1. 最终目标

交付一个可直接部署的转写服务：

- 一个 Docker 容器提供 WebUI、HTTP API、任务调度和转写 Worker；
- 内网用户上传音频或视频，或粘贴抖音分享链接，立即得到异步任务；
- 用户可以选择允许的 Whisper 模型和语言；
- 任务列表展示排队、处理阶段、近似进度、耗时、错误和结果；
- 结果可预览并下载 JSON、TXT、SRT、VTT；
- 支持取消、失败重试和删除；
- 容器重启后任务与结果不丢失；
- Linux NVIDIA 使用 CUDA，其他容器环境可以使用 CPU；
- 不接 OpenClaw，不做身份授权。

## 2. 固定技术方案

| 项目 | 决定 |
|---|---|
| Web/API | FastAPI，同时提供静态 WebUI |
| 前端 | 原生 HTML、CSS、JavaScript，不增加 Node 运行时 |
| 数据库 | SQLite WAL |
| 队列 | SQLite 任务表是唯一真相源，单 Worker 原子领取 |
| 推理 | faster-whisper 1.2.1 |
| 媒体 | ffprobe 校验，ffmpeg 统一为 16kHz 单声道 WAV |
| 链接下载 | 仅抖音分享链接（`app/douyin.py`）：短链重定向解析 → 官方详情接口（匿名 ttwid 会话）/分享页 SSR 取无水印播放地址 → 流式下载；域名白名单、HTTPS、重定向逐跳与公网 IP 校验 |
| 进度 | 浏览器上传进度 + Worker 阶段进度 + 转写时间轴估算 |
| 更新 | 活跃任务每 2 秒轮询，空闲时降低频率 |
| 部署 | CPU 与 CUDA 使用不同镜像目标，但每次部署均只有一个容器 |
| 持久化 | `/data` 保存 SQLite、源文件和结果，`/models` 保存模型缓存 |

不引入 PostgreSQL、Redis、Celery、WebSocket、Nginx、用户系统、
说话人分离、翻译或摘要。外部 URL 下载仅限抖音分享链接，不扩展其他平台。

## 3. 硬件与加速边界

提供三种设备策略：

- `TRANSCRIPTION_DEVICE=cpu`：强制 CPU；
- `TRANSCRIPTION_DEVICE=cuda`：必须检测到 CUDA，否则 Worker 保持不可用且不领取任务；
- `TRANSCRIPTION_DEVICE=auto`：检测到 CUDA 时使用 CUDA，否则使用 CPU。

用户不直接选择硬件，只选择模型。任务详情显示实际使用的设备和计算类型。

部署产物：

- CPU 镜像：`linux/amd64`、`linux/arm64`，可在 Mac OrbStack 中运行；
- CUDA 镜像：`linux/amd64`，通过 NVIDIA Container Toolkit 使用显卡；
- Apple Metal 不能从 OrbStack 的 Linux 容器透传。代码必须通过推理后端接口隔离，
  为后续原生 macOS `mlx-whisper` 或 `whisper.cpp` Worker 保留接入点，但不伪装成
  当前 Docker 容器已经支持 Metal。

## 4. 任务状态与恢复

状态：

```text
uploading(upload -> receiving_upload) -> queued -> running -> succeeded
        |                              |         |
        +--------------------------> failed <----+
                                       |
queued/running -> cancelled
failed/cancelled -> queued（手动重试）
queued/succeeded/failed/cancelled -> deleting -> deleted
                                      |
                                      +-> delete_failed -> deleting（重试）
```

链接任务（`source_url` 非空）创建后直接入队，没有 `uploading` 阶段；下载是
`running` 下的 `downloading` 阶段（`running` 合法阶段序列：
`downloading -> preprocessing -> transcribing -> writing_results -> complete`），
下载完成后 `source_path` 与媒体元数据写入任务记录，之后走与上传任务相同的链路。

Worker 使用 SQLite `BEGIN IMMEDIATE` 事务领取最早的 `queued` 任务。

重启恢复规则：

- `uploading`：标记为失败，删除未完成临时上传；
- `running`（含下载中断）：低于最大尝试次数时重新排队，否则标记失败；未完成
  下载的 `.download` 临时文件在启动时清理，重试即重新下载；
- `running/queued + cancel_requested`：直接完成取消，不重新入队；
- `deleting`：转为可见的 `delete_failed`，允许再次删除；
- `queued`：保持排队；
- 已完成、失败或取消任务保持原状态。

GPU 默认只允许一个运行任务，避免多模型并发占满显存。

## 5. 文件与结果

目录：

```text
/data/
├── db/app.sqlite3
└── tasks/<uuid>/
    ├── source.<ext>        # 上传文件或抖音下载的视频（链接任务为 source.mp4）
    ├── work/audio.wav
    └── results/
        ├── transcript.json
        ├── transcript.txt
        ├── transcript.srt
        └── transcript.vtt
/models/
```

约束：

- 上传分块写入 `.upload` 临时文件并计算 SHA-256；
- SQLite 原子授予单个上传请求占用权，每个请求使用独立临时文件，禁止同一任务并发覆盖；
- 用户文件名只作为显示信息，不参与内部路径拼接；
- ffprobe 必须确认存在音轨、媒体时长不超限；
- ffmpeg 使用参数数组调用，不经过 shell；
- 结果先写临时文件，全部成功后原子发布；
- 数据库只有在结果已发布后才能标记 `succeeded`；
- 删除只允许操作经过 UUID 校验且位于数据根目录下的任务目录。
- 删除采用 `deleting -> deleted/delete_failed` 两阶段状态，文件清理失败时记录保持可见并可重试。
- 任务级 `initial_prompt` 持久化并传入推理后端作为初始提示词（与全局中文提示
  合并），只影响生成，不产生替换规则，转写 JSON 始终是模型原始输出；
- 任务级 `douyin_cookie` 持久化仅用于本任务解析/下载请求头；**API 永不回显
  Cookie 明文**，只暴露 `has_douyin_cookie` 布尔标记；Cookie 不进入日志，删除
  任务时随记录删除；Cookie 失效时按无 Cookie 模式继续。

JSON 是权威结果，TXT、SRT、VTT 从同一份标准化分段结果生成。

生产推理必须开启词级时间戳，模型约 30 秒的原始解码窗口不能直接作为用户字幕。
标准字幕分段遵循以下规则：

- 时间取本段第一个词开始到最后一个词结束，按时间递增且不得重叠；
- 优先在句末标点和明显停顿处分段，其次选择逗号、分号等自然语义边界；
- 单段最短约 `5/6` 秒、目标 `2～5` 秒、最长 `7` 秒；
- 中文目标 `16～28` 字、硬上限 `32` 字，并以不超过 `9` 字/秒作为优先
  切分约束；连续语音本身超过阅读速度时，不删改正文或伪造重叠时间；
- SRT、VTT 中文每行最多 `16` 字、最多两行；其他语言按每行 `42` 字符排版；
- 词级对齐不可用时保留模型原始分段，不能用字符比例伪造精确时间戳。

## 6. API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/config` | 模型、语言和上传限制 |
| `POST` | `/api/tasks` | 创建任务：`file_name`（上传）或 `source_url`（抖音链接，二选一）；可选 `initial_prompt`（术语提示词）、`douyin_cookie`（仅链接任务） |
| `PUT` | `/api/tasks/{id}/source` | 流式上传原始文件并入队 |
| `GET` | `/api/tasks` | 分页、筛选、搜索任务 |
| `GET` | `/api/tasks/{id}` | 任务详情和结果预览 |
| `POST` | `/api/tasks/{id}/cancel` | 取消任务 |
| `POST` | `/api/tasks/{id}/retry` | 重试失败或取消任务 |
| `DELETE` | `/api/tasks/{id}` | 删除任务与文件 |
| `GET` | `/api/tasks/{id}/artifacts/{kind}` | 下载结果 |
| `GET` | `/api/system/status` | Worker、设备、模型、队列和磁盘状态 |
| `GET` | `/healthz` | 进程存活 |
| `GET` | `/readyz` | 数据库和存储就绪 |

## 7. WebUI

页面包含：

- 拖拽或选择多个音视频文件，或切换「粘贴链接」粘贴抖音分享链接/口令；
  链接模式支持一次粘贴多个链接，每行一个，批量创建任务；
- 模型、语言选择；可选任务级术语/提示词（`initial_prompt`，仅引导生成不改写结果）；
  链接模式可选填写抖音 Cookie（登录态 Cookie 为 HttpOnly，由用户手动从开发者工具复制）；
- 每个文件独立上传并显示上传进度；
- 系统状态卡：Worker、设备、当前模型、排队数量；
- 任务筛选、搜索、分页；
- 状态、阶段、转写进度、媒体时长和耗时；
- 任务详情抽屉：全文、分段时间戳、错误和运行参数；
- 下载 JSON、TXT、SRT、VTT；
- 取消、重试和删除操作。

视觉方向采用“媒体工作台”：高信息密度但不拥挤，桌面优先并适配移动端，
不依赖外部字体、图标或 CDN。

## 8. 配置默认值

```text
TRANSCRIPTION_BACKEND=faster-whisper
TRANSCRIPTION_DEVICE=auto
TRANSCRIPTION_COMPUTE_TYPE=auto
TRANSCRIPTION_MODELS=tiny,base,small,medium,large-v3,turbo
DEFAULT_MODEL=small
MAX_UPLOAD_BYTES=2147483648
MAX_MEDIA_SECONDS=28800
MIN_FREE_BYTES=1073741824
WORKER_POLL_SECONDS=1
MAX_TASK_ATTEMPTS=2
```

CPU 自动使用 `int8`；CUDA 自动使用 `int8_float16`。

## 9. 验收标准

自动化验证：

- SQLite 原子领取、状态转换与重启恢复；
- 并发上传只能有一个请求取得占用权；
- 取消中的运行任务重启后进入取消态，不形成不可领取的队列任务；
- 慢媒体探测不阻塞健康检查和其他 API；
- 输出格式与时间戳；
- 文件名和路径穿越防护；
- 创建、上传、列表、详情、取消、重试、删除 API；
- 文件删除失败后保持可见并可重试；
- Worker 使用测试后端完成端到端任务；
- 链接任务：`source_url` 校验（白名单/HTTPS）、创建直接入队、下载失败可重试、
  下载阶段取消、下载文件原子落盘；
- 抖音解析健壮性：官方详情接口优先（匿名 ttwid 会话，无需签名）、分享页 SSR
  回退（完整分享 URL 带参数优先）、UA 指纹轮换、重试与退避、403/429 与 WAF
  JS Challenge 壳页识别（分钟级退避）、分享页请求节流、下载重定向链逐跳域名
  与公网 IP 校验；解析/下载失败在 `MAX_TASK_ATTEMPTS` 内自动重新排队自愈，
  超限才失败；
- 任务级 `initial_prompt` 的存储、透传与 zh 合并；`douyin_cookie` 的注入与
  API 明文脱敏（仅 `has_douyin_cookie` 标记）；
- 历史数据库升级：新增列自动补齐，旧库可读；
- 健康检查和系统状态。

OrbStack 验收：

- 构建 CPU 镜像并创建真实容器；
- 使用持久卷启动服务；
- 浏览器可打开 WebUI；
- 上传真实短音频完成一次 faster-whisper CPU 转写；
- 可预览文本并下载四种结果；
- 重启容器后任务仍存在；
- 验收结束后删除测试容器和临时媒体，保留项目源码与明确的数据卷。

CUDA 上线前验收：

- 容器内 CTranslate2 能检测 CUDA；
- `nvidia-smi` 能观察到推理进程和显存；
- 真实音视频完成转写；
- GPU 不可用时任务保持排队且页面明确显示不可用；
- 同时最多运行一个任务。

## 10. 完成定义

源码、测试、Dockerfile、Compose、配置样例和运行说明全部交付；
自动化测试通过；OrbStack CPU 容器完成真实端到端验证；独立代码审查中的
严重和重要问题已修复。CUDA 验证如受 dayu-server 当前环境限制，必须明确列出
尚未执行的验证，不能将 CPU 验收宣称为 GPU 验收。
