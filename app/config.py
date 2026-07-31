from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Faster-Whisper WebUI"
    app_version: str = "0.1.0"
    data_dir: Path = Path("/data")
    model_dir: Path = Path("/models")

    transcription_backend: Literal["faster-whisper", "fake"] = "faster-whisper"
    transcription_device: Literal["auto", "cpu", "cuda"] = "auto"
    transcription_compute_type: str = "auto"
    transcription_models: str = "tiny,base,small,medium,large-v3,turbo"
    default_model: str = "small"
    default_language: Literal[
        "auto", "zh", "en", "ja", "ko", "yue", "fr", "de", "es", "ru"
    ] = "auto"
    transcription_initial_prompt_zh: str = Field(default="", max_length=1000)
    local_files_only: bool = False
    beam_size: int = Field(default=5, ge=1, le=20)
    vad_filter: bool = True

    max_upload_bytes: int = Field(default=2_147_483_648, ge=1)
    max_media_seconds: float = Field(default=28_800, gt=0)
    min_free_bytes: int = Field(default=1_073_741_824, ge=0)
    max_task_attempts: int = Field(default=2, ge=1, le=10)
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    heartbeat_seconds: float = Field(default=5.0, ge=1, le=60)
    upload_chunk_bytes: int = Field(default=1_048_576, ge=65_536)
    fake_backend_delay_seconds: float = Field(default=0.05, ge=0, le=10)

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    log_level: str = "INFO"

    @field_validator("transcription_compute_type")
    @classmethod
    def validate_compute_type(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("transcription_compute_type cannot be empty")
        return value

    @field_validator("transcription_models")
    @classmethod
    def validate_models(cls, value: str) -> str:
        models = [item.strip() for item in value.split(",") if item.strip()]
        if not models:
            raise ValueError("transcription_models must contain at least one model")
        if len(models) != len(set(models)):
            raise ValueError("transcription_models cannot contain duplicates")
        return ",".join(models)

    @field_validator("transcription_initial_prompt_zh")
    @classmethod
    def normalize_chinese_prompt(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_default_model(self) -> "Settings":
        if self.default_model not in self.model_names:
            raise ValueError("default_model must be listed in transcription_models")
        return self

    @property
    def model_names(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.transcription_models.split(","))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "app.sqlite3"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"
