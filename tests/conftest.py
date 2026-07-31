from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
        transcription_backend="fake",
        transcription_device="cpu",
        transcription_models="tiny,small",
        default_model="tiny",
        min_free_bytes=0,
        max_upload_bytes=10 * 1024 * 1024,
        max_media_seconds=60,
        worker_poll_seconds=0.1,
        heartbeat_seconds=1,
        fake_backend_delay_seconds=0.02,
    )


def wav_bytes(duration_seconds: float = 1.0, sample_rate: int = 16_000) -> bytes:
    frame_count = max(1, round(duration_seconds * sample_rate))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frame_count)
    return buffer.getvalue()
