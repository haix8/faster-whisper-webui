from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from app.domain import TaskStage, TaskStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    original_name TEXT NOT NULL,
    source_extension TEXT NOT NULL,
    content_type TEXT,
    expected_size_bytes INTEGER,
    size_bytes INTEGER,
    sha256 TEXT,
    duration_seconds REAL,
    media_format TEXT,
    audio_codec TEXT,
    language_requested TEXT NOT NULL DEFAULT 'auto',
    language_detected TEXT,
    language_probability REAL,
    model_name TEXT NOT NULL,
    backend_name TEXT,
    device TEXT,
    compute_type TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    result_text TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    source_path TEXT,
    source_url TEXT,
    error_code TEXT,
    error_message TEXT,
    worker_id TEXT,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_queued
    ON tasks(status, queued_at);
CREATE INDEX IF NOT EXISTS idx_tasks_created
    ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_sha256
    ON tasks(sha256);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(SCHEMA)
            self._upgrade_schema(connection)

    @staticmethod
    def _upgrade_schema(connection: sqlite3.Connection) -> None:
        # 历史数据库没有独立迁移框架：CREATE TABLE IF NOT EXISTS 不会为已存在的
        # 表补列，这里按列名显式补齐，保证旧库升级后字段契约一致。
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "source_url" not in existing_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN source_url TEXT")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def ping(self) -> bool:
        try:
            with self.connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def create_task(
        self,
        *,
        original_name: str,
        source_extension: str,
        content_type: str | None,
        expected_size_bytes: int | None,
        language_requested: str,
        model_name: str,
        max_attempts: int,
        idempotency_key: str | None,
        source_url: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        task_id = str(uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM tasks
                        WHERE idempotency_key = ? AND status != ?
                        """,
                        (idempotency_key, TaskStatus.DELETED),
                    ).fetchone()
                    if existing:
                        connection.commit()
                        return self._row_to_dict(existing), False

                if source_url:
                    # 链接任务没有上传阶段：创建即入队，由 Worker 负责下载。
                    status, stage, message, queued_at = (
                        TaskStatus.QUEUED,
                        TaskStage.QUEUE,
                        "等待处理",
                        now,
                    )
                else:
                    status, stage, message, queued_at = (
                        TaskStatus.UPLOADING,
                        TaskStage.UPLOAD,
                        "等待上传",
                        None,
                    )
                connection.execute(
                    """
                    INSERT INTO tasks (
                        id, idempotency_key, original_name, source_extension,
                        content_type, expected_size_bytes, language_requested,
                        model_name, status, stage, progress, message,
                        max_attempts, source_url, created_at, updated_at,
                        queued_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        idempotency_key,
                        original_name,
                        source_extension,
                        content_type,
                        expected_size_bytes,
                        language_requested,
                        model_name,
                        status,
                        stage,
                        message,
                        max_attempts,
                        source_url,
                        now,
                        now,
                        queued_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                connection.commit()
                return self._row_to_dict(row), True
            except Exception:
                connection.rollback()
                raise

    def get_task(
        self, task_id: str, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM tasks WHERE id = ?"
        params: list[Any] = [task_id]
        if not include_deleted:
            query += " AND status != ?"
            params.append(TaskStatus.DELETED)
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._row_to_dict(row) if row else None

    def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None,
        search: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["status != ?"]
        params: list[Any] = [TaskStatus.DELETED]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append("original_name LIKE ? ESCAPE '\\'")
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.append(f"%{escaped}%")
        where = " AND ".join(clauses)
        offset = (page - 1) * page_size
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM tasks
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return [self._row_to_dict(row) for row in rows], total

    def finish_upload(
        self,
        task_id: str,
        *,
        size_bytes: int,
        sha256: str,
        duration_seconds: float,
        media_format: str,
        audio_codec: str,
        source_path: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET size_bytes = ?, sha256 = ?, duration_seconds = ?,
                    media_format = ?, audio_codec = ?, source_path = ?,
                    status = ?, stage = ?, progress = 0, message = ?,
                    queued_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND status = ? AND stage = ?
                """,
                (
                    size_bytes,
                    sha256,
                    duration_seconds,
                    media_format,
                    audio_codec,
                    source_path,
                    TaskStatus.QUEUED,
                    TaskStage.QUEUE,
                    "等待处理",
                    now,
                    now,
                    task_id,
                    TaskStatus.UPLOADING,
                    TaskStage.RECEIVING_UPLOAD,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("task is not accepting an upload")
        return self.require_task(task_id)

    def finish_download(
        self,
        task_id: str,
        *,
        size_bytes: int,
        sha256: str,
        duration_seconds: float,
        media_format: str,
        audio_codec: str,
        source_path: str,
        original_name: str | None = None,
    ) -> dict[str, Any]:
        """记录链接任务下载完成后的源文件元数据，任务仍保持 running。"""
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET size_bytes = ?, sha256 = ?, duration_seconds = ?,
                    media_format = ?, audio_codec = ?, source_path = ?,
                    original_name = COALESCE(?, original_name),
                    updated_at = ?, version = version + 1
                WHERE id = ? AND status = ? AND stage = ?
                """,
                (
                    size_bytes,
                    sha256,
                    duration_seconds,
                    media_format,
                    audio_codec,
                    source_path,
                    original_name,
                    now,
                    task_id,
                    TaskStatus.RUNNING,
                    TaskStage.DOWNLOADING,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("task is not downloading a source")
        return self.require_task(task_id)

    def begin_upload(self, task_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET stage = ?, message = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND status = ? AND stage = ?
                """,
                (
                    TaskStage.RECEIVING_UPLOAD,
                    "正在接收文件",
                    now,
                    task_id,
                    TaskStatus.UPLOADING,
                    TaskStage.UPLOAD,
                ),
            )
        return cursor.rowcount == 1

    def fail_upload(self, task_id: str, code: str, message: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, message = ?, error_code = ?,
                    error_message = ?, finished_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND status = ? AND stage = ?
                """,
                (
                    TaskStatus.FAILED,
                    TaskStage.FAILED,
                    "上传失败",
                    code,
                    message,
                    now,
                    now,
                    task_id,
                    TaskStatus.UPLOADING,
                    TaskStage.RECEIVING_UPLOAD,
                ),
            )

    def claim_next_task(self, worker_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = ? AND cancel_requested = 0
                    ORDER BY queued_at ASC, created_at ASC
                    LIMIT 1
                    """,
                    (TaskStatus.QUEUED,),
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, stage = ?, progress = 1, message = ?,
                        worker_id = ?, heartbeat_at = ?, started_at = ?,
                        finished_at = NULL, attempts = attempts + 1,
                        error_code = NULL, error_message = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND status = ?
                    """,
                    (
                        TaskStatus.RUNNING,
                        TaskStage.STARTING,
                        "准备转写",
                        worker_id,
                        now,
                        now,
                        now,
                        row["id"],
                        TaskStatus.QUEUED,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                claimed = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (row["id"],)
                ).fetchone()
                connection.commit()
                return self._row_to_dict(claimed)
            except Exception:
                connection.rollback()
                raise

    def update_progress(
        self,
        task_id: str,
        *,
        stage: TaskStage,
        progress: float,
        message: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET stage = ?, progress = ?, message = ?,
                    heartbeat_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    stage,
                    max(0.0, min(100.0, progress)),
                    message,
                    now,
                    now,
                    task_id,
                    TaskStatus.RUNNING,
                ),
            )

    def heartbeat(self, task_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks SET heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (now, now, task_id, TaskStatus.RUNNING),
            )

    def finish_success(
        self,
        task_id: str,
        *,
        result_text: str,
        language_detected: str | None,
        language_probability: float | None,
        backend_name: str,
        device: str,
        compute_type: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, progress = 100, message = ?,
                    result_text = ?, language_detected = ?,
                    language_probability = ?, backend_name = ?, device = ?,
                    compute_type = ?, cancel_requested = 0,
                    finished_at = ?, heartbeat_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.SUCCEEDED,
                    TaskStage.COMPLETE,
                    "转写完成",
                    result_text,
                    language_detected,
                    language_probability,
                    backend_name,
                    device,
                    compute_type,
                    now,
                    now,
                    now,
                    task_id,
                    TaskStatus.RUNNING,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("running task disappeared before completion")

    def finish_failure(self, task_id: str, code: str, message: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, message = ?, error_code = ?,
                    error_message = ?, finished_at = ?, heartbeat_at = ?,
                    updated_at = ?, version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.FAILED,
                    TaskStage.FAILED,
                    "处理失败",
                    code,
                    message,
                    now,
                    now,
                    now,
                    task_id,
                    TaskStatus.RUNNING,
                ),
            )

    def finish_cancelled(self, task_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, message = ?, cancel_requested = 0,
                    finished_at = ?, heartbeat_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.CANCELLED,
                    TaskStage.CANCELLED,
                    "任务已取消",
                    now,
                    now,
                    now,
                    task_id,
                    TaskStatus.RUNNING,
                ),
            )

    def release_to_queue(self, task_id: str, message: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT cancel_requested FROM tasks
                    WHERE id = ? AND status = ?
                    """,
                    (task_id, TaskStatus.RUNNING),
                ).fetchone()
                if not row:
                    connection.commit()
                    return
                if row["cancel_requested"]:
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = ?, stage = ?, message = ?,
                            cancel_requested = 0, worker_id = NULL,
                            heartbeat_at = ?, finished_at = ?, updated_at = ?,
                            version = version + 1
                        WHERE id = ? AND status = ?
                        """,
                        (
                            TaskStatus.CANCELLED,
                            TaskStage.CANCELLED,
                            "任务已取消",
                            now,
                            now,
                            now,
                            task_id,
                            TaskStatus.RUNNING,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = ?, stage = ?, progress = 0, message = ?,
                            cancel_requested = 0, worker_id = NULL,
                            heartbeat_at = NULL, started_at = NULL,
                            finished_at = NULL, queued_at = ?, updated_at = ?,
                            version = version + 1
                        WHERE id = ? AND status = ?
                        """,
                        (
                            TaskStatus.QUEUED,
                            TaskStage.QUEUE,
                            message,
                            now,
                            now,
                            task_id,
                            TaskStatus.RUNNING,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ? AND status != ?",
                    (task_id, TaskStatus.DELETED),
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                status = TaskStatus(row["status"])
                if status in (TaskStatus.QUEUED, TaskStatus.UPLOADING):
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = ?, stage = ?, progress = 0, message = ?,
                            cancel_requested = 0, finished_at = ?, updated_at = ?,
                            version = version + 1
                        WHERE id = ?
                        """,
                        (
                            TaskStatus.CANCELLED,
                            TaskStage.CANCELLED,
                            "任务已取消",
                            now,
                            now,
                            task_id,
                        ),
                    )
                elif status is TaskStatus.RUNNING:
                    connection.execute(
                        """
                        UPDATE tasks
                        SET cancel_requested = 1, message = ?, updated_at = ?,
                            version = version + 1
                        WHERE id = ?
                        """,
                        ("正在取消", now, task_id),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.require_task(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def retry_task(self, task_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, progress = 0, message = ?,
                    cancel_requested = 0, attempts = 0, worker_id = NULL,
                    heartbeat_at = NULL, started_at = NULL, finished_at = NULL,
                    error_code = NULL, error_message = NULL, result_text = NULL,
                    queued_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    TaskStatus.QUEUED,
                    TaskStage.QUEUE,
                    "等待重新处理",
                    now,
                    now,
                    task_id,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("only failed or cancelled tasks can be retried")
        return self.require_task(task_id)

    def begin_delete(self, task_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, message = ?,
                    error_code = NULL, error_message = NULL,
                    updated_at = ?,
                    version = version + 1
                WHERE id = ? AND status IN (?, ?, ?, ?, ?)
                """,
                (
                    TaskStatus.DELETING,
                    TaskStage.DELETING,
                    "正在删除任务文件",
                    now,
                    task_id,
                    TaskStatus.QUEUED,
                    TaskStatus.SUCCEEDED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.DELETE_FAILED,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("活动任务必须先取消，才能删除")

    def finish_delete(self, task_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, message = ?, idempotency_key = NULL,
                    deleted_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.DELETED,
                    "任务已删除",
                    now,
                    now,
                    task_id,
                    TaskStatus.DELETING,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("task is not being deleted")

    def fail_delete(self, task_id: str, message: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, stage = ?, message = ?, error_code = ?,
                    error_message = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.DELETE_FAILED,
                    TaskStage.DELETE_FAILED,
                    "文件清理失败，可重试删除",
                    "DELETE_FAILED",
                    message,
                    now,
                    task_id,
                    TaskStatus.DELETING,
                ),
            )

    def recover_incomplete(self) -> dict[str, int]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                interrupted_uploads = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, stage = ?, message = ?, error_code = ?,
                        error_message = ?, finished_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE status = ?
                    """,
                    (
                        TaskStatus.FAILED,
                        TaskStage.FAILED,
                        "上传被服务重启中断",
                        "UPLOAD_INTERRUPTED",
                        "服务重启前上传未完成，请重新创建任务",
                        now,
                        now,
                        TaskStatus.UPLOADING,
                    ),
                ).rowcount
                cancelled = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, stage = ?, message = ?,
                        cancel_requested = 0, worker_id = NULL,
                        heartbeat_at = ?, finished_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE status IN (?, ?) AND cancel_requested = 1
                    """,
                    (
                        TaskStatus.CANCELLED,
                        TaskStage.CANCELLED,
                        "服务重启时完成取消",
                        now,
                        now,
                        now,
                        TaskStatus.RUNNING,
                        TaskStatus.QUEUED,
                    ),
                ).rowcount
                requeued = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, stage = ?, progress = 0, message = ?,
                        worker_id = NULL, heartbeat_at = NULL, started_at = NULL,
                        queued_at = ?, updated_at = ?, version = version + 1
                    WHERE status = ? AND cancel_requested = 0
                        AND attempts < max_attempts
                    """,
                    (
                        TaskStatus.QUEUED,
                        TaskStage.QUEUE,
                        "服务重启，任务已恢复排队",
                        now,
                        now,
                        TaskStatus.RUNNING,
                    ),
                ).rowcount
                exhausted = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, stage = ?, message = ?, error_code = ?,
                        error_message = ?, finished_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE status = ? AND cancel_requested = 0
                        AND attempts >= max_attempts
                    """,
                    (
                        TaskStatus.FAILED,
                        TaskStage.FAILED,
                        "任务因多次中断而失败",
                        "RETRY_EXHAUSTED",
                        "任务达到最大自动恢复次数，请手动重试",
                        now,
                        now,
                        TaskStatus.RUNNING,
                    ),
                ).rowcount
                interrupted_deletes = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, stage = ?, message = ?, error_code = ?,
                        error_message = ?, updated_at = ?, version = version + 1
                    WHERE status = ?
                    """,
                    (
                        TaskStatus.DELETE_FAILED,
                        TaskStage.DELETE_FAILED,
                        "删除被服务重启中断，可重试删除",
                        "DELETE_INTERRUPTED",
                        "文件可能已部分清理，请重新执行删除",
                        now,
                        TaskStatus.DELETING,
                    ),
                ).rowcount
                connection.commit()
                return {
                    "interrupted_uploads": interrupted_uploads,
                    "cancelled": cancelled,
                    "requeued": requeued,
                    "exhausted": exhausted,
                    "interrupted_deletes": interrupted_deletes,
                }
            except Exception:
                connection.rollback()
                raise

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tasks WHERE status != ?
                GROUP BY status
                """,
                (TaskStatus.DELETED,),
            ).fetchall()
        result = {
            status.value: 0 for status in TaskStatus if status != TaskStatus.DELETED
        }
        result.update({row["status"]: row["count"] for row in rows})
        return result

    def require_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        return task

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item
