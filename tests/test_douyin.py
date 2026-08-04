from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import httpx
import pytest

from app.domain import MediaValidationError, TaskCancelled
from app.douyin import (
    download_video,
    extract_share_url,
    fetch_video_info,
    resolve_video_id,
    validate_source_url,
)

SHARE_HTML = """
<!doctype html><html><head></head><body>
<script>window._ROUTER_DATA = {"loaderData":{"video_(id)\\/page":{"videoInfoRes":{"item_list":[{"desc":"测试视频标题 #话题","video":{"duration":244967,"play_addr":{"uri":"v0300fg","url_list":["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0300fg10000d9epqqfog65jtp8aa94g&ratio=720p&line=0"]}}}]}}}}</script>
</body></html>
"""

SHARE_TEXT = (
    "2.56 :1pm 11/11 GvS:/ b@n.dN 很多老板做企业最大的误区，"
    "是只看眼前利润。# 小米 # 企业经营  "
    "https://v.douyin.com/FAV7NYgWNuE/ 复制此链接，打开Dou音搜索，直接观看视频！"
)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(5.0),
    )


def patch_httpx_client(monkeypatch: pytest.MonkeyPatch, handler):
    """把 app.douyin 里对 httpx.Client 的构造替换为 MockTransport 客户端。

    必须先保存原类引用再替换，否则构造器内部递归调用被替换的 Client。
    """
    original_client = httpx.Client

    def factory(**kwargs):
        del kwargs
        return original_client(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(5.0),
        )

    monkeypatch.setattr("app.douyin.httpx.Client", factory)
    return factory


def test_extract_share_url_from_share_text() -> None:
    assert extract_share_url(SHARE_TEXT) == "https://v.douyin.com/FAV7NYgWNuE"


def test_extract_share_url_ignores_other_domains() -> None:
    assert extract_share_url("链接 https://example.com/video/123 文本") is None
    assert extract_share_url("") is None


def test_extract_share_url_cuts_at_ascii_punctuation() -> None:
    assert extract_share_url("看这里 https://v.douyin.com/AbCdEf/，继续") == (
        "https://v.douyin.com/AbCdEf"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://v.douyin.com/FAV7NYgWNuE/",
        "https://www.douyin.com/video/7664455344306834715",
        "https://www.iesdouyin.com/share/video/7664455344306834715/",
        "https://aweme.snssdk.com/aweme/v1/play/",
        "https://v95-aw-cold.douyinvod.com/video/tos/cn",
        "https://sns-video-bd.xhscdn.com/clips",
    ],
)
def test_validate_source_url_accepts_whitelisted_domains(url: str) -> None:
    assert validate_source_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video/1",
        "http://v.douyin.com/FAV7NYgWNuE/",
        "ftp://v.douyin.com/x",
        "https://evil-douyin.com/x",
        "https://douyin.com.evil.net/x",
        "https://",
    ],
)
def test_validate_source_url_rejects_others(url: str) -> None:
    with pytest.raises(MediaValidationError):
        validate_source_url(url)


def test_resolve_video_id_follows_short_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "v.douyin.com"
        return httpx.Response(
            302,
            headers={
                "location": (
                    "https://www.iesdouyin.com/share/video/7664455344306834715/"
                    "?region=CN&u_code=abc"
                )
            },
        )

    video_id = resolve_video_id(
        "https://v.douyin.com/FAV7NYgWNuE/", mock_client(handler)
    )
    assert video_id == "7664455344306834715"


def test_resolve_video_id_never_requests_full_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    video_id = resolve_video_id(
        "https://www.douyin.com/video/7664455344306834715", mock_client(handler)
    )
    assert video_id == "7664455344306834715"


def test_resolve_video_id_rejects_redirect_to_foreign_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://internal.corp.example/video/1"}
        )

    with pytest.raises(MediaValidationError):
        resolve_video_id("https://v.douyin.com/FAV7NYgWNuE/", mock_client(handler))


def test_fetch_video_info_parses_share_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.iesdouyin.com"
        return httpx.Response(200, text=SHARE_HTML)

    video = fetch_video_info("7664455344306834715", mock_client(handler))
    assert video.video_id == "7664455344306834715"
    assert video.title == "测试视频标题 #话题"
    assert video.duration_ms == 244967
    assert "/play/" in video.play_url
    assert "playwm" not in video.play_url


def test_fetch_video_info_cleans_control_characters_in_title() -> None:
    html = SHARE_HTML.replace(
        "测试视频标题 #话题", "有换行\\n和制表符\\t的标题 \\r\\n 再来一个"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    video = fetch_video_info("7664455344306834715", mock_client(handler))
    assert video.title == "有换行 和制表符 的标题 再来一个"
    assert "\n" not in video.title


def test_download_video_writes_file_and_returns_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"fake-video-bytes-" * 100
    expected_sha = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": str(len(content))}, content=content
        )

    patch_httpx_client(monkeypatch, handler)
    destination = tmp_path / "source.mp4"

    size, digest = download_video(
        "https://aweme.snssdk.com/aweme/v1/play/?video_id=x",
        destination,
        size_limit=10**7,
        on_progress=lambda _size, _total: None,
        is_cancelled=lambda: False,
        is_stopping=lambda: False,
    )

    assert size == len(content)
    assert digest == expected_sha
    assert destination.read_bytes() == content
    assert list(tmp_path.iterdir()) == [destination]


def test_download_video_rejects_oversize_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"x" * 1024
    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    patch_httpx_client(monkeypatch, handler)
    destination = tmp_path / "source.mp4"

    with pytest.raises(MediaValidationError):
        download_video(
            "https://aweme.snssdk.com/aweme/v1/play/",
            destination,
            size_limit=100,
            on_progress=lambda _size, _total: None,
            is_cancelled=lambda: False,
            is_stopping=lambda: False,
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_video_aborts_on_cancel(tmp_path: Path, monkeypatch) -> None:
    content = b"y" * 4096
    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    patch_httpx_client(monkeypatch, handler)
    destination = tmp_path / "source.mp4"
    cancelled = False

    def check_cancel() -> bool:
        nonlocal cancelled
        cancelled = True
        return True

    with pytest.raises(TaskCancelled):
        download_video(
            "https://aweme.snssdk.com/aweme/v1/play/",
            destination,
            size_limit=10**7,
            on_progress=lambda _size, _total: None,
            is_cancelled=check_cancel,
            is_stopping=lambda: False,
        )

    assert cancelled is True
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_public_resolution_rejects_private_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.douyin as douyin

    def fake_getaddrinfo(host: str, port: int):
        del host, port
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(MediaValidationError):
        douyin._ensure_public_resolution("https://aweme.snssdk.com/aweme/v1/play/")
