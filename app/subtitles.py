from __future__ import annotations

import math
import re

from app.domain import Segment

MIN_CUE_SECONDS = 5 / 6
TARGET_CUE_SECONDS = 5.0
MAX_CUE_SECONDS = 7.0
PAUSE_BREAK_SECONDS = 0.6

_CJK_LANGUAGES = {"zh", "yue", "ja", "ko"}
_CJK_TARGET_CHARS = 28
_CJK_MAX_CHARS = 32
_CJK_MAX_CHARS_PER_SECOND = 9.0
_OTHER_TARGET_CHARS = 64
_OTHER_MAX_CHARS = 84
_OTHER_MAX_CHARS_PER_SECOND = 20.0
_TERMINAL_PUNCTUATION = frozenset(".。!！?？…")
_SOFT_PUNCTUATION = frozenset(",，;；:：")
_ALL_BREAK_PUNCTUATION = _TERMINAL_PUNCTUATION | _SOFT_PUNCTUATION


def normalize_punctuation(text: str, language: str | None) -> str:
    if language != "zh":
        return text
    text = re.sub(r"(?<=[\u3400-\u9fff0-9]),", "，", text)
    text = re.sub(r"(?<=[\u3400-\u9fff0-9]);", "；", text)
    text = re.sub(r"(?<=[\u3400-\u9fff0-9]):", "：", text)
    text = re.sub(r"(?<=[\u3400-\u9fff0-9])\?", "？", text)
    return re.sub(r"(?<=[\u3400-\u9fff0-9])!", "！", text)


def split_words_into_segments(
    words: list[dict[str, object]],
    *,
    language: str | None,
    fallback_start: float,
    fallback_end: float,
    fallback_text: str,
) -> list[Segment]:
    prepared_words = _prepare_words(words, language)
    if prepared_words is None or not prepared_words:
        text = normalize_punctuation(fallback_text, language).strip()
        if not text:
            return []
        return [
            Segment(
                start=float(fallback_start),
                end=float(fallback_end),
                text=text,
            )
        ]

    chunks = _chunk_words(prepared_words, language)
    return [_segment_from_words(chunk, language) for chunk in chunks]


def wrap_subtitle_text(text: str, language: str | None) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    limit = 16 if language in _CJK_LANGUAGES else 42
    if len(cleaned) <= limit:
        return cleaned

    split_at = _line_break_position(cleaned, limit)
    first = cleaned[:split_at].rstrip()
    second = cleaned[split_at:].lstrip()
    return f"{first}\n{second}" if second else first


def _prepare_words(
    words: list[dict[str, object]], language: str | None
) -> list[dict[str, object]] | None:
    prepared: list[dict[str, object]] = []
    previous_start = -math.inf
    for word in words:
        text = str(word.get("word") or "")
        if not text:
            continue
        start = _finite_float(word.get("start"))
        end = _finite_float(word.get("end"))
        if start is None or end is None or start < previous_start or end < start:
            return None

        normalized = dict(word)
        normalized["start"] = start
        normalized["end"] = end
        normalized["word"] = normalize_punctuation(text, language)
        prepared.append(normalized)
        previous_start = start
    return prepared


def _chunk_words(
    words: list[dict[str, object]], language: str | None
) -> list[list[dict[str, object]]]:
    target_chars, max_chars, max_chars_per_second = _limits(language)
    chunks: list[list[dict[str, object]]] = []
    start_index = 0

    while start_index < len(words):
        cue_start = float(words[start_index]["start"])
        preferred_boundary: int | None = None
        last_fitting_boundary: int | None = None
        chosen_boundary: int | None = None

        for end_index in range(start_index, len(words)):
            cue_words = words[start_index : end_index + 1]
            cue_end = float(words[end_index]["end"])
            duration = max(0.0, cue_end - cue_start)
            char_count = _character_count(_join_words(cue_words))
            within_hard_limits = duration <= MAX_CUE_SECONDS and char_count <= max_chars

            if not within_hard_limits:
                chosen_boundary = (
                    preferred_boundary
                    if preferred_boundary is not None
                    else last_fitting_boundary
                )
                if chosen_boundary is None:
                    chosen_boundary = end_index
                break

            last_fitting_boundary = end_index
            text = str(words[end_index]["word"]).rstrip()
            next_gap = _gap_after(words, end_index)
            readable_duration = max(
                MIN_CUE_SECONDS,
                char_count / max_chars_per_second,
            )
            eligible = duration >= readable_duration
            terminal = _ends_with(text, _TERMINAL_PUNCTUATION)
            soft = _ends_with(text, _SOFT_PUNCTUATION)
            paused = next_gap >= PAUSE_BREAK_SECONDS

            if eligible and (terminal or paused):
                chosen_boundary = end_index
                break
            if eligible and soft:
                preferred_boundary = end_index
                if duration >= TARGET_CUE_SECONDS or char_count >= target_chars:
                    chosen_boundary = end_index
                    break

        if chosen_boundary is None:
            chosen_boundary = len(words) - 1
        chunks.append(words[start_index : chosen_boundary + 1])
        start_index = chosen_boundary + 1

    return chunks


def _segment_from_words(
    words: list[dict[str, object]], language: str | None
) -> Segment:
    text = normalize_punctuation(_join_words(words), language).strip()
    return Segment(
        start=float(words[0]["start"]),
        end=float(words[-1]["end"]),
        text=text,
        words=[dict(word) for word in words],
    )


def _line_break_position(text: str, limit: int) -> int:
    midpoint = len(text) // 2
    lower = max(1, len(text) - limit)
    upper = min(limit, len(text) - 1)
    if lower > upper:
        return max(1, min(midpoint, len(text) - 1))

    candidates = [
        index
        for index in range(lower, upper + 1)
        if text[index - 1] in _ALL_BREAK_PUNCTUATION
        or text[index - 1].isspace()
        or text[index].isspace()
    ]
    bottom_heavy = [index for index in candidates if index <= midpoint]
    if bottom_heavy:
        return min(bottom_heavy, key=lambda index: (midpoint - index, -index))
    if candidates:
        return min(candidates, key=lambda index: (abs(midpoint - index), index))
    return max(lower, min(midpoint, upper))


def _limits(language: str | None) -> tuple[int, int, float]:
    if language in _CJK_LANGUAGES:
        return (
            _CJK_TARGET_CHARS,
            _CJK_MAX_CHARS,
            _CJK_MAX_CHARS_PER_SECOND,
        )
    return (
        _OTHER_TARGET_CHARS,
        _OTHER_MAX_CHARS,
        _OTHER_MAX_CHARS_PER_SECOND,
    )


def _join_words(words: list[dict[str, object]]) -> str:
    return "".join(str(word["word"]) for word in words)


def _character_count(text: str) -> int:
    return sum(not character.isspace() for character in text)


def _gap_after(words: list[dict[str, object]], index: int) -> float:
    if index + 1 >= len(words):
        return 0.0
    return max(
        0.0,
        float(words[index + 1]["start"]) - float(words[index]["end"]),
    )


def _ends_with(text: str, punctuation: frozenset[str]) -> bool:
    return bool(text) and text[-1] in punctuation


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
