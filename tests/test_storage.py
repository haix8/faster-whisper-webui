from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.storage import Storage


def test_storage_never_uses_user_name_for_internal_path(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "models")
    storage.initialize()
    task_id = str(uuid4())
    path = storage.source_path(task_id, ".wav")
    assert path.name == "source.wav"
    assert path.parent.name == task_id


@pytest.mark.parametrize("task_id", ["../../etc", "not-a-uuid", ""])
def test_task_path_rejects_non_uuid(task_id: str, tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "models")
    storage.initialize()
    with pytest.raises(ValueError):
        storage.task_dir(task_id)


def test_relative_path_cannot_escape_data_root(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "models")
    storage.initialize()
    with pytest.raises(ValueError):
        storage.resolve_relative("../outside")


def test_cleanup_stale_uploads_only_removes_uuid_task_uploads(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "models")
    storage.initialize()
    task_id = str(uuid4())
    upload = storage.upload_path(task_id, str(uuid4()))
    upload.write_bytes(b"partial")
    unrelated = storage.tasks_dir / "not-a-task" / "source.upload"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"keep")

    assert storage.cleanup_stale_uploads() == 1
    assert not upload.exists()
    assert unrelated.exists()


def test_download_temp_path_and_cleanup(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "models")
    storage.initialize()
    task_id = str(uuid4())
    download = storage.download_temp_path(task_id, str(uuid4()))
    assert download.parent.name == task_id
    assert download.name.startswith("source.")
    assert download.name.endswith(".download")
    download.write_bytes(b"partial")

    assert storage.cleanup_stale_downloads() == 1
    assert not download.exists()


def test_cleanup_stale_downloads_only_removes_uuid_task_files(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "models")
    storage.initialize()
    task_id = str(uuid4())
    download = storage.download_temp_path(task_id, str(uuid4()))
    download.write_bytes(b"partial")
    unrelated = storage.tasks_dir / "not-a-task" / "source.abc.download"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"keep")
    storage.cleanup_download(task_id, str(uuid4()))

    assert download.exists()
    assert storage.cleanup_stale_downloads() == 1
    assert not download.exists()
    assert unrelated.exists()
