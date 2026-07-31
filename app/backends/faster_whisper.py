from __future__ import annotations

import gc
import logging
import re
from pathlib import Path
from typing import Any

from app.backends.base import TranscriptionBackend
from app.config import Settings
from app.domain import (
    BackendRuntime,
    BackendUnavailable,
    Segment,
    TaskCancelled,
    TranscriptionResult,
    WorkerStopping,
)

logger = logging.getLogger(__name__)


class FasterWhisperBackend(TranscriptionBackend):
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model: Any | None = None
        self._loaded_key: tuple[str, str, str] | None = None
        self._import_error: str | None = None
        self._resolved_device: str | None = None
        self._resolved_compute_type: str | None = None

    def runtime(self) -> BackendRuntime:
        try:
            device, compute_type = self._resolve_runtime()
        except BackendUnavailable as error:
            return BackendRuntime(
                name="faster-whisper",
                available=False,
                device=self.settings.transcription_device,
                compute_type=self.settings.transcription_compute_type,
                detail=str(error),
                loaded_model=self.loaded_model,
            )
        return BackendRuntime(
            name="faster-whisper",
            available=True,
            device=device,
            compute_type=compute_type,
            detail=f"{device.upper()} 推理可用",
            loaded_model=self.loaded_model,
        )

    @property
    def loaded_model(self) -> str | None:
        return self._loaded_key[0] if self._loaded_key else None

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
    ) -> TranscriptionResult:
        device, compute_type = self._resolve_runtime()
        if is_cancelled():
            raise TaskCancelled("任务已取消")
        if is_stopping():
            raise WorkerStopping("服务正在停止")

        model = self._load_model(model_name, device, compute_type)
        if is_cancelled():
            raise TaskCancelled("任务已取消")
        if is_stopping():
            raise WorkerStopping("服务正在停止")
        requested_language = None if language in (None, "auto") else language
        initial_prompt = (
            self.settings.transcription_initial_prompt_zh
            if requested_language == "zh"
            else ""
        )
        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=requested_language,
                beam_size=self.settings.beam_size,
                vad_filter=self.settings.vad_filter,
                word_timestamps=False,
                condition_on_previous_text=True,
                initial_prompt=initial_prompt or None,
            )
            detected_language = getattr(info, "language", requested_language)
            segments: list[Segment] = []
            text_parts: list[str] = []
            for raw_segment in raw_segments:
                if is_cancelled():
                    raise TaskCancelled("任务已取消")
                if is_stopping():
                    raise WorkerStopping("服务正在停止")
                raw_text = _normalize_punctuation(
                    str(raw_segment.text), detected_language
                )
                text = raw_text.strip()
                segment = Segment(
                    start=float(raw_segment.start),
                    end=float(raw_segment.end),
                    text=text,
                )
                segments.append(segment)
                if raw_text:
                    text_parts.append(raw_text)
                on_progress(segment.end, text)
        except (TaskCancelled, WorkerStopping):
            raise
        except Exception as error:
            logger.exception("faster-whisper transcription failed")
            raise RuntimeError(_safe_error(error)) from error

        return TranscriptionResult(
            text=_join_segment_texts(text_parts),
            segments=segments,
            language=detected_language,
            language_probability=_optional_float(
                getattr(info, "language_probability", None)
            ),
            duration_seconds=duration_seconds,
            model=model_name,
            backend="faster-whisper",
            device=device,
            compute_type=compute_type,
        )

    def _load_model(self, model_name: str, device: str, compute_type: str) -> Any:
        key = (model_name, device, compute_type)
        if self._model is not None and self._loaded_key == key:
            return self._model

        # Release the previous model before constructing another one. Keeping both
        # alive during a model switch can transiently exhaust a small GPU.
        self._model = None
        self._loaded_key = None
        gc.collect()

        try:
            from faster_whisper import WhisperModel

            logger.info(
                "loading faster-whisper model=%s device=%s compute_type=%s",
                model_name,
                device,
                compute_type,
            )
            self._model = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                download_root=str(self.settings.model_dir),
                local_files_only=self.settings.local_files_only,
            )
            self._loaded_key = key
            return self._model
        except Exception as error:
            self._model = None
            self._loaded_key = None
            raise RuntimeError(
                f"模型 {model_name} 加载失败：{_safe_error(error)}"
            ) from error

    def _resolve_runtime(self) -> tuple[str, str]:
        if self._import_error:
            raise BackendUnavailable(self._import_error)
        try:
            import ctranslate2
            import faster_whisper  # noqa: F401
        except Exception as error:
            self._import_error = f"faster-whisper 运行库不可用：{_safe_error(error)}"
            raise BackendUnavailable(self._import_error) from error

        requested = self.settings.transcription_device
        try:
            cuda_count = int(ctranslate2.get_cuda_device_count())
        except Exception:
            cuda_count = 0

        if requested == "cuda" and cuda_count < 1:
            raise BackendUnavailable("配置要求 CUDA，但容器没有检测到可用的 NVIDIA GPU")
        device = (
            "cuda"
            if requested == "cuda" or (requested == "auto" and cuda_count > 0)
            else "cpu"
        )
        compute_type = self.settings.transcription_compute_type
        if compute_type == "auto":
            compute_type = "int8_float16" if device == "cuda" else "int8"

        self._resolved_device = device
        self._resolved_compute_type = compute_type
        return device, compute_type


def _safe_error(error: Exception) -> str:
    message = str(error).replace("\x00", "").strip()
    return message[-1500:] or error.__class__.__name__


def _join_segment_texts(parts: list[str]) -> str:
    return "".join(parts).strip()


def _normalize_punctuation(text: str, language: str | None) -> str:
    if language != "zh":
        return text
    text = re.sub(r"(?<=[\u3400-\u9fff0-9]),(?=[\u3400-\u9fff0-9])", "，", text)
    text = re.sub(r"(?<=[\u3400-\u9fff0-9]);(?=[\u3400-\u9fff0-9])", "；", text)
    text = re.sub(r"(?<=[\u3400-\u9fff0-9]):(?=[\u3400-\u9fff0-9])", "：", text)
    text = re.sub(r"(?<=[\u3400-\u9fff0-9])\?", "？", text)
    return re.sub(r"(?<=[\u3400-\u9fff0-9])!", "！", text)


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
