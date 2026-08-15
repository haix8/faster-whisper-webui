from __future__ import annotations

import time
from pathlib import Path

from app.backends.base import TranscriptionBackend
from app.config import Settings
from app.domain import (
    BackendRuntime,
    Segment,
    TaskCancelled,
    TranscriptionResult,
    WorkerStopping,
)


class FakeBackend(TranscriptionBackend):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.loaded_model: str | None = None

    def runtime(self) -> BackendRuntime:
        return BackendRuntime(
            name="fake",
            available=True,
            device="cpu",
            compute_type="deterministic",
            detail="测试后端已就绪",
            loaded_model=self.loaded_model,
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        model_name: str,
        language: str | None,
        duration_seconds: float,
        on_progress,
        is_cancelled,
        is_stopping,
        initial_prompt: str | None = None,
    ) -> TranscriptionResult:
        del audio_path
        self.loaded_model = model_name
        text_parts = ["这是一个测试转写结果。", "Faster Whisper WebUI 已完成任务。"]
        segment_duration = max(duration_seconds / len(text_parts), 0.5)
        segments: list[Segment] = []
        for index, text in enumerate(text_parts):
            if is_stopping():
                raise WorkerStopping("服务正在停止")
            if is_cancelled():
                raise TaskCancelled("任务已取消")
            time.sleep(self.settings.fake_backend_delay_seconds)
            start = index * segment_duration
            end = min((index + 1) * segment_duration, duration_seconds)
            if end <= start:
                end = start + 0.5
            segments.append(Segment(start=start, end=end, text=text))
            on_progress(end, text)
        return TranscriptionResult(
            text="".join(text_parts),
            segments=segments,
            language=None if language in (None, "auto") else language,
            language_probability=1.0,
            duration_seconds=duration_seconds,
            model=model_name,
            backend="fake",
            device="cpu",
            compute_type="deterministic",
        )
