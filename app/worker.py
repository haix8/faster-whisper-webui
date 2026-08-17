from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from app.backends.base import TranscriptionBackend
from app.config import Settings
from app.db import Database
from app.domain import (
    BackendUnavailable,
    TaskCancelled,
    TaskStage,
    WorkerStopping,
)
from app.douyin import download_video, resolve_douyin_share
from app.formatters import publish_artifacts
from app.media import normalize_audio, probe_media
from app.storage import Storage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkerSnapshot:
    running: bool = False
    available: bool = False
    worker_id: str = ""
    current_task_id: str | None = None
    downloading_task_id: str | None = None
    backend: str = ""
    device: str = ""
    compute_type: str = ""
    loaded_model: str | None = None
    detail: str = "Worker 尚未启动"
    last_seen_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TranscriptionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        storage: Storage,
        backend: TranscriptionBackend,
    ):
        self.settings = settings
        self.database = database
        self.storage = storage
        self.backend = backend
        self.worker_id = f"worker-{uuid4().hex[:12]}"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._download_thread: threading.Thread | None = None
        self._snapshot = WorkerSnapshot(worker_id=self.worker_id)
        self._snapshot_lock = threading.Lock()

    def start(self) -> dict[str, int]:
        recovery = self.database.recover_incomplete()
        recovery["stale_uploads_removed"] = self.storage.cleanup_stale_uploads()
        recovery["stale_downloads_removed"] = self.storage.cleanup_stale_downloads()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="transcription-worker",
            daemon=True,
        )
        self._download_thread = threading.Thread(
            target=self._download_loop,
            name="download-worker",
            daemon=True,
        )
        self._thread.start()
        self._download_thread.start()
        return recovery

    def stop(self, timeout: float = 10) -> None:
        self._stop_event.set()
        self._wake_event.set()
        for thread in (self._thread, self._download_thread):
            if thread:
                thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake_event.set()

    def snapshot(self) -> dict[str, object]:
        with self._snapshot_lock:
            snapshot = self._snapshot.to_dict()
        if snapshot["last_seen_at"] is not None:
            snapshot["last_seen_seconds_ago"] = max(
                0.0, time.time() - float(snapshot["last_seen_at"])
            )
        else:
            snapshot["last_seen_seconds_ago"] = None
        snapshot.pop("last_seen_at", None)
        return snapshot

    def _run(self) -> None:
        self._set_snapshot(running=True, detail="Worker 正在启动")
        while not self._stop_event.is_set():
            try:
                runtime = self.backend.runtime()
                self._set_snapshot(
                    running=True,
                    available=runtime.available,
                    backend=runtime.name,
                    device=runtime.device,
                    compute_type=runtime.compute_type,
                    loaded_model=runtime.loaded_model,
                    detail=runtime.detail,
                    last_seen_at=time.time(),
                )
                if not runtime.available:
                    self._wait(min(5.0, self.settings.worker_poll_seconds * 5))
                    continue

                task = self.database.claim_next_task(self.worker_id)
                if not task:
                    self._set_snapshot(
                        available=True,
                        current_task_id=None,
                        detail=self._idle_detail(),
                        last_seen_at=time.time(),
                    )
                    self._wait(self.settings.worker_poll_seconds)
                    continue

                self._set_snapshot(
                    current_task_id=task["id"],
                    detail=f"正在处理 {task['original_name']}",
                    last_seen_at=time.time(),
                )
                self._process_task(task)
            except Exception as error:
                logger.exception("worker loop failed")
                self._set_snapshot(
                    running=True,
                    available=False,
                    current_task_id=None,
                    detail=f"Worker 运行异常，正在重试：{_safe_error(error)}",
                    last_seen_at=time.time(),
                )
                self._wait(min(5.0, self.settings.worker_poll_seconds * 5))

        self._set_snapshot(
            running=False,
            available=False,
            current_task_id=None,
            detail="Worker 已停止",
            last_seen_at=time.time(),
        )

    def _download_loop(self) -> None:
        """下载线程：与转写线程并行，只做链接任务解析/下载，不加载模型。"""
        while not self._stop_event.is_set():
            try:
                task = self.database.claim_next_download(self.worker_id)
                if not task:
                    self._set_snapshot(
                        downloading_task_id=None,
                        last_seen_at=time.time(),
                    )
                    self._wait(self.settings.worker_poll_seconds)
                    continue
                self._set_snapshot(
                    downloading_task_id=task["id"],
                    last_seen_at=time.time(),
                )
                self._download_task(task)
            except Exception:
                logger.exception("download worker loop failed")
                self._set_snapshot(
                    downloading_task_id=None,
                    last_seen_at=time.time(),
                )
                self._wait(min(5.0, self.settings.worker_poll_seconds * 5))
        self._set_snapshot(
            downloading_task_id=None,
            last_seen_at=time.time(),
        )

    def _download_task(self, task: dict[str, object]) -> None:
        task_id = str(task["id"])
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(task_id, heartbeat_stop),
            name=f"download-heartbeat-{task_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._raise_if_interrupted(task_id)
            source_path = self.storage.source_path(
                task_id, str(task["source_extension"])
            )
            self._download_source(
                task_id,
                str(task["source_url"]),
                source_path,
                cookie=task.get("douyin_cookie"),
            )
            # 下载完成放回队列：保留原排队时间，转写线程空闲后按序接手。
            self.database.release_to_queue(
                task_id,
                "下载完成，等待转写",
                stage=TaskStage.DOWNLOADING,
                reset_queued_at=False,
            )
            logger.info("download completed task_id=%s", task_id)
        except TaskCancelled:
            self.database.finish_cancelled(task_id)
            logger.info("download cancelled task_id=%s", task_id)
        except WorkerStopping:
            self.database.release_to_queue(
                task_id, "服务停止，任务已恢复排队", stage=TaskStage.DOWNLOADING
            )
            logger.info("download interrupted by stop task_id=%s", task_id)
        except Exception as error:
            logger.exception("download task failed task_id=%s", task_id)
            if self._should_auto_requeue(task):
                self.database.release_to_queue(
                    task_id,
                    f"抖音解析/下载失败，已自动重新排队：{_safe_error(error)}",
                    stage=TaskStage.DOWNLOADING,
                )
                logger.warning(
                    "download failed, auto requeue task_id=%s attempts=%s",
                    task_id,
                    task.get("attempts"),
                )
            else:
                self.database.finish_failure(
                    task_id,
                    getattr(error, "code", "PROCESSING_FAILED"),
                    _safe_error(error),
                )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            self._set_snapshot(
                downloading_task_id=None,
                last_seen_at=time.time(),
            )

    def _idle_detail(self) -> str:
        with self._snapshot_lock:
            downloading_id = self._snapshot.downloading_task_id
        if downloading_id:
            return f"正在下载视频 {downloading_id[:8]}"
        return "Worker 空闲"

    def _process_task(self, task: dict[str, object]) -> None:
        task_id = str(task["id"])
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(task_id, heartbeat_stop),
            name=f"heartbeat-{task_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._raise_if_interrupted(task_id)
            source_path_value = task.get("source_path")
            if source_path_value:
                source_path = self.storage.resolve_relative(str(source_path_value))
            else:
                source_path = self.storage.source_path(
                    task_id, str(task["source_extension"])
                )
            source_url = task.get("source_url")
            if source_url and not source_path.is_file():
                try:
                    self._download_source(
                        task_id,
                        str(source_url),
                        source_path,
                        cookie=task.get("douyin_cookie"),
                    )
                except (TaskCancelled, WorkerStopping, BackendUnavailable):
                    raise
                except Exception as error:
                    if self._should_auto_requeue(task):
                        self.database.release_to_queue(
                            task_id,
                            f"抖音解析/下载失败，已自动重新排队：{_safe_error(error)}",
                        )
                        logger.warning(
                            "download failed, auto requeue task_id=%s attempts=%s",
                            task_id,
                            task.get("attempts"),
                        )
                        return
                    self.database.finish_failure(
                        task_id,
                        getattr(error, "code", "PROCESSING_FAILED"),
                        _safe_error(error),
                    )
                    return
            if not source_path.is_file():
                raise RuntimeError("任务源文件不存在")

            self.database.update_progress(
                task_id,
                stage=TaskStage.PREPROCESSING,
                progress=3,
                message="正在提取音轨",
            )
            audio_path = self.storage.normalized_audio_path(task_id)
            normalize_audio(
                source_path,
                audio_path,
                self.settings.ffmpeg_bin,
                is_cancelled=lambda: self.database.is_cancel_requested(task_id),
                is_stopping=self._stop_event.is_set,
            )
            self.database.update_progress(
                task_id,
                stage=TaskStage.TRANSCRIBING,
                progress=10,
                message="正在加载模型",
            )

            duration = float(task.get("duration_seconds") or 0)
            last_progress_at = 0.0

            def on_progress(segment_end: float, text: str) -> None:
                nonlocal last_progress_at
                now = time.monotonic()
                if now - last_progress_at < 0.5 and segment_end < duration:
                    return
                last_progress_at = now
                fraction = min(1.0, segment_end / duration) if duration else 0
                progress = 10 + fraction * 85
                message = (
                    f"转写至 {segment_end:.1f} / {duration:.1f} 秒"
                    if duration
                    else "正在转写"
                )
                if text:
                    message = f"{message} · {text[:36]}"
                self.database.update_progress(
                    task_id,
                    stage=TaskStage.TRANSCRIBING,
                    progress=progress,
                    message=message,
                )
                self._set_snapshot(last_seen_at=time.time())

            result = self.backend.transcribe(
                audio_path,
                model_name=str(task["model_name"]),
                language=str(task["language_requested"]),
                duration_seconds=duration,
                on_progress=on_progress,
                is_cancelled=lambda: self.database.is_cancel_requested(task_id),
                is_stopping=self._stop_event.is_set,
                initial_prompt=task.get("initial_prompt"),
            )
            self._raise_if_interrupted(task_id)
            self.database.update_progress(
                task_id,
                stage=TaskStage.WRITING_RESULTS,
                progress=96,
                message="正在生成结果文件",
            )
            publish_artifacts(self.storage, task_id, result)
            self.database.finish_success(
                task_id,
                result_text=result.text,
                language_detected=result.language,
                language_probability=result.language_probability,
                backend_name=result.backend,
                device=result.device,
                compute_type=result.compute_type,
            )
            logger.info(
                "transcription completed task_id=%s model=%s device=%s",
                task_id,
                result.model,
                result.device,
            )
        except TaskCancelled:
            self.database.finish_cancelled(task_id)
            logger.info("transcription cancelled task_id=%s", task_id)
        except WorkerStopping:
            self.database.release_to_queue(task_id, "服务停止，任务已恢复排队")
        except BackendUnavailable as error:
            self.database.release_to_queue(task_id, str(error))
            self._set_snapshot(available=False, detail=str(error))
        except Exception as error:
            logger.exception("transcription task failed task_id=%s", task_id)
            self.database.finish_failure(
                task_id,
                getattr(error, "code", "PROCESSING_FAILED"),
                _safe_error(error),
            )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            try:
                self.storage.cleanup_work(task_id)
            except Exception:
                logger.exception("failed to clean work directory task_id=%s", task_id)
            runtime = self.backend.runtime()
            self._set_snapshot(
                current_task_id=None,
                available=runtime.available,
                backend=runtime.name,
                device=runtime.device,
                compute_type=runtime.compute_type,
                loaded_model=runtime.loaded_model,
                detail=runtime.detail,
                last_seen_at=time.time(),
            )

    def _download_source(
        self,
        task_id: str,
        source_url: str,
        destination: Path,
        *,
        cookie: str | None = None,
    ) -> None:
        self.database.update_progress(
            task_id,
            stage=TaskStage.DOWNLOADING,
            progress=1,
            message="正在解析抖音链接",
        )
        video = resolve_douyin_share(
            source_url,
            is_cancelled=lambda: self.database.is_cancel_requested(task_id),
            is_stopping=self._stop_event.is_set,
            cookie=cookie,
        )
        title = video.title[:36] if video.title else "视频"
        self.database.update_progress(
            task_id,
            stage=TaskStage.DOWNLOADING,
            progress=2,
            message=f"正在下载：{title}",
        )
        last_download_progress_at = 0.0

        def on_download_progress(size: int, total: int | None) -> None:
            nonlocal last_download_progress_at
            now = time.monotonic()
            if now - last_download_progress_at < 0.5:
                return
            last_download_progress_at = now
            if total:
                progress = 2 + min(1.0, size / total) * 7
                message = (
                    f"已下载 {size / (1024 * 1024):.1f} "
                    f"/ {total / (1024 * 1024):.1f} MB"
                )
            else:
                progress = 5
                message = f"已下载 {size / (1024 * 1024):.1f} MB"
            self.database.update_progress(
                task_id,
                stage=TaskStage.DOWNLOADING,
                progress=progress,
                message=message,
            )
            self._set_snapshot(last_seen_at=time.time())

        size_bytes, sha256 = download_video(
            video.play_url,
            destination,
            size_limit=self.settings.max_upload_bytes,
            on_progress=on_download_progress,
            is_cancelled=lambda: self.database.is_cancel_requested(task_id),
            is_stopping=self._stop_event.is_set,
            cookie=cookie,
        )
        media = probe_media(
            destination,
            self.settings.ffprobe_bin,
            self.settings.max_media_seconds,
        )
        self.database.finish_download(
            task_id,
            size_bytes=size_bytes,
            sha256=sha256,
            duration_seconds=media.duration_seconds,
            media_format=media.format_name,
            audio_codec=media.audio_codec,
            source_path=self.storage.relative_path(destination),
            original_name=video.title[:255] if video.title else None,
        )
        self._raise_if_interrupted(task_id)

    def _should_auto_requeue(self, task: dict[str, object]) -> bool:
        return int(task.get("attempts") or 0) < int(task.get("max_attempts") or 1)

    def _raise_if_interrupted(self, task_id: str) -> None:
        if self.database.is_cancel_requested(task_id):
            raise TaskCancelled("任务已取消")
        if self._stop_event.is_set():
            raise WorkerStopping("服务正在停止")

    def _heartbeat_loop(self, task_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.settings.heartbeat_seconds):
            try:
                self.database.heartbeat(task_id)
                self._set_snapshot(last_seen_at=time.time())
            except Exception:
                logger.exception("worker heartbeat failed task_id=%s", task_id)

    def _wait(self, seconds: float) -> None:
        self._wake_event.wait(seconds)
        self._wake_event.clear()

    def _set_snapshot(self, **values: object) -> None:
        with self._snapshot_lock:
            for key, value in values.items():
                setattr(self._snapshot, key, value)


def _safe_error(error: Exception) -> str:
    message = str(error).replace("\x00", "").strip()
    return message[-2000:] or error.__class__.__name__
