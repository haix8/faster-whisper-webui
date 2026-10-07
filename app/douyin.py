from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import socket
import threading
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

# 抖音 WAF JS Challenge 壳页的特征串。命中只用于给"没有视频数据的页面"
# 分类错误类型（风控需要分钟级冷却，普通短退避无效）。
# 注意：argus-csp-token 也会出现在正常分享页的预加载脚本 nonce 上，
# 因此必须优先尝试解析内嵌数据，解析不到再按风控处理。
_WAF_MARKERS = ("waf-jschallenge", "byted-static.com", "argus-csp-token")

# 解析与下载链路允许的域名（后缀匹配）。只放行抖音及其 CDN，
# 防止内网服务被诱导请求到非预期目标（SSRF）。
# 下载重定向链末端会落到字节系 CDN 域名（如 *.ydycdn.com），
# 这些域名随 CDN 调度变化，后缀匹配只放行观测到的字节 CDN 域名族。
ALLOWED_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
    "snssdk.com",
    "douyinvod.com",
    "xhscdn.com",
    "ydycdn.com",
    "cjjd14.com",
    "smtcdns.com",
    "pkoplink.com",
    "qrstuvwxyzab.com",
    "sjxydc.com",
    "jspcdn.cn",
)

# 移动端与桌面 UA 池，按链接/视频 ID 哈希选择，打散指纹降低风控命中率。
_USER_AGENTS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; 22081212C Build/SKQ1.211006.001) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_MAX_REDIRECTS = 5
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_INFO_BYTES = 2 * 1024 * 1024
_INFO_RETRIES = 3
# 普通解析失败退避（秒）；WAF 风控退避单独用 _WAF_RETRY_BACKOFF，分钟级。
_WAF_RETRY_BACKOFF = (30.0, 60.0, 120.0)
# 分享页请求最小间隔（秒），降低高频触发 WAF 的概率。
_SHARE_MIN_INTERVAL = 2.0

# 分享页候选解析路径（不带参数的裸路径，作为完整分享 URL 的回退）。
_SHARE_PAGES = ("https://www.iesdouyin.com/share/video/{video_id}/",)

# 官方 Web API：作品详情接口。配合匿名会话 Cookie（ttwid）即可返回完整数据，
# 实测无需 a_bogus 签名、无需登录态。域名与参数保持浏览器端一致。
_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_DETAIL_API_PARAMS = (
    "device_platform=webapp&aid=6383&channel=channel_pc_web"
    "&version_code=190500&version_name=19.5.0&cookie_enabled=true"
    "&platform=PC&downlink=10"
)
# ttwid 匿名会话 Cookie 注册接口（字节跳动官方，免费）。
_TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_REGISTER_BODY = {
    "region": "cn",
    "aid": 6383,
    "needFid": False,
    "service": "www.ixigua.com",
    "migrate_info": {"ticket": "", "source": "node"},
    "cbUrlProtocol": "https",
    "union": True,
}
# ttwid 会话缓存：进程内缓存，12 小时过期后重新注册。
_TTWID_TTL_SECONDS = 12 * 3600
_ttwid_cache: str | None = None
_ttwid_cache_expires_at = 0.0
_ttwid_cache_lock = threading.Lock()
# detail API 使用桌面 Chrome UA（PC Web 接口）。
_DETAIL_API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)


def _pick_user_agent(seed: str) -> str:
    digest = hashlib.sha256((seed or "").encode("utf-8")).digest()
    return _USER_AGENTS[int.from_bytes(digest[:4], "big") % len(_USER_AGENTS)]


def _headers_for(seed: str, cookie: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": _pick_user_agent(seed),
        "Referer": "https://www.douyin.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


@dataclass(slots=True)
class DouyinVideo:
    video_id: str
    title: str
    duration_ms: int
    play_url: str


class DouyinRateLimitError(MediaValidationError):
    """命中抖音 WAF/风控（JS Challenge 壳页、403、429），需要分钟级冷却。"""


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
    cookie: str | None = None,
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
        timeout=_TIMEOUT,
        follow_redirects=False,
        headers=_headers_for(share_url, cookie),
    ) as client:
        video_id, resolved_url = _resolve_share_target(share_url, client)
        if is_cancelled():
            raise TaskCancelled("任务已取消")
        if is_stopping():
            raise WorkerStopping("服务正在停止")
        video = fetch_video_info(
            video_id,
            client,
            cookie=cookie,
            share_url=resolved_url,
            is_cancelled=is_cancelled,
            is_stopping=is_stopping,
        )
    return video


def resolve_video_id(share_url: str, client: httpx.Client) -> str:
    """手动逐跳跟随重定向（每跳都校验域名）并提取视频 ID。"""
    video_id, _ = _resolve_share_target(share_url, client)
    return video_id


def _resolve_share_target(share_url: str, client: httpx.Client) -> tuple[str, str]:
    """逐跳跟随重定向，返回 (video_id, 完整分享页 URL)。

    完整分享页 URL 携带 region/mid/share_sign 等参数，请求时保留参数
    更接近真实浏览器行为，能显著提高 SSR 数据返回率。
    """
    current = validate_source_url(share_url)
    for _ in range(_MAX_REDIRECTS):
        for pattern in _VIDEO_ID_PATTERNS:
            match = pattern.search(current)
            if match:
                return match.group(1), current
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
            return match.group(1), current
    raise MediaValidationError("无法从抖音链接中识别视频 ID")


def fetch_video_info(
    video_id: str,
    client: httpx.Client,
    *,
    cookie: str | None = None,
    share_url: str | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    is_stopping: Callable[[], bool] | None = None,
) -> DouyinVideo:
    """获取标题、时长与无水印播放地址。

    优先走官方 detail API（匿名 ttwid 会话，实测无需签名），失败后回退
    分享页 SSR 解析（完整分享 URL → 裸分享页）。WAF 风控错误使用分钟级退避。
    """
    last_error: Exception | None = None
    for attempt in range(_INFO_RETRIES):
        try:
            return _fetch_video_info_paths(
                video_id, client, cookie=cookie, share_url=share_url
            )
        except DouyinRateLimitError as error:
            last_error = error
            backoff = _WAF_RETRY_BACKOFF[min(attempt, len(_WAF_RETRY_BACKOFF) - 1)]
        except MediaValidationError as error:
            last_error = error
            backoff = 0.8 * (2**attempt)
        if attempt + 1 < _INFO_RETRIES:
            # 分享页偶发风控空响应，指数退避加随机抖动后重试；
            # WAF 壳页需要分钟级冷却，退避更长。
            _sleep_interruptible(
                backoff + random.uniform(0, backoff * 0.25),
                is_cancelled=is_cancelled,
                is_stopping=is_stopping,
            )
    raise MediaValidationError(f"无法解析抖音视频信息：{last_error}") from last_error


def _fetch_video_info_paths(
    video_id: str,
    client: httpx.Client,
    *,
    cookie: str | None = None,
    share_url: str | None = None,
) -> DouyinVideo:
    """按优先级依次尝试：detail API → 分享页 SSR 解析。"""
    last_error: MediaValidationError | None = None
    try:
        return _fetch_from_detail_api(video_id, client, cookie=cookie)
    except MediaValidationError as error:
        last_error = error
    try:
        return _parse_share_page(video_id, client, cookie=cookie, share_url=share_url)
    except MediaValidationError as error:
        last_error = error
    raise last_error if last_error else MediaValidationError("抖音视频信息解析失败")


def _get_ttwid(client: httpx.Client) -> str:
    """获取匿名会话 Cookie（ttwid），进程内缓存 12 小时。"""
    global _ttwid_cache, _ttwid_cache_expires_at
    with _ttwid_cache_lock:
        now = time.monotonic()
        if _ttwid_cache and now < _ttwid_cache_expires_at:
            return _ttwid_cache
        _ensure_public_resolution(_TTWID_REGISTER_URL)
        try:
            response = client.post(
                _TTWID_REGISTER_URL,
                json=_TTWID_REGISTER_BODY,
                headers={
                    "User-Agent": _DETAIL_API_UA,
                    "Referer": "https://www.iesdouyin.com/",
                },
            )
        except httpx.TimeoutException as error:
            raise MediaValidationError("访问抖音链接超时") from error
        except httpx.HTTPError as error:
            raise MediaValidationError(
                f"获取抖音会话标识失败：{_safe_error(error)}"
            ) from error
        if response.status_code != 200:
            raise MediaValidationError(
                f"获取抖音会话标识失败：HTTP {response.status_code}"
            )
        set_cookie = response.headers.get("set-cookie", "")
        match = re.search(r"(ttwid=[^;\s]+)", set_cookie)
        if not match:
            raise MediaValidationError("抖音会话标识响应缺少 ttwid")
        _ttwid_cache = match.group(1)
        _ttwid_cache_expires_at = now + _TTWID_TTL_SECONDS
        return _ttwid_cache


def _fetch_from_detail_api(
    video_id: str,
    client: httpx.Client,
    *,
    cookie: str | None = None,
) -> DouyinVideo:
    """调用官方 detail API 获取视频信息（无需签名，带匿名 ttwid 会话）。"""
    _throttle_share_request()
    if cookie:
        headers = {
            "User-Agent": _DETAIL_API_UA,
            "Referer": "https://www.douyin.com/",
            "Cookie": cookie,
        }
    else:
        headers = {
            "User-Agent": _DETAIL_API_UA,
            "Referer": "https://www.douyin.com/",
            "Cookie": _get_ttwid(client),
        }
    url = f"{_DETAIL_API}?aweme_id={video_id}&{_DETAIL_API_PARAMS}"
    response = _request(client, "GET", url, headers=headers)
    if response.status_code in (403, 429):
        raise DouyinRateLimitError("访问抖音被限制，疑似触发平台风控，请稍后重试")
    if response.status_code != 200:
        raise MediaValidationError(f"抖音详情接口返回异常：HTTP {response.status_code}")
    if not response.content or len(response.content) > _MAX_INFO_BYTES:
        raise DouyinRateLimitError("疑似被风控拦截，详情接口未返回数据，稍后重试")
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise MediaValidationError("抖音详情接口返回非 JSON 数据") from error
    detail = payload.get("aweme_detail") or {}
    if not detail:
        raise MediaValidationError("抖音详情接口缺少视频信息")
    return _detail_to_video(detail, video_id)


def _detail_to_video(detail: dict[str, object], video_id: str) -> DouyinVideo:
    """把 detail API 的 aweme_detail 转成 DouyinVideo。"""
    video = detail.get("video") or {}
    play_addr = video.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    if not url_list:
        # 部分响应把地址放在 bit_rate[0]（高清列表）下。
        bit_rate = video.get("bit_rate") or []
        if isinstance(bit_rate, list) and bit_rate:
            play_addr = bit_rate[0].get("play_addr") or {}
            url_list = play_addr.get("url_list") or []
    play_url = url_list[0] if url_list else None
    if not isinstance(play_url, str) or not play_url:
        raise MediaValidationError("抖音详情接口缺少播放地址")
    play_url = play_url.replace(_PLAYWM_MARKER, _PLAY_MARKER)
    title = _clean_title(str(detail.get("desc") or ""))
    try:
        duration_ms = int(detail.get("duration") or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    return DouyinVideo(
        video_id=video_id,
        title=title,
        duration_ms=duration_ms,
        play_url=play_url,
    )


def _sleep_interruptible(
    seconds: float,
    *,
    is_cancelled: Callable[[], bool] | None,
    is_stopping: Callable[[], bool] | None,
) -> None:
    """分片睡眠，支持取消与停止检查（最长退避 120s，不能阻塞取消）。"""
    deadline = time.monotonic() + seconds
    while True:
        if is_cancelled and is_cancelled():
            raise TaskCancelled("任务已取消")
        if is_stopping and is_stopping():
            raise WorkerStopping("服务正在停止")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def download_video(
    play_url: str,
    destination: Path,
    *,
    size_limit: int,
    on_progress: Callable[[int, int | None], None],
    is_cancelled: Callable[[], bool],
    is_stopping: Callable[[], bool],
    cookie: str | None = None,
) -> tuple[int, str]:
    """流式下载视频到临时文件并原子替换为 destination，返回 (字节数, SHA-256)。

    成功时 destination 存在且完整；任何异常时临时文件已清理，destination 不被触碰。
    重定向链的每一跳都校验域名白名单与公网 IP，防止 CDN 跳转绕过 SSRF 防护。
    """
    validate_source_url(play_url)
    _ensure_public_resolution(play_url)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.download")
    try:
        with httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=_headers_for(play_url, cookie),
            event_hooks={"response": [_validate_redirect_hop]},
        ) as client:
            with client.stream("GET", play_url) as response:
                if response.status_code not in (200, 206):
                    raise MediaValidationError(
                        f"视频下载失败：HTTP {response.status_code}"
                    )
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


def _validate_redirect_hop(response: httpx.Response) -> None:
    """重定向链每一跳的域名白名单与公网 IP 校验（httpx event hook）。"""
    url = str(response.url)
    if not _hostname_matches(url, ALLOWED_DOMAINS):
        raise MediaValidationError(
            f"视频下载落点域名不在白名单：{urlparse(url).hostname}"
        )
    _ensure_public_resolution(url)


def _parse_share_page(
    video_id: str,
    client: httpx.Client,
    *,
    cookie: str | None = None,
    share_url: str | None = None,
) -> DouyinVideo:
    """请求分享页并解析内嵌数据。

    候选顺序：完整分享 URL（带参数，若提供）→ 裸 iesdouyin 分享页。
    """
    candidates = [share_url] if share_url else []
    candidates.extend(page.format(video_id=video_id) for page in _SHARE_PAGES)
    last_error: MediaValidationError | None = None
    seen: set[str] = set()
    for page_url in candidates:
        if page_url in seen:
            continue
        seen.add(page_url)
        try:
            response = _request_share_page(page_url, client, cookie=cookie)
            if response.status_code in (403, 429):
                raise DouyinRateLimitError(
                    "访问抖音被限制，疑似触发平台风控，请稍后重试"
                )
            if response.status_code != 200:
                raise MediaValidationError(
                    f"抖音分享页返回异常：HTTP {response.status_code}"
                )
            if not response.content or len(response.content) > _MAX_INFO_BYTES:
                raise DouyinRateLimitError("疑似被风控拦截，分享页未返回数据，稍后重试")
            # 正常分享页也可能带 argus 预加载脚本（argus-csp-token 只是脚本 nonce），
            # 所以先尝试解析内嵌数据；能拿到视频信息就不按风控处理。
            item = _extract_item_from_router_data(response.text)
            if item is not None:
                return _item_to_video(item, video_id)
            if _is_waf_challenge(response.text):
                raise DouyinRateLimitError("触发抖音风控验证（WAF 拦截），请稍后重试")
            raise MediaValidationError("抖音分享页缺少视频信息")
        except MediaValidationError as error:
            last_error = error
    # 保留风控错误类型，让外层 fetch_video_info 使用 WAF 分钟级退避。
    if isinstance(last_error, DouyinRateLimitError):
        raise last_error
    raise MediaValidationError(f"抖音分享页解析失败：{last_error}") from last_error


_last_share_request_at = 0.0
_share_request_lock = threading.Lock()


def _throttle_share_request() -> None:
    """分享页/详情接口请求前的最小间隔节流，降低高频触发 WAF 的概率。"""
    global _last_share_request_at
    with _share_request_lock:
        now = time.monotonic()
        wait = _last_share_request_at + _SHARE_MIN_INTERVAL - now
        _last_share_request_at = now + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


def _request_share_page(
    url: str, client: httpx.Client, *, cookie: str | None = None
) -> httpx.Response:
    """请求分享页（带节流）。"""
    _throttle_share_request()
    return _request(client, "GET", url, headers=_headers_for(url, cookie))


def _is_waf_challenge(html: str) -> bool:
    return any(marker in html for marker in _WAF_MARKERS)


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


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        response = client.request(method, url, headers=headers)
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
