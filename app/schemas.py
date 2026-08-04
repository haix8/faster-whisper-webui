from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskCreate(BaseModel):
    # 上传任务必须提供 file_name；链接任务必须提供 source_url，两者互斥。
    file_name: str | None = Field(default=None, min_length=1, max_length=255)
    source_url: str | None = Field(default=None, max_length=2000)
    size_bytes: int | None = Field(default=None, ge=1)
    content_type: str | None = Field(default=None, max_length=255)
    model: str = Field(min_length=1, max_length=200)
    language: str = Field(default="auto", min_length=2, max_length=20)

    @field_validator("file_name")
    @classmethod
    def normalize_file_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
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

    @field_validator("source_url")
    @classmethod
    def normalize_source_url(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> "TaskCreate":
        if not self.file_name and not self.source_url:
            raise ValueError("file_name 或 source_url 必须提供一个")
        if self.file_name and self.source_url:
            raise ValueError("file_name 与 source_url 不能同时提供")
        return self
