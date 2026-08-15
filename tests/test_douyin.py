from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import httpx
import pytest

import app.douyin as douyin
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
    保留 headers、follow_redirects、event_hooks 等构造参数，
    使下载链路能校验请求头与逐跳重定向。
    """
    original_client = httpx.Client

    def factory(**kwargs):
        headers = kwargs.pop("headers", None)
        return original_client(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(5.0),
            headers=headers,
            follow_redirects=kwargs.pop("follow_redirects", False),
            event_hooks=kwargs.pop("event_hooks", None),
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
        "https://6f10f285.ydycdn.com:7179/video/tos/cn",
        "https://wxsz4c.z.cjjd14.com:33443/video/tos/cn",
        "https://d70782a7.v.smtcdns.com/video/tos/cn",
        "https://k8y82df5rlhby.v1d.pkoplink.com/video/tos/cn",
        "https://2027728267.qrstuvwxyzab.com/video/tos/cn",
        "https://65bf9670-9.sjxydc.com/video/tos/cn",
        "https://bbvhbbbhjghk.jspcdn.cn:20443/video/tos/cn",
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


DETAIL_JSON = """
{
  "aweme_detail": {
    "desc": "测试视频标题 #话题",
    "duration": 244967,
    "video": {
      "play_addr": {
        "uri": "v0300fg",
        "url_list": [
          "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0300fg10000d9epqqfog65jtp8aa94g&ratio=720p&line=0"
        ]
      }
    }
  }
}
"""


def test_fetch_video_info_parses_share_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ttwid.bytedance.com":
            return httpx.Response(
                200, headers={"set-cookie": "ttwid=1%7Cabc; Path=/"}, text="{}"
            )
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(500, text="boom")
        assert request.url.host == "www.iesdouyin.com"
        return httpx.Response(200, text=SHARE_HTML)

    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)
    monkeypatch.setattr("app.douyin._ttwid_cache", None)
    video = fetch_video_info("7664455344306834715", mock_client(handler))
    assert video.video_id == "7664455344306834715"
    assert video.title == "测试视频标题 #话题"
    assert video.duration_ms == 244967
    assert "/play/" in video.play_url
    assert "playwm" not in video.play_url


def test_fetch_video_info_uses_detail_api_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        if request.url.host == "ttwid.bytedance.com":
            return httpx.Response(
                200,
                headers={"set-cookie": "ttwid=1%7Cabc%7C123%7Cfff; Path=/"},
                text="{}",
            )
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            assert "ttwid=1%7Cabc" in request.headers.get("cookie", "")
            return httpx.Response(200, text=DETAIL_JSON)
        return httpx.Response(200, text=SHARE_HTML)

    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
    monkeypatch.setattr("app.douyin._ttwid_cache", None)
    video = fetch_video_info("7664455344306834715", mock_client(handler))
    assert video.title == "测试视频标题 #话题"
    assert video.duration_ms == 244967
    assert "/play/" in video.play_url
    assert any("aweme/v1/web/aweme/detail/" in url for url in seen)
    assert not any("iesdouyin.com/share/video" in url for url in seen)


def test_fetch_video_info_falls_back_to_share_page_on_detail_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detail API 失败时应回退到分享页解析。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=SHARE_HTML)

    monkeypatch.setattr("app.douyin._ttwid_cache", "ttwid=cached")
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
    video = fetch_video_info("7664455344306834715", mock_client(handler))
    assert video.title == "测试视频标题 #话题"


def test_fetch_video_info_cleans_control_characters_in_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = SHARE_HTML.replace(
        "测试视频标题 #话题", "有换行\\n和制表符\\t的标题 \\r\\n 再来一个"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=html)

    monkeypatch.setattr("app.douyin._ttwid_cache", "ttwid=cached")
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
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


def test_pick_user_agent_is_stable_and_diverse() -> None:
    first = douyin._pick_user_agent("https://v.douyin.com/FAV7NYgWNuE/")
    assert douyin._pick_user_agent("https://v.douyin.com/FAV7NYgWNuE/") == first
    seen = {douyin._pick_user_agent(f"seed-{index}") for index in range(100)}
    assert len(seen) >= 2


def test_headers_for_includes_cookie_only_when_provided() -> None:
    with_cookie = douyin._headers_for("https://v.douyin.com/abc/", "sessionid=xyz")
    assert with_cookie["Cookie"] == "sessionid=xyz"
    assert with_cookie["User-Agent"]
    without_cookie = douyin._headers_for("https://v.douyin.com/abc/")
    assert "Cookie" not in without_cookie


def test_fetch_video_info_passes_cookie_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie") or "")
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(200, text=DETAIL_JSON)
        return httpx.Response(200, text=SHARE_HTML)

    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
    video = fetch_video_info(
        "7664455344306834715",
        mock_client(handler),
        cookie="sessionid=abc; tt_webid=42",
    )
    assert video.video_id == "7664455344306834715"
    assert seen and all("sessionid=abc" in value for value in seen)


def test_fetch_video_info_prefers_full_share_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整分享 URL（带参数）优先，失败后回退裸分享页。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(500, text="boom")
        if "share_sign" in request.url.query.decode():
            return httpx.Response(200, text="<html><body>empty</body></html>")
        return httpx.Response(200, text=SHARE_HTML)

    monkeypatch.setattr("app.douyin._ttwid_cache", "ttwid=cached")
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
    video = fetch_video_info(
        "7664455344306834715",
        mock_client(handler),
        share_url=(
            "https://www.iesdouyin.com/share/video/7664455344306834715/"
            "?region=CN&mid=1&share_sign=abc"
        ),
    )
    assert video.title == "测试视频标题 #话题"
    assert any("share_sign=abc" in url for url in seen)
    assert seen[-1].endswith("/share/video/7664455344306834715/")


def test_fetch_video_info_reports_wind_control_on_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(403, text="<html></html>")
        return httpx.Response(403, text="<html></html>")

    monkeypatch.setattr("app.douyin._ttwid_cache", "ttwid=cached")
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
    with pytest.raises(MediaValidationError, match="风控"):
        fetch_video_info("7664455344306834715", mock_client(handler))


def test_fetch_video_info_uses_minute_backoff_on_waf_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAF JS Challenge 壳页应触发分钟级退避而非秒级短退避。"""
    backoffs: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(200, text="")
        return httpx.Response(
            200,
            text=(
                "<html><head><script src="
                '"https://lf-waf-js.byted-static.com/obj/waf-jschallenge/'
                'out-sha256.js"></script></head><body></body></html>'
            ),
        )

    monkeypatch.setattr("app.douyin._ttwid_cache", "ttwid=cached")
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.douyin._sleep_interruptible",
        lambda seconds, **kwargs: backoffs.append(seconds),
    )
    with pytest.raises(MediaValidationError, match="风控"):
        fetch_video_info("7664455344306834715", mock_client(handler))
    assert backoffs and all(value >= 30.0 for value in backoffs)


def test_fetch_video_info_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(200, text="")
        if calls["count"] <= 3:
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=SHARE_HTML)

    monkeypatch.setattr("app.douyin._ttwid_cache", "ttwid=cached")
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
    video = fetch_video_info("7664455344306834715", mock_client(handler))
    assert video.title == "测试视频标题 #话题"
    assert calls["count"] >= 3


def test_download_video_rejects_redirect_to_unknown_cdn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载重定向链落到白名单外域名时必须拒绝（逐跳校验）。"""
    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "aweme.snssdk.com":
            return httpx.Response(
                302, headers={"location": "https://evil-cdn.example/video.mp4"}
            )
        return httpx.Response(200, content=b"fake-video-bytes-")

    patch_httpx_client(monkeypatch, handler)
    destination = tmp_path / "source.mp4"

    with pytest.raises(MediaValidationError, match="白名单"):
        download_video(
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=x",
            destination,
            size_limit=10**7,
            on_progress=lambda _size, _total: None,
            is_cancelled=lambda: False,
            is_stopping=lambda: False,
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_video_accepts_byte_cdn_redirect_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重定向链经过字节系 CDN（ydycdn.com）时应被放行。"""
    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)
    content = b"fake-video-bytes-" * 50

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "aweme.snssdk.com":
            return httpx.Response(
                302, headers={"location": "https://v95-aw-cold.douyinvod.com/video/tos"}
            )
        if request.url.host == "v95-aw-cold.douyinvod.com":
            return httpx.Response(
                302, headers={"location": "https://6f10f285.ydycdn.com:7179/video/tos"}
            )
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
    assert digest == hashlib.sha256(content).hexdigest()
    assert destination.read_bytes() == content


def test_resolve_douyin_share_passes_resolved_url_with_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总入口应把短链重定向后的完整 URL（带参数）传给分享页解析。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://www.iesdouyin.com/share/video/7664455344306834715/"
                        "?region=CN&mid=42&share_sign=xyz"
                    )
                },
            )
        if request.url.host == "ttwid.bytedance.com":
            return httpx.Response(
                200, headers={"set-cookie": "ttwid=1%7Cabc; Path=/"}, text="{}"
            )
        if request.url.path.startswith("/aweme/v1/web/aweme/detail/"):
            return httpx.Response(500, text="boom")
        if "share_sign=xyz" in str(request.url):
            return httpx.Response(200, text=SHARE_HTML)
        return httpx.Response(200, text="<html></html>")

    patch_httpx_client(monkeypatch, handler)
    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)
    monkeypatch.setattr("app.douyin._ttwid_cache", None)
    monkeypatch.setattr("app.douyin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.douyin._sleep_interruptible", lambda *a, **k: None)
    video = douyin.resolve_douyin_share(
        "https://v.douyin.com/FAV7NYgWNuE/",
        is_cancelled=lambda: False,
        is_stopping=lambda: False,
    )
    assert video.title == "测试视频标题 #话题"
    assert any("share_sign=xyz" in url for url in seen)


def test_download_video_passes_cookie_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"fake-video-bytes-" * 50
    monkeypatch.setattr("app.douyin._ensure_public_resolution", lambda url: None)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie") or "")
        return httpx.Response(
            200, headers={"content-length": str(len(content))}, content=content
        )

    patch_httpx_client(monkeypatch, handler)
    destination = tmp_path / "source.mp4"

    download_video(
        "https://aweme.snssdk.com/aweme/v1/play/?video_id=x",
        destination,
        size_limit=10**7,
        on_progress=lambda _size, _total: None,
        is_cancelled=lambda: False,
        is_stopping=lambda: False,
        cookie="sessionid=abc",
    )

    assert seen and all("sessionid=abc" in value for value in seen)
