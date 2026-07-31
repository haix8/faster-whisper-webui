from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from app.domain import TranscriptionResult
from app.storage import Storage

logger = logging.getLogger(__name__)


def render_txt(result: TranscriptionResult) -> str:
    text = result.text.strip()
    return f"{text}\n" if text else ""


def render_srt(result: TranscriptionResult) -> str:
    blocks = []
    for index, segment in enumerate(result.segments, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_timestamp(segment.start, srt=True)} --> "
                    f"{format_timestamp(segment.end, srt=True)}",
                    segment.text.strip(),
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_vtt(result: TranscriptionResult) -> str:
    blocks = ["WEBVTT"]
    for segment in result.segments:
        blocks.append(
            "\n".join(
                [
                    f"{format_timestamp(segment.start)} --> "
                    f"{format_timestamp(segment.end)}",
                    segment.text.strip(),
                ]
            )
        )
    return "\n\n".join(blocks) + "\n"


def render_json(result: TranscriptionResult) -> str:
    payload = {
        "schema_version": 1,
        "text": result.text,
        "language": result.language,
        "language_probability": result.language_probability,
        "duration_seconds": result.duration_seconds,
        "model": result.model,
        "backend": result.backend,
        "device": result.device,
        "compute_type": result.compute_type,
        "segments": [asdict(segment) for segment in result.segments],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def publish_artifacts(
    storage: Storage, task_id: str, result: TranscriptionResult
) -> dict[str, Path]:
    outputs = {
        "json": render_json(result),
        "txt": render_txt(result),
        "srt": render_srt(result),
        "vtt": render_vtt(result),
    }
    task_dir = storage.task_dir(task_id)
    staging = task_dir / f".results-{uuid4().hex}.tmp"
    final = task_dir / "results"
    backup = task_dir / f".results-{uuid4().hex}.backup"
    staging.mkdir(parents=True, exist_ok=False)
    replaced_existing = False
    try:
        for kind, content in outputs.items():
            storage.atomic_write_text(staging / f"transcript.{kind}", content)
        if final.exists():
            os.replace(final, backup)
            replaced_existing = True
        os.replace(staging, final)
        _cleanup_tree(backup)
        return {kind: final / f"transcript.{kind}" for kind in outputs}
    except Exception:
        if replaced_existing and backup.exists() and not final.exists():
            os.replace(backup, final)
        raise
    finally:
        _cleanup_tree(staging)
        if final.exists():
            _cleanup_tree(backup)


def _cleanup_tree(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError:
        logger.warning("failed to clean artifact staging path path=%s", path)


def format_timestamp(seconds: float, *, srt: bool = False) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{milliseconds:03d}"
