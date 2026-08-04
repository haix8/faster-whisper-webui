from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.db import Database
from app.domain import TaskStage, TaskStatus


def create_queued_task(database: Database, index: int) -> str:
    task, _ = database.create_task(
        original_name=f"audio-{index}.wav",
        source_extension=".wav",
        content_type="audio/wav",
        expected_size_bytes=100,
        language_requested="auto",
        model_name="tiny",
        max_attempts=2,
        idempotency_key=f"key-{index}",
    )
    assert database.begin_upload(task["id"]) is True
    database.finish_upload(
        task["id"],
        size_bytes=100,
        sha256=f"{index:064d}",
        duration_seconds=1.0,
        media_format="wav",
        audio_codec="pcm_s16le",
        source_path=f"tasks/{task['id']}/source.wav",
    )
    return task["id"]


def test_atomic_claim_returns_each_task_once(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task_ids = {create_queued_task(database, index) for index in range(8)}

    def claim(index: int) -> str | None:
        task = database.claim_next_task(f"worker-{index}")
        return task["id"] if task else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(claim, range(8)))

    assert set(claimed) == task_ids
    assert len(claimed) == len(set(claimed))
    assert database.claim_next_task("extra") is None


def test_atomic_begin_upload_allows_one_request(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task, _ = database.create_task(
        original_name="audio.wav",
        source_extension=".wav",
        content_type="audio/wav",
        expected_size_bytes=100,
        language_requested="auto",
        model_name="tiny",
        max_attempts=2,
        idempotency_key="upload-key",
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        acquired = list(pool.map(lambda _: database.begin_upload(task["id"]), range(8)))

    assert acquired.count(True) == 1
    assert acquired.count(False) == 7


def test_recovery_requeues_interrupted_task(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task_id = create_queued_task(database, 1)
    assert database.claim_next_task("worker")["id"] == task_id

    recovery = database.recover_incomplete()

    assert recovery["requeued"] == 1
    recovered = database.require_task(task_id)
    assert recovered["status"] == TaskStatus.QUEUED
    assert recovered["attempts"] == 1


def test_recovery_finishes_requested_cancellation(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task_id = create_queued_task(database, 3)
    assert database.claim_next_task("worker")["id"] == task_id
    database.request_cancel(task_id)

    recovery = database.recover_incomplete()

    assert recovery["cancelled"] == 1
    recovered = database.require_task(task_id)
    assert recovered["status"] == TaskStatus.CANCELLED
    assert recovered["cancel_requested"] is False
    assert database.claim_next_task("other-worker") is None


def test_release_to_queue_honors_requested_cancellation(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task_id = create_queued_task(database, 4)
    assert database.claim_next_task("worker")["id"] == task_id
    database.request_cancel(task_id)

    database.release_to_queue(task_id, "service stopping")

    released = database.require_task(task_id)
    assert released["status"] == TaskStatus.CANCELLED
    assert released["cancel_requested"] is False


def test_recovery_exposes_interrupted_delete_for_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task_id = create_queued_task(database, 5)
    database.begin_delete(task_id)

    recovery = database.recover_incomplete()

    assert recovery["interrupted_deletes"] == 1
    recovered = database.require_task(task_id)
    assert recovered["status"] == TaskStatus.DELETE_FAILED
    database.begin_delete(task_id)
    database.finish_delete(task_id)
    assert database.get_task(task_id) is None


def test_link_task_is_created_directly_queued(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task, created = database.create_task(
        original_name="抖音视频",
        source_extension=".mp4",
        content_type=None,
        expected_size_bytes=None,
        language_requested="auto",
        model_name="tiny",
        max_attempts=2,
        idempotency_key="link-key",
        source_url="https://v.douyin.com/FAV7NYgWNuE/",
    )

    assert created is True
    assert task["status"] == TaskStatus.QUEUED
    assert task["stage"] == TaskStage.QUEUE
    assert task["queued_at"] is not None
    assert task["source_url"] == "https://v.douyin.com/FAV7NYgWNuE/"


def test_finish_download_records_source_metadata(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task, _ = database.create_task(
        original_name="抖音视频",
        source_extension=".mp4",
        content_type=None,
        expected_size_bytes=None,
        language_requested="auto",
        model_name="tiny",
        max_attempts=2,
        idempotency_key="link-key-2",
        source_url="https://v.douyin.com/FAV7NYgWNuE/",
    )
    claimed = database.claim_next_task("worker")
    assert claimed is not None
    database.update_progress(
        claimed["id"],
        stage=TaskStage.DOWNLOADING,
        progress=1,
        message="正在下载",
    )

    updated = database.finish_download(
        claimed["id"],
        size_bytes=1234,
        sha256="ab" * 32,
        duration_seconds=1.5,
        media_format="mov,mp4",
        audio_codec="aac",
        source_path=f"tasks/{claimed['id']}/source.mp4",
        original_name="下载完成后的标题",
    )

    assert updated["status"] == TaskStatus.RUNNING
    assert updated["size_bytes"] == 1234
    assert updated["sha256"] == "ab" * 32
    assert updated["original_name"] == "下载完成后的标题"
    assert updated["source_path"] == f"tasks/{claimed['id']}/source.mp4"


def test_finish_download_keeps_original_name_when_absent(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task, _ = database.create_task(
        original_name="抖音视频",
        source_extension=".mp4",
        content_type=None,
        expected_size_bytes=None,
        language_requested="auto",
        model_name="tiny",
        max_attempts=2,
        idempotency_key="link-key-3",
        source_url="https://v.douyin.com/FAV7NYgWNuE/",
    )
    claimed = database.claim_next_task("worker")
    assert claimed is not None
    database.update_progress(
        claimed["id"], stage=TaskStage.DOWNLOADING, progress=1, message="正在下载"
    )
    updated = database.finish_download(
        claimed["id"],
        size_bytes=1,
        sha256="0" * 64,
        duration_seconds=1.0,
        media_format="mp4",
        audio_codec="aac",
        source_path=f"tasks/{claimed['id']}/source.mp4",
    )
    assert updated["original_name"] == "抖音视频"


def test_legacy_database_gains_source_url_column(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("ALTER TABLE tasks DROP COLUMN source_url")

    database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
        }
    assert "source_url" in columns


def test_queued_cancel_and_manual_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task_id = create_queued_task(database, 2)

    cancelled = database.request_cancel(task_id)
    assert cancelled["status"] == TaskStatus.CANCELLED

    retried = database.retry_task(task_id)
    assert retried["status"] == TaskStatus.QUEUED
    assert retried["attempts"] == 0
