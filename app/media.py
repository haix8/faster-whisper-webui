from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from app.domain import MediaInfo, MediaValidationError, TaskCancelled, WorkerStopping


def probe_media(path: Path, ffprobe_bin: str, max_duration: float) -> MediaInfo:
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=index,codec_type,codec_name,channels,sample_rate,duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as error:
        raise MediaValidationError("服务器未安装 ffprobe") from error
    except subprocess.TimeoutExpired as error:
        raise MediaValidationError("媒体检查超时") from error

    if completed.returncode != 0:
        detail = _clean_process_error(completed.stderr)
        raise MediaValidationError(f"无法读取媒体文件：{detail}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise MediaValidationError("ffprobe 返回了无效数据") from error

    audio_streams = [
        stream
        for stream in payload.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise MediaValidationError("媒体文件不包含音轨")

    stream = audio_streams[0]
    raw_duration = payload.get("format", {}).get("duration") or stream.get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as error:
        raise MediaValidationError("无法确定媒体时长") from error

    if duration <= 0:
        raise MediaValidationError("媒体时长必须大于 0")
    if duration > max_duration:
        raise MediaValidationError(
            f"媒体时长 {duration:.1f} 秒超过限制 {max_duration:.0f} 秒"
        )

    return MediaInfo(
        duration_seconds=duration,
        format_name=payload.get("format", {}).get("format_name", "unknown"),
        audio_codec=stream.get("codec_name", "unknown"),
        audio_channels=_optional_int(stream.get("channels")),
        sample_rate=_optional_int(stream.get("sample_rate")),
        source_path=path,
    )


def normalize_audio(
    source: Path,
    destination: Path,
    ffmpeg_bin: str,
    *,
    is_cancelled: Callable[[], bool],
    is_stopping: Callable[[], bool],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as error:
        raise MediaValidationError("服务器未安装 ffmpeg") from error

    try:
        while process.poll() is None:
            if is_cancelled():
                _terminate_process(process)
                raise TaskCancelled("任务已取消")
            if is_stopping():
                _terminate_process(process)
                raise WorkerStopping("服务正在停止")
            time.sleep(0.2)
        stderr = process.stderr.read() if process.stderr else ""
        if process.returncode != 0:
            raise MediaValidationError(
                f"音频预处理失败：{_clean_process_error(stderr)}"
            )
    finally:
        if process.poll() is None:
            _terminate_process(process)
        if process.stderr:
            process.stderr.close()

    if not destination.exists() or destination.stat().st_size == 0:
        raise MediaValidationError("音频预处理没有生成有效文件")


def _terminate_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _clean_process_error(value: str | None) -> str:
    detail = (value or "未知错误").strip().replace("\x00", "")
    return detail[-1000:] or "未知错误"
