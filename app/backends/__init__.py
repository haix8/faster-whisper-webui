from __future__ import annotations

from app.backends.base import TranscriptionBackend
from app.config import Settings


def create_backend(settings: Settings) -> TranscriptionBackend:
    if settings.transcription_backend == "fake":
        from app.backends.fake import FakeBackend

        return FakeBackend(settings)
    if settings.transcription_backend == "faster-whisper":
        from app.backends.faster_whisper import FasterWhisperBackend

        return FasterWhisperBackend(settings)
    raise ValueError(f"unsupported backend: {settings.transcription_backend}")


__all__ = ["TranscriptionBackend", "create_backend"]
