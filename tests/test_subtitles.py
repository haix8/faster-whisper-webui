from __future__ import annotations

from app.subtitles import (
    MAX_CUE_SECONDS,
    split_words_into_segments,
    wrap_subtitle_text,
)


def timed_words(
    text: str,
    *,
    seconds_per_character: float = 0.2,
) -> list[dict[str, object]]:
    words = []
    for index, character in enumerate(text):
        start = index * seconds_per_character
        words.append(
            {
                "start": start,
                "end": start + seconds_per_character,
                "word": character,
                "probability": 0.99,
            }
        )
    return words


def test_chinese_words_are_split_on_semantics_with_hard_limits() -> None:
    text = (
        "这是第一部分内容，接着说明第二部分内容，"
        "还要继续补充第三部分内容，最后在这里结束。"
    )

    segments = split_words_into_segments(
        timed_words(text),
        language="zh",
        fallback_start=0,
        fallback_end=20,
        fallback_text=text,
    )

    assert len(segments) > 1
    assert "".join(segment.text for segment in segments) == text
    assert all(len(segment.text) <= 32 for segment in segments)
    assert all(segment.end - segment.start <= MAX_CUE_SECONDS for segment in segments)
    assert all(segment.words for segment in segments)
    assert all(
        current.end <= following.start
        for current, following in zip(segments, segments[1:], strict=False)
    )


def test_long_pause_creates_a_natural_boundary() -> None:
    words = [
        {"start": 0.0, "end": 0.5, "word": "你"},
        {"start": 0.5, "end": 1.0, "word": "好。"},
        {"start": 1.8, "end": 2.3, "word": "再"},
        {"start": 2.3, "end": 2.8, "word": "见。"},
    ]

    segments = split_words_into_segments(
        words,
        language="zh",
        fallback_start=0,
        fallback_end=2.8,
        fallback_text="你好。再见。",
    )

    assert [(segment.start, segment.end, segment.text) for segment in segments] == [
        (0.0, 1.0, "你好。"),
        (1.8, 2.8, "再见。"),
    ]


def test_missing_or_invalid_word_timestamps_fall_back_to_model_segment() -> None:
    segments = split_words_into_segments(
        [{"start": None, "end": 1.0, "word": "无法对齐"}],
        language="zh",
        fallback_start=0.2,
        fallback_end=3.4,
        fallback_text="无法对齐,保留原始分段。",
    )

    assert len(segments) == 1
    assert segments[0].start == 0.2
    assert segments[0].end == 3.4
    assert segments[0].text == "无法对齐，保留原始分段。"
    assert segments[0].words == []


def test_chinese_subtitle_wraps_to_two_balanced_lines() -> None:
    text = f"{'甲' * 13}，{'乙' * 14}"

    wrapped = wrap_subtitle_text(text, "zh")
    lines = wrapped.splitlines()

    assert lines == [f"{'甲' * 13}，", "乙" * 14]
    assert all(len(line) <= 16 for line in lines)
    assert "".join(lines) == text


def test_latin_subtitle_wraps_at_a_word_boundary() -> None:
    text = "Readable subtitles should wrap at a natural word boundary when possible."

    lines = wrap_subtitle_text(text, "en").splitlines()

    assert len(lines) == 2
    assert all(len(line) <= 42 for line in lines)
    assert " ".join(lines) == text
