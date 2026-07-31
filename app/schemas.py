from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class TaskCreate(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int | None = Field(default=None, ge=1)
    content_type: str | None = Field(default=None, max_length=255)
    model: str = Field(min_length=1, max_length=200)
    language: str = Field(default="auto", min_length=2, max_length=20)

    @field_validator("file_name")
    @classmethod
    def normalize_file_name(cls, value: str) -> str:
        normalized = value.replace("\\", "/").split("/")[-1].strip()
        if not normalized or normalized in {".", ".."}:
            raise ValueError("file_name is invalid")
        return normalized

    @field_validator("content_type")
    @classmethod
    def normalize_content_type(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return value.strip().lower()
