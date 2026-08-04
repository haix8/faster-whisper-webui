from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4


ALLOWED_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".avi",
    ".flac",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".wma",
    ".wmv",
}

ARTIFACT_FILES = {
    "json": "transcript.json",
    "txt": "transcript.txt",
    "srt": "transcript.srt",
    "vtt": "transcript.vtt",
}


class Storage:
    def __init__(self, data_dir: Path, model_dir: Path):
        self.data_dir = data_dir.resolve()
        self.model_dir = model_dir.resolve()
        self.tasks_dir = self.data_dir / "tasks"

    def initialize(self) -> None:
        (self.data_dir / "db").mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.model_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def source_extension(file_name: str) -> str:
        extension = Path(file_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError(f"不支持的文件扩展名：{extension or '无扩展名'}")
        return extension

    @staticmethod
    def validated_task_id(task_id: str) -> str:
        return str(UUID(task_id))

    def task_dir(self, task_id: str) -> Path:
        canonical_id = self.validated_task_id(task_id)
        path = (self.tasks_dir / canonical_id).resolve()
        self._ensure_inside(path, self.tasks_dir)
        return path

    def create_task_dir(self, task_id: str) -> Path:
        path = self.task_dir(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def upload_path(self, task_id: str, upload_id: str) -> Path:
        canonical_upload_id = UUID(upload_id).hex
        return self.create_task_dir(task_id) / f"source.{canonical_upload_id}.upload"

    def source_path(self, task_id: str, extension: str) -> Path:
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError("invalid source extension")
        return self.create_task_dir(task_id) / f"source{extension}"

    def work_dir(self, task_id: str) -> Path:
        path = self.create_task_dir(task_id) / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def normalized_audio_path(self, task_id: str) -> Path:
        return self.work_dir(task_id) / "audio.wav"

    def results_dir(self, task_id: str) -> Path:
        path = self.create_task_dir(task_id) / "results"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_path(self, task_id: str, kind: str) -> Path:
        try:
            file_name = ARTIFACT_FILES[kind]
        except KeyError as error:
            raise ValueError("unknown artifact kind") from error
        path = (self.task_dir(task_id) / "results" / file_name).resolve()
        self._ensure_inside(path, self.task_dir(task_id))
        return path

    def relative_path(self, path: Path) -> str:
        resolved = path.resolve()
        self._ensure_inside(resolved, self.data_dir)
        return resolved.relative_to(self.data_dir).as_posix()

    def resolve_relative(self, relative_path: str) -> Path:
        path = (self.data_dir / relative_path).resolve()
        self._ensure_inside(path, self.data_dir)
        return path

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.data_dir).free

    def cleanup_work(self, task_id: str) -> None:
        work = self.task_dir(task_id) / "work"
        if work.exists():
            self._ensure_inside(work.resolve(), self.task_dir(task_id))
            shutil.rmtree(work)

    def cleanup_upload(self, task_id: str, upload_id: str) -> None:
        upload = self.upload_path(task_id, upload_id)
        if upload.exists():
            upload.unlink()

    def download_temp_path(self, task_id: str, download_id: str) -> Path:
        canonical_download_id = UUID(download_id).hex
        return (
            self.create_task_dir(task_id) / f"source.{canonical_download_id}.download"
        )

    def cleanup_download(self, task_id: str, download_id: str) -> None:
        download = self.download_temp_path(task_id, download_id)
        if download.exists():
            download.unlink()

    def cleanup_stale_uploads(self) -> int:
        removed = 0
        candidates = [
            *self.tasks_dir.glob("*/source.upload"),
            *self.tasks_dir.glob("*/source.*.upload"),
        ]
        for upload in candidates:
            try:
                task_dir = self.task_dir(upload.parent.name)
            except ValueError:
                continue
            if upload.parent != task_dir:
                continue
            upload.unlink(missing_ok=True)
            removed += 1
        return removed

    def cleanup_stale_downloads(self) -> int:
        removed = 0
        candidates = [*self.tasks_dir.glob("*/source.*.download")]
        for download in candidates:
            try:
                task_dir = self.task_dir(download.parent.name)
            except ValueError:
                continue
            if download.parent != task_dir:
                continue
            download.unlink(missing_ok=True)
            removed += 1
        return removed

    def delete_task(self, task_id: str) -> None:
        path = self.task_dir(task_id)
        if path.exists():
            self._ensure_inside(path.resolve(), self.tasks_dir)
            shutil.rmtree(path)

    @staticmethod
    def atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _ensure_inside(path: Path, root: Path) -> None:
        try:
            path.relative_to(root.resolve())
        except ValueError as error:
            raise ValueError("path escapes storage root") from error
