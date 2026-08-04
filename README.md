# 声迹 · Faster-Whisper WebUI

一个面向内网的音视频异步转写工作台。单个容器同时提供 WebUI、FastAPI、
SQLite 持久任务队列和单任务转写 Worker。

## 功能

- 上传音频或视频，选择模型和语言；
- 后台排队，不需要停留在上传页面；
- 任务状态、阶段、近似进度、耗时和错误；
- 取消、失败重试、删除；
- 同一任务只接受一个上传流，删除文件失败时任务保持可见并可重试；
- 正文预览及 JSON、TXT、SRT、VTT 下载，字幕按词级时间戳和语义边界生成；
- SQLite、源文件、模型缓存和结果持久化；
- CPU 与 NVIDIA CUDA 两种容器构建；
- GPU 不可用时可配置为明确停队列，不静默回退。

当前不包含身份认证、OpenClaw、外部 URL 下载、说话人分离、翻译和摘要。
取消采用协作式检查：预处理和分段转写可及时停止；首次模型下载或模型加载本身
不能被安全中断，但加载结束后会立即再次检查取消请求。

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
    ├── source.<ext>
    └── results/
        ├── transcript.json
        ├── transcript.txt
        ├── transcript.srt
        └── transcript.vtt
/models/
```

任务内部路径只使用 UUID。用户上传的原始文件名仅用于页面显示和下载文件名。
上传请求先在 SQLite 中原子取得占用权，再写入请求独有的临时文件；服务重启会
清理未完成上传。删除采用两阶段状态，文件清理异常不会把任务永久隐藏。

## API

运行后访问 `/api/docs` 查看 OpenAPI 页面。主要接口：

- `POST /api/tasks` 创建任务；
- `PUT /api/tasks/{id}/source` 以原始请求体上传文件；
- `GET /api/tasks` 查询任务列表；
- `GET /api/tasks/{id}` 查询详情；
- `POST /api/tasks/{id}/cancel` 取消；
- `POST /api/tasks/{id}/retry` 重试；
- `DELETE /api/tasks/{id}` 删除；
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
