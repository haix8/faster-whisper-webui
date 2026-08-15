from __future__ import annotations

import hashlib
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import anyio
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.backends import create_backend
from app.backends.base import TranscriptionBackend
from app.config import Settings
from app.db import Database
from app.domain import MediaValidationError, TaskStatus
from app.douyin import extract_share_url, validate_source_url
from app.media import probe_media
from app.schemas import TaskCreate
from app.storage import ARTIFACT_FILES, Storage
from app.worker import TranscriptionWorker

logger = logging.getLogger(__name__)

LANGUAGES = [
    {"value": "auto", "label": "自动检测"},
    {"value": "zh", "label": "中文"},
    {"value": "en", "label": "英语"},
    {"value": "ja", "label": "日语"},
    {"value": "ko", "label": "韩语"},
    {"value": "yue", "label": "粤语"},
    {"value": "fr", "label": "法语"},
    {"value": "de", "label": "德语"},
    {"value": "es", "label": "西班牙语"},
    {"value": "ru", "label": "俄语"},
]

MODEL_DESCRIPTIONS = {
    "tiny": {"label": "Tiny", "hint": "最快，适合快速草稿"},
    "base": {"label": "Base", "hint": "轻量，适合清晰录音"},
    "small": {"label": "Small", "hint": "速度和质量均衡"},
    "medium": {"label": "Medium", "hint": "质量更高，资源占用较大"},
    "large-v3": {"label": "Large v3", "hint": "最高质量，显存需求高"},
    "turbo": {"label": "Turbo", "hint": "推荐，高质量与高速度"},
}


class Runtime:
    def __init__(
        self,
        settings: Settings,
        backend: TranscriptionBackend | None = None,
    ):
        self.settings = settings
        self.database = Database(settings.db_path)
        self.storage = Storage(settings.data_dir, settings.model_dir)
        self.backend = backend or create_backend(settings)
        self.worker = TranscriptionWorker(
            settings=settings,
            database=self.database,
            storage=self.storage,
            backend=self.backend,
        )


def create_app(
    settings: Settings | None = None,
    backend: TranscriptionBackend | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    runtime = Runtime(resolved_settings, backend)
    static_dir = Path(__file__).resolve().parent / "static"

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime.storage.initialize()
        runtime.database.initialize()
        recovery = runtime.worker.start()
        logger.info("worker recovery completed recovery=%s", recovery)
        application.state.runtime = runtime
        try:
            yield
        finally:
            runtime.worker.stop()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    application.state.runtime = runtime

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path == "/" or request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @application.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz")
    def ready() -> JSONResponse:
        database_ready = runtime.database.ping()
        storage_ready = runtime.storage.data_dir.is_dir() and os.access(
            runtime.storage.data_dir, os.R_OK | os.W_OK
        )
        ready_state = database_ready and storage_ready
        return JSONResponse(
            status_code=200 if ready_state else 503,
            content={
                "status": "ready" if ready_state else "not_ready",
                "database": database_ready,
                "storage": storage_ready,
            },
        )

    @application.get("/api/config")
    def get_config() -> dict[str, Any]:
        models = []
        for name in resolved_settings.model_names:
            description = MODEL_DESCRIPTIONS.get(
                name, {"label": name, "hint": "自定义模型"}
            )
            models.append({"value": name, **description})
        return {
            "models": models,
            "default_model": resolved_settings.default_model,
            "default_language": resolved_settings.default_language,
            "languages": LANGUAGES,
            "limits": {
                "max_upload_bytes": resolved_settings.max_upload_bytes,
                "max_media_seconds": resolved_settings.max_media_seconds,
            },
            "artifact_types": list(ARTIFACT_FILES),
        }

    @application.post("/api/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(
        payload: TaskCreate,
        response: Response,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", max_length=200
        ),
    ) -> dict[str, Any]:
        if payload.model not in resolved_settings.model_names:
            raise HTTPException(422, "所选模型未被服务允许")
        allowed_languages = {item["value"] for item in LANGUAGES}
        if payload.language not in allowed_languages:
            raise HTTPException(422, "不支持所选语言")
        if (
            payload.size_bytes is not None
            and payload.size_bytes > resolved_settings.max_upload_bytes
        ):
            raise HTTPException(413, "文件超过上传大小限制")
        if runtime.storage.free_bytes() < resolved_settings.min_free_bytes:
            raise HTTPException(507, "服务器可用磁盘空间不足")
        if payload.source_url:
            # 与 Worker 一致：先提取口令文本中的链接，再校验域名白名单。
            share_url = extract_share_url(payload.source_url) or (
                payload.source_url if payload.source_url.startswith("http") else None
            )
            if not share_url:
                raise HTTPException(422, "未找到抖音分享链接")
            try:
                validate_source_url(share_url)
            except MediaValidationError as error:
                raise HTTPException(422, str(error)) from error
            payload.source_url = share_url
            original_name = "抖音视频"
            extension = ".mp4"
            expected_size = None
        else:
            original_name = str(payload.file_name)
            try:
                extension = runtime.storage.source_extension(original_name)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            expected_size = payload.size_bytes

        task, created = runtime.database.create_task(
            original_name=original_name,
            source_extension=extension,
            content_type=payload.content_type,
            expected_size_bytes=expected_size,
            language_requested=payload.language,
            model_name=payload.model,
            max_attempts=resolved_settings.max_task_attempts,
            idempotency_key=idempotency_key,
            source_url=payload.source_url,
            initial_prompt=payload.initial_prompt,
            douyin_cookie=payload.douyin_cookie,
        )
        if created:
            runtime.storage.create_task_dir(task["id"])
        else:
            if not _same_create_request(task, payload):
                raise HTTPException(409, "Idempotency-Key 已被参数不同的创建请求使用")
            response.status_code = status.HTTP_200_OK
        return serialize_task(task, include_result=True)

    @application.put("/api/tasks/{task_id}/source")
    async def upload_source(task_id: str, request: Request) -> dict[str, Any]:
        canonical_id = require_task_id(task_id)
        task = await run_in_threadpool(runtime.database.get_task, canonical_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] != TaskStatus.UPLOADING:
            raise HTTPException(409, "任务当前不接受上传")
        acquired = await run_in_threadpool(runtime.database.begin_upload, canonical_id)
        if not acquired:
            raise HTTPException(409, "该任务已有上传请求正在处理")

        content_length = _optional_int(request.headers.get("content-length"))
        upload_id = str(uuid4())
        upload_path = runtime.storage.upload_path(canonical_id, upload_id)
        final_path = runtime.storage.source_path(
            canonical_id, str(task["source_extension"])
        )
        digest = hashlib.sha256()
        size = 0
        next_disk_check = 0
        try:
            if (
                content_length is not None
                and content_length > resolved_settings.max_upload_bytes
            ):
                raise HTTPException(413, "文件超过上传大小限制")

            async with await anyio.open_file(upload_path, "wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > resolved_settings.max_upload_bytes:
                        raise HTTPException(413, "文件超过上传大小限制")
                    if size >= next_disk_check:
                        free_bytes = await run_in_threadpool(runtime.storage.free_bytes)
                        if free_bytes < resolved_settings.min_free_bytes:
                            raise HTTPException(507, "服务器可用磁盘空间不足")
                        next_disk_check = size + 16 * 1024 * 1024
                    await handle.write(chunk)
                    digest.update(chunk)
                await handle.flush()
            await run_in_threadpool(_fsync_file, upload_path)

            if size == 0:
                raise HTTPException(422, "上传文件为空")
            expected_size = task.get("expected_size_bytes")
            if expected_size is not None and int(expected_size) != size:
                raise HTTPException(
                    422,
                    f"上传字节数与预期不一致：预期 {expected_size}，实际 {size}",
                )

            await run_in_threadpool(os.replace, upload_path, final_path)
            media = await run_in_threadpool(
                probe_media,
                final_path,
                resolved_settings.ffprobe_bin,
                resolved_settings.max_media_seconds,
            )
            source_path = await run_in_threadpool(
                runtime.storage.relative_path, final_path
            )
            updated = await run_in_threadpool(
                runtime.database.finish_upload,
                canonical_id,
                size_bytes=size,
                sha256=digest.hexdigest(),
                duration_seconds=media.duration_seconds,
                media_format=media.format_name,
                audio_codec=media.audio_codec,
                source_path=source_path,
            )
        except HTTPException as error:
            await run_in_threadpool(
                _cleanup_failed_upload,
                runtime.storage,
                canonical_id,
                upload_id,
                final_path,
            )
            await run_in_threadpool(
                runtime.database.fail_upload,
                canonical_id,
                _upload_error_code(error.status_code),
                str(error.detail),
            )
            raise
        except MediaValidationError as error:
            await run_in_threadpool(
                _cleanup_failed_upload,
                runtime.storage,
                canonical_id,
                upload_id,
                final_path,
            )
            await run_in_threadpool(
                runtime.database.fail_upload, canonical_id, error.code, str(error)
            )
            raise HTTPException(422, str(error)) from error
        except Exception as error:
            logger.exception("upload failed task_id=%s", canonical_id)
            await run_in_threadpool(
                _cleanup_failed_upload,
                runtime.storage,
                canonical_id,
                upload_id,
                final_path,
            )
            await run_in_threadpool(
                runtime.database.fail_upload,
                canonical_id,
                "UPLOAD_FAILED",
                _safe_error(error),
            )
            raise HTTPException(500, "上传处理失败") from error
        runtime.worker.wake()
        return serialize_task(updated, include_result=True)

    @application.get("/api/tasks")
    def list_tasks(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=100),
        task_status: str | None = Query(default=None, alias="status"),
        search: str | None = Query(default=None, alias="q", max_length=100),
    ) -> dict[str, Any]:
        if task_status:
            try:
                parsed = TaskStatus(task_status)
            except ValueError as error:
                raise HTTPException(422, "未知任务状态") from error
            if parsed is TaskStatus.DELETED:
                raise HTTPException(422, "不能查询已删除任务")
        items, total = runtime.database.list_tasks(
            page=page,
            page_size=page_size,
            status=task_status,
            search=search.strip() if search else None,
        )
        return {
            "items": [serialize_task(item) for item in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @application.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        task = runtime.database.get_task(require_task_id(task_id))
        if not task:
            raise HTTPException(404, "任务不存在")
        return serialize_task(task, include_result=True)

    @application.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, Any]:
        canonical_id = require_task_id(task_id)
        try:
            task = runtime.database.request_cancel(canonical_id)
        except KeyError as error:
            raise HTTPException(404, "任务不存在") from error
        runtime.worker.wake()
        return serialize_task(task, include_result=True)

    @application.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str) -> dict[str, Any]:
        canonical_id = require_task_id(task_id)
        task = runtime.database.get_task(canonical_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        source_path = task.get("source_path")
        has_source = bool(task.get("source_url")) or (
            source_path and runtime.storage.resolve_relative(str(source_path)).is_file()
        )
        if not has_source:
            raise HTTPException(409, "源文件不存在，无法重试")
        try:
            updated = runtime.database.retry_task(canonical_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        runtime.worker.wake()
        return serialize_task(updated, include_result=True)

    @application.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str) -> Response:
        canonical_id = require_task_id(task_id)
        task = runtime.database.get_task(canonical_id, include_deleted=True)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] == TaskStatus.DELETED:
            try:
                runtime.storage.delete_task(canonical_id)
            except Exception as error:
                logger.exception(
                    "legacy task file deletion failed task_id=%s", canonical_id
                )
                raise HTTPException(500, "文件清理失败，请重试删除") from error
            return Response(status_code=204)
        try:
            runtime.database.begin_delete(canonical_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        try:
            runtime.storage.delete_task(canonical_id)
        except Exception as error:
            logger.exception("task file deletion failed task_id=%s", canonical_id)
            runtime.database.fail_delete(canonical_id, _safe_error(error))
            raise HTTPException(500, "文件清理失败，可重试删除") from error
        try:
            runtime.database.finish_delete(canonical_id)
        except Exception as error:
            logger.exception("task delete finalization failed task_id=%s", canonical_id)
            raise HTTPException(500, "文件已清理，删除状态待恢复") from error
        return Response(status_code=204)

    @application.get("/api/tasks/{task_id}/artifacts/{kind}")
    def download_artifact(task_id: str, kind: str) -> FileResponse:
        canonical_id = require_task_id(task_id)
        task = runtime.database.get_task(canonical_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] != TaskStatus.SUCCEEDED:
            raise HTTPException(409, "任务尚未成功完成")
        if kind not in ARTIFACT_FILES:
            raise HTTPException(404, "结果类型不存在")
        path = runtime.storage.artifact_path(canonical_id, kind)
        if not path.is_file():
            raise HTTPException(404, "结果文件不存在")
        stem = Path(str(task["original_name"])).stem or "transcript"
        return FileResponse(
            path,
            media_type=_artifact_media_type(kind),
            filename=f"{stem}.{kind}",
        )

    @application.get("/api/system/status")
    def system_status() -> dict[str, Any]:
        disk = shutil.disk_usage(runtime.storage.data_dir)
        return {
            "worker": runtime.worker.snapshot(),
            "tasks": runtime.database.counts(),
            "disk": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "minimum_free_bytes": resolved_settings.min_free_bytes,
                "accepting_uploads": disk.free >= resolved_settings.min_free_bytes,
            },
            "configured_device": resolved_settings.transcription_device,
            "configured_backend": resolved_settings.transcription_backend,
        }

    return application


def serialize_task(
    task: dict[str, Any], *, include_result: bool = False
) -> dict[str, Any]:
    result = dict(task)
    # douyin_cookie 是用户抖音登录凭证，永不回显明文，仅暴露是否有设置。
    result["has_douyin_cookie"] = bool(task.get("douyin_cookie"))
    result.pop("douyin_cookie", None)
    if not include_result:
        result.pop("result_text", None)
    status_value = TaskStatus(result["status"])
    result["actions"] = {
        "cancel": status_value
        in {TaskStatus.UPLOADING, TaskStatus.QUEUED, TaskStatus.RUNNING},
        "retry": status_value in {TaskStatus.FAILED, TaskStatus.CANCELLED}
        and (bool(result.get("source_url")) or bool(result.get("source_path"))),
        "delete": status_value
        in {
            TaskStatus.QUEUED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.DELETE_FAILED,
        },
    }
    result["artifacts"] = (
        {kind: f"/api/tasks/{result['id']}/artifacts/{kind}" for kind in ARTIFACT_FILES}
        if status_value is TaskStatus.SUCCEEDED
        else {}
    )
    return result


def require_task_id(task_id: str) -> str:
    try:
        return str(UUID(task_id))
    except ValueError as error:
        raise HTTPException(404, "任务不存在") from error


def _cleanup_failed_upload(
    storage: Storage,
    task_id: str,
    upload_id: str,
    final_path: Path,
) -> None:
    try:
        storage.cleanup_upload(task_id, upload_id)
        if final_path.exists():
            final_path.unlink()
    except Exception:
        logger.exception("failed to clean upload task_id=%s", task_id)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _same_create_request(task: dict[str, Any], payload: TaskCreate) -> bool:
    return (
        task["original_name"] == payload.file_name
        and task["expected_size_bytes"] == payload.size_bytes
        and task["model_name"] == payload.model
        and task["language_requested"] == payload.language
        and task["source_url"] == payload.source_url
        and task["initial_prompt"] == payload.initial_prompt
        and task["douyin_cookie"] == payload.douyin_cookie
    )


def _upload_error_code(status_code: int) -> str:
    if status_code == 413:
        return "UPLOAD_TOO_LARGE"
    if status_code == 507:
        return "DISK_SPACE_LOW"
    return "UPLOAD_INVALID"


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _safe_error(error: Exception) -> str:
    message = str(error).replace("\x00", "").strip()
    return message[-1500:] or error.__class__.__name__


def _artifact_media_type(kind: str) -> str:
    return {
        "json": "application/json",
        "txt": "text/plain; charset=utf-8",
        "srt": "application/x-subrip",
        "vtt": "text/vtt; charset=utf-8",
    }[kind]


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()
