from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx

from app.domain import MediaValidationError, TaskCancelled, WorkerStopping

logger = logging.getLogger(__name__)

# 分享口令文本常混有中英文标点，从第一个 http(s) 起截到空白或标点为止。
_URL_PATTERN = re.compile(r"https?://[^\s　，,；;：:！!？?、()（）]+", re.I)
_VIDEO_ID_PATTERNS = (
    re.compile(r"/(?:share/)?video/(\d{5,})"),
    re.compile(r"/(?:share/)?note/(\d{5,})"),
)
_PLAYWM_MARKER = "/playwm/"
_PLAY_MARKER = "/play/"

# 解析与下载链路允许的域名（后缀匹配）。只放行抖音及其 CDN，
# 防止内网服务被诱导请求到非预期目标（SSRF）。
ALLOWED_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
    "snssdk.com",
    "douyinvod.com",
    "xhscdn.com",
)

_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Referer": "https://www.douyin.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_MAX_REDIRECTS = 5
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_INFO_BYTES = 2 * 1024 * 1024
_INFO_RETRIES = 2


@dataclass(slots=True)
class DouyinVideo:
    video_id: str
    title: str
    duration_ms: int
    play_url: str


def extract_share_url(text: str) -> str | None:
    """从分享口令或任意文本中提取第一个抖音链接。"""
    for match in _URL_PATTERN.finditer(text or ""):
        url = match.group(0).rstrip("/")
        if _hostname_matches(url, ("douyin.com",)):
            return url
    return None


def validate_source_url(url: str) -> str:
    """校验链接属于域名白名单，返回规范 URL。"""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MediaValidationError("链接必须是有效的 https 地址")
    if not _hostname_matches(url, ALLOWED_DOMAINS):
        raise MediaValidationError("仅支持抖音分享链接")
    return url


def resolve_douyin_share(
    text: str,
    *,
    is_cancelled: Callable[[], bool],
    is_stopping: Callable[[], bool],
) -> DouyinVideo:
    """总入口：从口令/链接解析出视频信息（无水印播放地址）。"""
    if is_cancelled():
        raise TaskCancelled("任务已取消")
    if is_stopping():
        raise WorkerStopping("服务正在停止")
    share_url = extract_share_url(text) or (
        text.strip() if text.strip().startswith("http") else None
    )
    if not share_url:
        raise MediaValidationError("未找到抖音分享链接")
    validate_source_url(share_url)
    with httpx.Client(
        timeout=_TIMEOUT, follow_redirects=False, headers=_HEADERS
    ) as client:
        video_id = resolve_video_id(share_url, client)
        if is_cancelled():
            raise TaskCancelled("任务已取消")
        if is_stopping():
            raise WorkerStopping("服务正在停止")
        video = fetch_video_info(video_id, client)
    return video


def resolve_video_id(share_url: str, client: httpx.Client) -> str:
    """手动逐跳跟随重定向（每跳都校验域名）并提取视频 ID。"""
    current = validate_source_url(share_url)
    for _ in range(_MAX_REDIRECTS):
        for pattern in _VIDEO_ID_PATTERNS:
            match = pattern.search(current)
            if match:
                return match.group(1)
        response = _request(client, "GET", current)
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        location = response.headers.get("location")
        if not location:
            break
        current = validate_source_url(urljoin(current, location))
    for pattern in _VIDEO_ID_PATTERNS:
        match = pattern.search(current)
        if match:
            return match.group(1)
    raise MediaValidationError("无法从抖音链接中识别视频 ID")


def fetch_video_info(video_id: str, client: httpx.Client) -> DouyinVideo:
    """从分享页内嵌数据获取标题、时长与无水印播放地址。"""
    last_error: Exception | None = None
    for attempt in range(_INFO_RETRIES):
        try:
            return _parse_share_page(video_id, client)
        except MediaValidationError as error:
            last_error = error
        if attempt + 1 < _INFO_RETRIES:
            # 分享页偶发风控空响应，短退避后重试一次。
            time.sleep(0.5 * (attempt + 1))
    raise MediaValidationError(f"无法解析抖音视频信息：{last_error}") from last_error


def download_video(
    play_url: str,
    destination: Path,
    *,
    size_limit: int,
    on_progress: Callable[[int, int | None], None],
    is_cancelled: Callable[[], bool],
    is_stopping: Callable[[], bool],
) -> tuple[int, str]:
    """流式下载视频到临时文件并原子替换为 destination，返回 (字节数, SHA-256)。

    成功时 destination 存在且完整；任何异常时临时文件已清理，destination 不被触碰。
    """
    validate_source_url(play_url)
    _ensure_public_resolution(play_url)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.download")
    try:
        with httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            with client.stream("GET", play_url) as response:
                if response.status_code not in (200, 206):
                    raise MediaValidationError(
                        f"视频下载失败：HTTP {response.status_code}"
                    )
                # CDN 可能重定向到其它视频域，落点域名与解析结果同样要过白名单。
                validate_source_url(str(response.url))
                _ensure_public_resolution(str(response.url))
                content_length = response.headers.get("content-length")
                total: int | None = None
                if content_length and content_length.isdigit():
                    total = int(content_length)
                size = 0
                digest = hashlib.sha256()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                        if is_cancelled():
                            raise TaskCancelled("任务已取消")
                        if is_stopping():
                            raise WorkerStopping("服务正在停止")
                        size += len(chunk)
                        if size > size_limit:
                            raise MediaValidationError(
                                f"视频超过上传大小限制 {size_limit} 字节"
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        on_progress(size, total)
                    handle.flush()
                    os.fsync(handle.fileno())
        if size == 0:
            raise MediaValidationError("下载的视频为空")
        os.replace(temporary, destination)
        return size, digest.hexdigest()
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _parse_share_page(video_id: str, client: httpx.Client) -> DouyinVideo:
    url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    response = _request(client, "GET", url)
    if response.status_code != 200:
        raise MediaValidationError(f"抖音分享页返回异常：HTTP {response.status_code}")
    if len(response.content) > _MAX_INFO_BYTES or not response.content:
        raise MediaValidationError("抖音分享页未返回视频数据")
    item = _extract_item_from_router_data(response.text)
    if item is None:
        raise MediaValidationError("抖音分享页缺少视频信息")
    return _item_to_video(item, video_id)


def _extract_item_from_router_data(html: str) -> dict[str, object] | None:
    match = re.search(
        r"(?:window\.)?_ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>",
        html,
        re.S,
    )
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    loader = payload.get("loaderData") or {}
    for value in loader.values():
        if not isinstance(value, dict):
            continue
        video_info = value.get("videoInfoRes") or {}
        items = video_info.get("item_list") or []
        if items:
            return items[0]
    return None


def _item_to_video(item: dict[str, object], video_id: str) -> DouyinVideo:
    video = item.get("video") or {}
    play_addr = video.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    play_url = url_list[0] if url_list else None
    if not isinstance(play_url, str) or not play_url:
        raise MediaValidationError("抖音分享页缺少播放地址")
    # 播放接口默认带水印（playwm），替换为无 WM 标记的 play 接口。
    play_url = play_url.replace(_PLAYWM_MARKER, _PLAY_MARKER)
    title = _clean_title(str(item.get("desc") or ""))
    try:
        duration_ms = int(video.get("duration") or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    return DouyinVideo(
        video_id=video_id,
        title=title,
        duration_ms=duration_ms,
        play_url=play_url,
    )


def _clean_title(title: str) -> str:
    """标题会用作任务显示名和下载文件名，控制字符与连续空白一律折叠为空格。"""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", title)
    return re.sub(r"\s+", " ", cleaned).strip()


def _request(client: httpx.Client, method: str, url: str) -> httpx.Response:
    try:
        response = client.request(method, url)
    except httpx.TimeoutException as error:
        raise MediaValidationError("访问抖音链接超时") from error
    except httpx.HTTPError as error:
        raise MediaValidationError(f"访问抖音链接失败：{_safe_error(error)}") from error
    return response


def _ensure_public_resolution(url: str) -> None:
    """解析域名并要求所有地址都是公网 IP，防止 DNS 重绑定指向内网。"""
    hostname = urlparse(url).hostname or ""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as error:
        raise MediaValidationError(f"无法解析视频服务器地址：{hostname}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise MediaValidationError("视频服务器地址异常，已拒绝下载")


def _hostname_matches(url: str, domains: tuple[str, ...]) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in domains
    )


def _safe_error(error: Exception) -> str:
    message = str(error).replace("\x00", "").strip()
    return message[-1500:] or error.__class__.__name__
