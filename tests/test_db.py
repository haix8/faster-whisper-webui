from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.db import Database
from app.domain import TaskStatus


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


def test_queued_cancel_and_manual_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    task_id = create_queued_task(database, 2)

    cancelled = database.request_cancel(task_id)
    assert cancelled["status"] == TaskStatus.CANCELLED

    retried = database.retry_task(task_id)
    assert retried["status"] == TaskStatus.QUEUED
    assert retried["attempts"] == 0
