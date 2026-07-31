from __future__ import annotations

import json

from app.domain import Segment, TranscriptionResult
from app.formatters import (
    format_timestamp,
    render_json,
    render_srt,
    render_txt,
    render_vtt,
)


def sample_result() -> TranscriptionResult:
    return TranscriptionResult(
        text="你好。第二句。",
        segments=[
            Segment(start=0, end=1.2346, text="你好。"),
            Segment(start=61.2, end=3661.001, text="第二句。"),
        ],
        language="zh",
        language_probability=0.99,
        duration_seconds=3661.1,
        model="small",
        backend="fake",
        device="cpu",
        compute_type="int8",
    )


def test_timestamp_rounding_and_formats() -> None:
    assert format_timestamp(1.2346, srt=True) == "00:00:01,235"
    assert format_timestamp(3661.001) == "01:01:01.001"

    result = sample_result()
    assert render_txt(result) == "你好。第二句。\n"
    assert "00:00:00,000 --> 00:00:01,235" in render_srt(result)
    assert render_vtt(result).startswith("WEBVTT\n\n")
    payload = json.loads(render_json(result))
    assert payload["schema_version"] == 1
    assert payload["segments"][1]["start"] == 61.2
