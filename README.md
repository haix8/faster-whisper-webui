# 声迹 · Faster-Whisper WebUI

一个面向内网的音视频异步转写工作台。单个容器同时提供 WebUI、FastAPI、
SQLite 持久任务队列和单任务转写 Worker。

## 功能

- 上传音频或视频，或粘贴抖音分享链接（支持整段分享口令、一次粘贴多个链接批量创建），选择模型和语言；
- 每个任务可选填写术语/提示词（人名、产品名等），引导 Whisper 降低专有名词错字，不改写转写原文；
- 后台排队，不需要停留在上传页面；
- 任务状态、阶段、近似进度、耗时和错误；
- 取消、失败重试、删除；
- 同一任务只接受一个上传流，删除文件失败时任务保持可见并可重试；
- 正文预览及 JSON、TXT、SRT、VTT 下载，字幕按词级时间戳和语义边界生成；
- 任务详情可下载源文件：上传任务为原始文件，链接任务为无水印视频（下载
  失败或转写失败但源文件已就位的任务同样可下载）；
- SQLite、源文件、模型缓存和结果持久化；
- CPU 与 NVIDIA CUDA 两种容器构建；
- GPU 不可用时可配置为明确停队列，不静默回退。

当前不包含身份认证、OpenClaw、说话人分离、翻译和摘要。外部 URL 下载仅限
抖音分享链接，不扩展其他平台。取消采用协作式检查：下载、预处理和分段转写
可及时停止；首次模型下载或模型加载本身不能被安全中断，但加载结束后会立即
再次检查取消请求。

## 快速启动：CPU

```bash
cp .env.example .env
docker compose up --build -d
```

打开 `http://服务器地址:8000`。

默认 Compose 使用两个命名卷：

- `whisper-data`：SQLite、源文件和结果；
- `whisper-models`：Hugging Face/CTranslate2 模型。

首次使用某个模型时会自动下载。可以把 `.env` 中的 `DEFAULT_MODEL` 改为
`tiny`，降低第一次验证的下载和运行开销。

## 抖音链接转写

页面「粘贴链接」模式下粘贴抖音分享链接（`v.douyin.com` 短链、完整链接或整段
分享口令）即可创建任务；每行一个链接可批量创建多个任务。视频由服务器下载，
下载完成后自动进入与上传任务相同的转写流程；任务显示名会更新为视频标题，
详情中可查看来源链接。

抖音接口存在平台风控，偶发解析失败属正常现象。服务端已做以下处理：

- 优先走官方详情接口（`aweme/v1/web/aweme/detail/`，自动维护匿名会话标识
  ttwid，实测无需签名与登录态）；失败后回退分享页 SSR 解析（完整分享 URL
  带参数优先、裸分享页兜底）；
- 多 UA 指纹轮换、指数退避加重试；识别 WAF JS Challenge 风控壳页与 403/429，
  命中时使用分钟级退避并明确报错，避免短间隔重试加重风控；分享页请求带
  最小间隔节流；
- 解析/下载失败且未达最大尝试次数（`MAX_TASK_ATTEMPTS`）时自动重新排队自愈，
  超限后才标记失败；
- 可在「粘贴链接」页签可选填写抖音 Cookie（登录态），用于进一步降低风控命中率。
  登录态 Cookie 为 HttpOnly、浏览器无法自动读取，需登录后在开发者工具的 Network
  面板复制 `Cookie:` 请求头整段（可达数 KB）再粘贴。Cookie 只保存在当前浏览器
  （`localStorage`），随任务创建传给服务端并仅用于该任务的解析/下载请求；
  **API 不返回 Cookie 明文**（仅暴露是否已设置的标记），Cookie 不进入日志，
  删除任务时随记录删除。Cookie 可能失效或触发平台风控，失效时按无 Cookie 模式
  继续。注意：服务当前没有身份认证，Cookie 按浏览器隔离而非账号隔离，同浏览器
  切换使用者会复用上一份填写值，请自行保管凭证。

安全边界：下载链路只允许抖音及其 CDN 域名（`douyin.com`、`iesdouyin.com`、
`snssdk.com`、`douyinvod.com`、`xhscdn.com` 及字节系 CDN 落点域名后缀匹配）、
强制 HTTPS、重定向链逐跳校验域名与解析结果拒绝私网地址，并按
`MAX_UPLOAD_BYTES` 限制下载大小。需要服务器能访问公网。

## NVIDIA CUDA

宿主机需要：

- 可用的 NVIDIA 驱动；
- Docker Engine；
- NVIDIA Container Toolkit；
- `docker run --gpus all ... nvidia-smi` 能正常执行。

启动：

```bash
docker compose -f compose.yaml -f compose.cuda.yaml up --build -d
```

CUDA 配置强制使用 `TRANSCRIPTION_DEVICE=cuda`。如果容器检测不到 GPU，
Worker 会显示不可用并保持任务排队，不会悄悄切换到 CPU。

验证：

```bash
docker compose -f compose.yaml -f compose.cuda.yaml exec whisper-webui nvidia-smi
curl http://127.0.0.1:8000/api/system/status
```

## Mac 与 Apple GPU

CPU 镜像支持 `linux/arm64`，因此可以在 Apple Silicon 的 OrbStack 中构建并运行。

OrbStack 运行的是 Linux 容器，不能把 macOS Metal 直接透传给容器。因此：

- OrbStack 容器可以验证全部 Web、任务、文件和 CPU 转写链路；
- Linux NVIDIA 服务器使用 CUDA 镜像；
- Apple GPU 加速需要后续接入运行在 macOS 原生环境中的
  `mlx-whisper` 或 `whisper.cpp` Worker。

推理代码已经通过 `app/backends/base.py` 的接口隔离，增加原生后端不需要修改
上传、SQLite 队列、WebUI 或结果格式。当前版本不会把 Mac 容器 CPU 运行误报为
Metal 加速。

## 主要配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TRANSCRIPTION_DEVICE` | `cpu`（Compose） | `cpu`、`cuda`、`auto` |
| `TRANSCRIPTION_COMPUTE_TYPE` | `auto` | CPU 自动 `int8`，CUDA 自动 `int8_float16` |
| `TRANSCRIPTION_MODELS` | `tiny,base,small,medium,large-v3,turbo` | 页面允许选择的模型 |
| `DEFAULT_MODEL` | `small` | 页面默认模型 |
| `DEFAULT_LANGUAGE` | `auto` | 页面默认语言；已知语言时固定语言可提升识别稳定性 |
| `TRANSCRIPTION_INITIAL_PROMPT_ZH` | 空 | 中文转写风格提示，可用于引导模型输出自然标点 |
| `MAX_UPLOAD_BYTES` | `2147483648` | 单文件最大字节数 |
| `MAX_MEDIA_SECONDS` | `28800` | 最大媒体时长 |
| `MIN_FREE_BYTES` | `1073741824` | 低于此剩余空间时拒绝上传 |
| `LOCAL_FILES_ONLY` | `false` | 为 `true` 时禁止在线下载模型 |

生产环境建议固定镜像和模型缓存，不使用无人值守方式自动升级 NVIDIA 驱动。
镜像安装使用 `requirements.lock` 与 `requirements-tools.lock` 中经过容器验证的
完整依赖版本；调整顶层依赖时需同步重新生成并验证锁文件。

## 数据目录

```text
/data/
├── db/app.sqlite3
└── tasks/<uuid>/
    ├── source.<ext>        # 上传文件，或抖音链接任务下载的 source.mp4
    └── results/
        ├── transcript.json
        ├── transcript.txt
        ├── transcript.srt
        └── transcript.vtt
/models/
```

任务内部路径只使用 UUID。用户上传的原始文件名仅用于页面显示和下载文件名；
链接任务在下载成功后把视频标题更新为显示名。上传请求先在 SQLite 中原子取得
占用权，再写入请求独有的临时文件；链接下载使用独立的 `.download` 临时文件，
成功后原子替换。服务重启会清理未完成的临时文件。删除采用两阶段状态，文件
清理异常不会把任务永久隐藏。

## API

运行后访问 `/api/docs` 查看 OpenAPI 页面。主要接口：

- `POST /api/tasks` 创建任务（`file_name` 或 `source_url` 二选一；可选
  `initial_prompt` 术语提示词、链接任务可选 `douyin_cookie`）；
- `PUT /api/tasks/{id}/source` 以原始请求体上传文件；
- `GET /api/tasks` 查询任务列表；
- `GET /api/tasks/{id}` 查询详情；
- `POST /api/tasks/{id}/cancel` 取消；
- `POST /api/tasks/{id}/retry` 重试；
- `DELETE /api/tasks/{id}` 删除；
- `GET /api/tasks/{id}/source` 下载任务源文件（上传原文件 / 链接下载的无水印视频）；
- `GET /api/tasks/{id}/artifacts/{kind}` 下载结果；
- `GET /api/system/status` 查询 Worker、设备、队列和磁盘。

## 开发与测试

推荐直接使用容器中的 Python 3.11 环境：

```bash
docker build -t faster-whisper-webui:test .
docker run --rm \
  --user root \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/workspace" \
  -w /workspace \
  faster-whisper-webui:test \
  sh -c "pip install -r requirements-dev.txt && pytest -q -p no:cacheprovider"
```

代码检查：

```bash
ruff check app tests
ruff format --check app tests
node --check app/static/app.js
node --check app/static/upload-id.js
node --test tests/test_*.js
```

完整目标和验收标准见 [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md)。
