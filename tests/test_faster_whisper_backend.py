from pathlib import Path
from types import SimpleNamespace

from app.backends.faster_whisper import (
    FasterWhisperBackend,
    _join_segment_texts,
    _normalize_punctuation,
)
from app.config import Settings


def test_segment_text_join_preserves_language_spacing() -> None:
    assert _join_segment_texts([" Hello world.", " This is a test."]) == (
        "Hello world. This is a test."
    )
    assert _join_segment_texts(["你好。", "这是测试。"]) == "你好。这是测试。"


def test_chinese_punctuation_is_normalized_without_touching_other_languages() -> None:
    assert _normalize_punctuation(" 你好,世界? 好!", "zh") == " 你好，世界？ 好！"
    assert _normalize_punctuation(" 你好,", "zh") == " 你好，"
    assert _normalize_punctuation(" Hello, world?", "en") == " Hello, world?"


def test_chinese_prompt_is_only_used_for_explicit_chinese(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
        transcription_models="turbo",
        default_model="turbo",
        transcription_initial_prompt_zh="使用自然、规范的中文标点。",
    )
    backend = FasterWhisperBackend(settings)
    calls: list[dict[str, object]] = []

    class Model:
        def transcribe(self, _audio_path: str, **options):
            calls.append(options)
            return (
                [SimpleNamespace(start=0, end=1, text=" 你好,世界?")],
                SimpleNamespace(language=options["language"], language_probability=1),
            )

    backend._resolve_runtime = lambda: ("cpu", "int8")  # type: ignore[method-assign]
    backend._load_model = lambda *_args: Model()  # type: ignore[method-assign]
    callbacks = {
        "on_progress": lambda *_args: None,
        "is_cancelled": lambda: False,
        "is_stopping": lambda: False,
    }

    result = backend.transcribe(
        tmp_path / "audio.wav",
        model_name="turbo",
        language="zh",
        duration_seconds=1,
        **callbacks,
    )
    backend.transcribe(
        tmp_path / "audio.wav",
        model_name="turbo",
        language="en",
        duration_seconds=1,
        **callbacks,
    )

    assert calls[0]["initial_prompt"] == "使用自然、规范的中文标点。"
    assert calls[1]["initial_prompt"] is None
    assert calls[0]["word_timestamps"] is True
    assert calls[1]["word_timestamps"] is True
    assert result.text == "你好，世界？"
