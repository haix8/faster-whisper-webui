from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable


class TaskStatus(StrEnum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETING = "deleting"
    DELETE_FAILED = "delete_failed"
    DELETED = "deleted"


class TaskStage(StrEnum):
    UPLOAD = "upload"
    RECEIVING_UPLOAD = "receiving_upload"
    QUEUE = "queue"
    STARTING = "starting"
    PREPROCESSING = "preprocessing"
    TRANSCRIBING = "transcribing"
    WRITING_RESULTS = "writing_results"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETING = "deleting"
    DELETE_FAILED = "delete_failed"


@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str
    words: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TranscriptionResult:
    text: str
    segments: list[Segment]
    language: str | None
    language_probability: float | None
    duration_seconds: float
    model: str
    backend: str
    device: str
    compute_type: str


@dataclass(slots=True)
class BackendRuntime:
    name: str
    available: bool
    device: str
    compute_type: str
    detail: str
    loaded_model: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


ProgressCallback = Callable[[float, str], None]
CancelCallback = Callable[[], bool]


class TranscriptionError(RuntimeError):
    code = "TRANSCRIPTION_ERROR"


class BackendUnavailable(TranscriptionError):
    code = "BACKEND_UNAVAILABLE"


class TaskCancelled(TranscriptionError):
    code = "TASK_CANCELLED"


class WorkerStopping(TranscriptionError):
    code = "WORKER_STOPPING"


class MediaValidationError(TranscriptionError):
    code = "INVALID_MEDIA"


@dataclass(slots=True)
class MediaInfo:
    duration_seconds: float
    format_name: str
    audio_codec: str
    audio_channels: int | None
    sample_rate: int | None
    source_path: Path
