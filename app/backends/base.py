from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.domain import (
    BackendRuntime,
    CancelCallback,
    ProgressCallback,
    TranscriptionResult,
)


class TranscriptionBackend(ABC):
    @abstractmethod
    def runtime(self) -> BackendRuntime:
        """Return current backend availability without loading a model."""

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        *,
        model_name: str,
        language: str | None,
        duration_seconds: float,
        on_progress: ProgressCallback,
        is_cancelled: CancelCallback,
        is_stopping: CancelCallback,
        initial_prompt: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe one normalized audio file."""
