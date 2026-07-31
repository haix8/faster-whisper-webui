# AGENTS.md — whisper

本文件是“声迹 · Faster-Whisper WebUI”的项目级 Agent 工作指引。除非子目录存在更近层级的 `AGENTS.md`，本文件适用于整个项目。所有命令默认从项目根目录执行。

## 1. 项目定位

- 这是一个面向内网的音视频异步转写服务。
- 一个容器同时提供 FastAPI、原生 WebUI、SQLite 持久任务队列和单个转写 Worker。
- 推理后端为 `faster-whisper`；媒体由 `ffprobe` 校验并通过 `ffmpeg` 标准化。
- `/data` 持久化数据库、源文件和结果，`/models` 持久化模型缓存。
- Linux NVIDIA 生产环境使用 CUDA；普通 Linux 与 OrbStack 容器使用 CPU。
- 当前明确不包含身份授权、OpenClaw 接入、外部 URL 下载、说话人分离、翻译和摘要。除非用户明确改变范围，不要顺手引入 Redis、Celery、WebSocket、PostgreSQL、Nginx 或前端框架。

架构和验收标准以 `IMPLEMENTATION_SPEC.md` 为基线，实际运行命令与配置以当前代码、Compose、`.env` 和运行时状态为准。

## 2. 关键目录与入口

- `app/main.py`：FastAPI 应用、API 路由、生命周期和静态资源入口。
- `app/config.py`：Pydantic Settings 与环境变量契约。
- `app/db.py`：SQLite Schema、任务状态转换、原子领取和重启恢复。
- `app/worker.py`：单 Worker 调度、心跳、取消、转写和结果发布流程。
- `app/storage.py`：任务目录、上传临时文件、结果路径和路径逃逸防护。
- `app/media.py`：`ffprobe` 校验与 `ffmpeg` 音频标准化。
- `app/formatters.py`：JSON、TXT、SRT、VTT 结果生成与原子发布。
- `app/domain.py`：状态、阶段、结果和运行时领域对象。
- `app/backends/base.py`：推理后端契约。
- `app/backends/faster_whisper.py`：生产推理实现、模型缓存、设备解析和中文提示。
- `app/backends/fake.py`：自动化测试后端。
- `app/static/`：无构建步骤的 HTML、CSS、JavaScript WebUI。
- `tests/`：数据库、API、存储、格式、推理参数和前端契约测试。
- `compose.yaml`：CPU/通用服务定义。
- `compose.cuda.yaml`：CUDA 覆盖配置。
- `Dockerfile`、`Dockerfile.cuda`：CPU 与 NVIDIA CUDA 镜像。

## 3. 不得破坏的运行时不变量

### 3.1 SQLite 与任务状态

- SQLite 任务表是队列和任务状态的唯一真相源，不维护第二套内存队列。
- Worker 必须继续通过事务原子领取最早的 `queued` 任务；不得先查询再无条件更新。
- 默认只有一个 Worker、一个运行任务，避免多模型并发挤满显存。
- 状态和阶段必须保持合法流转：

```text
uploading -> queued -> running -> succeeded
     |          |         |
     +----------+-------> failed
queued/running -> cancelled
failed/cancelled -> queued
可删除状态 -> deleting -> deleted
                     \-> delete_failed
```

- 取消是协作式的：预处理和分段转写期间检查取消；模型首次下载或加载不能伪装成可立即中断。
- 重启恢复必须覆盖未完成上传、运行中任务、取消请求和删除中任务，不能产生永久不可领取的记录。
- 新增字段或状态时，要同步 Schema、序列化、列表/详情、操作权限、恢复逻辑和测试。现有 SQLite 没有独立迁移框架，Schema 变更必须显式处理历史数据库兼容，不能只修改 `CREATE TABLE IF NOT EXISTS`。

### 3.2 上传、文件与结果

- 同一任务只能有一个上传请求取得写入权；每个请求使用独立 `.upload` 临时文件。
- 用户文件名只用于显示，不能直接参与服务器路径拼接。
- 任务 ID 必须经过 UUID 规范化，所有路径必须验证仍位于 `/data` 内。
- 上传完成前要校验大小、SHA-256、音轨、媒体时长和可用磁盘。
- 结果必须先完整写入临时文件并原子发布，之后才能把数据库状态标记为 `succeeded`。
- JSON 是权威结果；TXT、SRT、VTT 必须由同一份标准化分段结果生成。
- 删除任务采用两阶段状态；文件清理失败时记录必须保持可见并允许重试。
- `/data` 与 `/models` 是用户持久数据。禁止在普通构建、测试、升级或 UI 修复中删除、重建或清空对应命名卷。

### 3.3 推理与设备

- API、Worker 和 WebUI 通过 `TranscriptionBackend` 契约隔离推理实现；增加新后端时不要把平台判断散落到路由或前端。
- `TRANSCRIPTION_DEVICE=cuda` 表示 CUDA 不可用时 Worker 保持不可用并停止领取任务，不能静默回退 CPU。
- `TRANSCRIPTION_DEVICE=auto` 才允许在 CUDA 不可用时选择 CPU。
- 用户只选择模型和语言，不直接选择硬件；任务详情记录实际设备与计算类型。
- 切换模型前必须释放旧模型，避免瞬时同时占用两份显存。
- 中文初始提示仅用于显式选择 `zh` 的任务；`auto` 检测不能被当成必然启用中文提示。
- 中文标点规范化不能修改其他语言。不要用不可审计的全局字符串替换“修正”专业词。
- Whisper 不能保证专有名词零错误。若实现热词或术语纠正，必须做成明确的任务输入/配置，并保留原始转写与可验证规则。

### 3.4 WebUI 与轮询

- 前端保持原生 HTML/CSS/JavaScript，不增加 Node 运行时、外部字体、图标 CDN 或 SPA 框架。
- 上传进度由浏览器上报；任务阶段和转写进度来自服务端。
- 活跃任务可高频轮询，空闲时降频；不要为了“实时”引入 WebSocket，除非需求和架构重新确认。
- 轮询结果未变化时不得重建任务 DOM，避免 hover、焦点和页面位置抖动。
- 并发请求必须防止旧响应覆盖新的筛选、搜索或分页结果。
- 详情刷新必须保留弹窗、正文和时间轴滚动位置。
- 时间轴保持独立限高滚动，不让长音频把弹窗扩展成超长页面。
- 修改 `app.js`、`styles.css` 或 `upload-id.js` 时，同步更新 `index.html` 中三个静态资源的统一版本参数，并更新前端契约测试。

## 4. 默认实施方式

- 从用户给出的接口、错误、任务 ID、页面现象或服务器状态开始，不做无边界探索。
- 先沿真实链路定位：浏览器请求 → API → SQLite 状态 → Worker → ffmpeg/faster-whisper → 结果文件。
- 小范围修复直接实施；涉及状态机、Schema、持久卷、并发、删除、部署或 GPU 运行时的变更，实施前先说明不变量、影响和回滚。
- 优先复用现有模块，不为局部需求重写队列或引入新的基础设施。
- 保持 API、任务 JSON 和四种结果格式兼容；改变契约时同步更新 README、实施目标和测试。
- 不提交真实 `.env`、模型、媒体、数据库、缓存或测试产物。
- 当前目录可能没有 Git 元数据；执行提交、分支、回滚等 Git 操作前先确认 `git rev-parse --is-inside-work-tree`，不能假定仓库状态或声称已提交。

## 5. 自动化验证

### 5.1 前端

```bash
node --check app/static/app.js
node --check app/static/upload-id.js
node --test tests/test_*.js
```

页面交互变更还要用真实浏览器检查相应行为。轮询问题至少观察一个完整空闲轮询周期；弹窗问题要验证长正文、长时间轴、背景滚动和移动端尺寸。

### 5.2 Python、API 与代码质量

本机未必安装 `pytest` 和 `ruff`，默认使用项目镜像执行：

```bash
docker build -t faster-whisper-webui:test .
docker run --rm \
  --user root \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/workspace:ro" \
  -w /workspace \
  faster-whisper-webui:test \
  sh -lc '
    pip install --disable-pip-version-check -q -r requirements-dev.txt &&
    pytest -q -p no:cacheprovider &&
    ruff check --no-cache app tests &&
    ruff format --check --no-cache app tests
  '
```

- 只读源码挂载下必须给 Ruff 使用 `--no-cache`。
- 不要把 FakeBackend 通过测试等同于真实 faster-whisper 推理通过。
- 数据库或状态机变更至少覆盖原子领取、并发上传、取消、重试、删除失败和重启恢复。
- 文件变更至少覆盖路径穿越、原子写入和失败清理。

### 5.3 OrbStack 容器验收

- Apple Silicon 上可构建并运行 CPU 镜像，验证 Web、API、SQLite、上传、结果和重启恢复。
- OrbStack 的 Linux 容器不能使用 macOS Metal；CPU 成功不能宣称 Apple GPU 成功。
- 临时烟测优先使用唯一命名卷，并在同一执行链路中停止容器、删除精确测试卷。
- 应用以 UID `10001` 运行；root 所有的 `/data`、`/models` tmpfs 默认不可写。不要重复使用会造成权限误判的 root-owned tmpfs 烟测方式。
- 真实媒体烟测创建的任务应有明确测试名称；只删除本次 Agent 自己创建的测试任务，不删除用户任务。

## 6. 验证矩阵

| 变更类型 | 最低验证 |
|---|---|
| HTML/CSS/JS | Node 检查 + 前端契约测试 + 真实浏览器关键交互 |
| API/校验 | Pytest + 实际 HTTP 请求与错误语义 |
| SQLite/队列 | 状态机测试 + 并发/恢复场景 |
| 存储/格式 | 路径与原子写测试 + 四种结果回读 |
| faster-whisper 参数 | 单元测试参数传递 + 真实音视频转写 |
| Docker/依赖 | CPU 镜像构建和健康检查 |
| CUDA/模型 | 容器内 GPU 检测 + 真实 CUDA 任务 + 显存观察 |
| 生产部署 | 上线前空闲检查 + 健康/就绪/配置/系统状态 + 日志与回滚镜像 |

## 7. dayu-server 生产部署

生产信息可能变化，以下仅作为当前项目运行手册；每次操作都要重新读取实际状态。

- SSH 别名：`dayu-server`
- 服务目录：`/home/dayu/services/faster-whisper-webui`
- 内网地址：`http://192.168.0.52:8000/`
- Compose：`compose.yaml` + `compose.cuda.yaml`
- 持久卷：`faster-whisper-webui_whisper-data`、`faster-whisper-webui_whisper-models`
- 当前生产基线：CUDA `int8_float16`，允许 `tiny,small,medium,turbo`，默认 `turbo` / `zh`，`LOCAL_FILES_ONLY=true`

### 7.1 上线前只读检查

```bash
ssh dayu-server '
  cd /home/dayu/services/faster-whisper-webui &&
  docker compose -f compose.yaml -f compose.cuda.yaml ps &&
  docker inspect faster-whisper-webui \
    --format "image={{.Image}} health={{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}"
'

curl -fsS http://192.168.0.52:8000/readyz
curl -fsS http://192.168.0.52:8000/api/config
curl -fsS http://192.168.0.52:8000/api/system/status
```

- 确认 `queued=0`、`running=0` 且 Worker 空闲后再重建容器；若有活动任务，等待完成或取得用户明确允许中断。
- 记录当前镜像 ID，并为本次发布建立新的、可辨识的回滚 tag。
- 核对服务器 `.env`，但不要在日志或交付内容中泄露敏感配置。

### 7.2 部署原则

- 只同步本次范围内的源码、测试和文档；保留服务器 `.env`。
- 不执行 `docker compose down -v`、`docker volume rm`、全局 `docker system prune` 或任何会删除数据/模型卷的操作。
- 如果新增模型且生产为 `LOCAL_FILES_ONLY=true`，先以受控方式把完整模型缓存写入模型卷并验证，再把模型暴露给页面；不能让首个用户任务承担在线下载。
- 构建和重建：

```bash
ssh dayu-server '
  cd /home/dayu/services/faster-whisper-webui &&
  docker compose -f compose.yaml -f compose.cuda.yaml build &&
  docker compose -f compose.yaml -f compose.cuda.yaml up -d --force-recreate
'
```

- 环境变量只在确有配置变更时修改；生产默认值与仓库 `.env.example` 可以不同，不能用样例文件覆盖生产 `.env`。

### 7.3 上线后验证

```bash
curl -fsS http://192.168.0.52:8000/readyz
curl -fsS http://192.168.0.52:8000/api/config
curl -fsS http://192.168.0.52:8000/api/system/status

ssh dayu-server '
  docker inspect faster-whisper-webui \
    --format "health={{.State.Health.Status}} restarts={{.RestartCount}} image={{.Image}}" &&
  docker logs --since 10m faster-whisper-webui
'
```

- 推理、模型、CUDA、ffmpeg 或依赖变更必须再跑一个真实 CUDA 音视频任务，核对 `model_name`、`language_detected`、`device=cuda`、`compute_type=int8_float16` 和结果文件。
- 用 `nvidia-smi` 确认推理进程与显存；完成后确认 Worker 回到空闲、队列清零、容器无重启。
- 浏览器相关变更必须在生产页面重新验证，不能只看静态资源是否返回 200。
- 测试任务完成后只清理本次创建的测试记录和媒体，并确认用户原有任务数量未变化。

### 7.4 回滚

- 使用本次部署前创建的回滚 tag，不要默认历史 `cuda-ui-v014` 永远是最新回滚点。
- 回滚只替换 `faster-whisper-webui:cuda` 镜像并重建容器，保持 `.env`、数据库卷和模型卷不变。
- 回滚后重复 `/readyz`、`/api/config`、`/api/system/status`、容器健康和关键页面检查。

## 8. CUDA 故障边界

- `nvidia-smi` 失败时先区分宿主机驱动、内核模块、容器 Toolkit 和 CTranslate2，不要先改应用代码。
- `Failed to initialize NVML: Driver/library version mismatch` 通常表示 NVIDIA 用户态库与当前已加载内核模块不一致。至少核对：

```bash
nvidia-smi
cat /proc/driver/nvidia/version
modinfo nvidia | grep '^version:'
test -e /var/run/reboot-required && cat /var/run/reboot-required
docker exec faster-whisper-webui nvidia-smi
docker exec faster-whisper-webui python -c \
  'import ctranslate2; print(ctranslate2.get_cuda_device_count())'
```

- NVIDIA 包升级、驱动切换或自动更新策略属于宿主机维护，不是普通应用部署步骤；变更前要说明服务中断与回滚。
- 不把 `nvidia-smi` 成功等同于真实推理成功，也不把 CPU 推理成功等同于 CUDA 成功。

## 9. 安全与数据边界

- 当前无身份认证是明确的内网产品决定，不代表可以安全暴露到公网。改变监听、端口映射、反向代理或网络边界前先确认访问范围。
- `.env` 不提交；密钥、代理凭据和服务器敏感信息不写入源码、测试、README 或日志。
- 容器保持非 root、只读根文件系统、`cap_drop: ALL` 和 `no-new-privileges`；放宽安全配置必须有明确运行时证据。
- 删除、清卷、批量清理任务、模型或镜像前解析精确目标。用户任务和结果默认不可删除。
- 不因磁盘清理顺手执行 `docker system prune`；先确认具体镜像、容器、卷是否属于本项目以及是否可回滚。

## 10. 文档与交付

- 产品范围或架构决策改变时更新 `IMPLEMENTATION_SPEC.md`。
- 配置、启动、模型、端口或部署方式改变时更新 `README.md`、`.env.example` 和 Compose。
- API、状态、结果格式或前端交互改变时同步更新相应测试。
- 不为小修复新增过程文档；长期有效的运行手册和业务不变量才写入仓库。

交付时至少说明：

- 改了什么以及关键文件。
- 执行了哪些自动化、容器、浏览器或 CUDA 验证，结果如何。
- 是否改变环境变量、模型缓存、SQLite Schema、持久卷或部署步骤。
- 未执行的验证、剩余限制和回滚方式。
- 如果当前目录不是 Git 仓库，明确说明没有提交记录，不暗示代码已提交或推送。
