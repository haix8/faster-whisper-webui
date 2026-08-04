from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import MediaInfo, MediaValidationError
from app.douyin import DouyinVideo
from app.main import create_app
from tests.conftest import wav_bytes


def create_and_upload(
    client: TestClient,
    *,
    name: str = "meeting.wav",
    duration: float = 1.0,
    idempotency_key: str | None = None,
) -> dict:
    payload = wav_bytes(duration)
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    created = client.post(
        "/api/tasks",
        json={
            "file_name": name,
            "size_bytes": len(payload),
            "content_type": "audio/wav",
            "model": "tiny",
            "language": "zh",
        },
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    task = created.json()
    if task["status"] == "uploading":
        uploaded = client.put(
            f"/api/tasks/{task['id']}/source",
            content=payload,
            headers={"Content-Type": "audio/wav"},
        )
        assert uploaded.status_code == 200, uploaded.text
        task = uploaded.json()
    return task


def wait_for_status(
    client: TestClient,
    task_id: str,
    expected: set[str],
    timeout: float = 10,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in expected:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task did not reach {expected}")


def test_index_disables_browser_cache(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "/static/styles.css?v=0.1.6" in response.text
        assert "/static/upload-id.js?v=0.1.6" in response.text
        assert "/static/app.js?v=0.1.6" in response.text


def test_config_exposes_default_language(settings: Settings) -> None:
    configured = settings.model_copy(update={"default_language": "zh"})
    with TestClient(create_app(configured)) as client:
        config = client.get("/api/config").json()

    assert config["default_language"] == "zh"


def test_end_to_end_task_and_artifacts(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        task = create_and_upload(client, name="../../safe.wav")
        completed = wait_for_status(client, task["id"], {"succeeded"})

        assert completed["original_name"] == "safe.wav"
        assert completed["result_text"] == (
            "这是一个测试转写结果。Faster Whisper WebUI 已完成任务。"
        )
        assert completed["device"] == "cpu"

        listed = client.get("/api/tasks").json()
        assert listed["total"] == 1
        assert "result_text" not in listed["items"][0]

        for kind in ("json", "txt", "srt", "vtt"):
            artifact = client.get(f"/api/tasks/{task['id']}/artifacts/{kind}")
            assert artifact.status_code == 200
            assert artifact.content
        result = json.loads(client.get(f"/api/tasks/{task['id']}/artifacts/json").text)
        assert result["schema_version"] == 1
        assert len(result["segments"]) == 2


def test_idempotency_returns_existing_upload_task(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        body = {
            "file_name": "one.wav",
            "size_bytes": 100,
            "content_type": "audio/wav",
            "model": "tiny",
            "language": "auto",
        }
        first = client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": "same-key"}
        )
        second = client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": "same-key"}
        )
        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]


def test_idempotency_rejects_different_payload(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        body = {
            "file_name": "one.wav",
            "size_bytes": 100,
            "content_type": "audio/wav",
            "model": "tiny",
            "language": "auto",
        }
        assert (
            client.post(
                "/api/tasks", json=body, headers={"Idempotency-Key": "same-key"}
            ).status_code
            == 201
        )
        body["file_name"] = "different.wav"
        conflict = client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": "same-key"}
        )
        assert conflict.status_code == 409


def test_cancel_retry_and_delete(settings: Settings) -> None:
    slower = settings.model_copy(update={"fake_backend_delay_seconds": 0.35})
    with TestClient(create_app(slower)) as client:
        task = create_and_upload(
            client, duration=3, idempotency_key="cancel-retry-delete"
        )
        cancel = client.post(f"/api/tasks/{task['id']}/cancel")
        assert cancel.status_code == 200
        cancelled = wait_for_status(client, task["id"], {"cancelled"})
        assert cancelled["actions"]["retry"] is True

        retried = client.post(f"/api/tasks/{task['id']}/retry")
        assert retried.status_code == 200
        wait_for_status(client, task["id"], {"succeeded"})

        deleted = client.delete(f"/api/tasks/{task['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/tasks/{task['id']}").status_code == 404

        recreated = create_and_upload(client, idempotency_key="cancel-retry-delete")
        assert recreated["id"] != task["id"]


def test_completed_task_survives_app_restart(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        task = create_and_upload(client)
        wait_for_status(client, task["id"], {"succeeded"})

    with TestClient(create_app(settings)) as restarted:
        restored = restarted.get(f"/api/tasks/{task['id']}")
        assert restored.status_code == 200
        assert restored.json()["status"] == "succeeded"


def test_link_task_creation_validation(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/tasks",
            json={
                "source_url": "https://v.douyin.com/FAV7NYgWNuE/",
                "model": "tiny",
                "language": "auto",
            },
        )
        assert created.status_code == 201
        task = created.json()
        assert task["status"] == "queued"
        assert task["source_url"] == "https://v.douyin.com/FAV7NYgWNuE"
        assert task["original_name"] == "抖音视频"
        assert task["actions"]["cancel"] is True
        assert task["actions"]["retry"] is False
        assert task["artifacts"] == {}

        foreign = client.post(
            "/api/tasks",
            json={
                "source_url": "https://example.com/video/1",
                "model": "tiny",
                "language": "auto",
            },
        )
        assert foreign.status_code == 422

        plain_http = client.post(
            "/api/tasks",
            json={
                "source_url": "http://v.douyin.com/FAV7NYgWNuE/",
                "model": "tiny",
                "language": "auto",
            },
        )
        assert plain_http.status_code == 422

        missing = client.post(
            "/api/tasks",
            json={"model": "tiny", "language": "auto"},
        )
        assert missing.status_code == 422

        both = client.post(
            "/api/tasks",
            json={
                "file_name": "a.wav",
                "source_url": "https://v.douyin.com/FAV7NYgWNuE/",
                "model": "tiny",
                "language": "auto",
            },
        )
        assert both.status_code == 422

        share_text = (
            "复制此链接 https://v.douyin.com/FAV7NYgWNuE/，打开Dou音搜索，"
            "直接观看视频！"
        )
        from_text = client.post(
            "/api/tasks",
            json={
                "source_url": share_text,
                "model": "tiny",
                "language": "auto",
            },
        )
        assert from_text.status_code == 201
        assert from_text.json()["source_url"] == "https://v.douyin.com/FAV7NYgWNuE"

        no_link = client.post(
            "/api/tasks",
            json={
                "source_url": "这是一段没有链接的口令文本",
                "model": "tiny",
                "language": "auto",
            },
        )
        assert no_link.status_code == 422


def test_link_task_end_to_end(settings: Settings, monkeypatch) -> None:
    payload = wav_bytes(1.0)

    def fake_resolve(text: str, *, is_cancelled, is_stopping) -> DouyinVideo:
        del is_cancelled, is_stopping
        return DouyinVideo(
            video_id="7664455344306834715",
            title="端到端测试标题",
            duration_ms=1000,
            play_url="https://aweme.snssdk.com/aweme/v1/play/",
        )

    def fake_download(
        play_url, destination, *, size_limit, on_progress, is_cancelled, is_stopping
    ) -> tuple[int, str]:
        del play_url, size_limit, is_cancelled, is_stopping
        destination.write_bytes(payload)
        on_progress(len(payload), len(payload))
        return len(payload), "ab" * 32

    def fake_probe(path, ffprobe_bin, max_duration) -> MediaInfo:
        del ffprobe_bin, max_duration
        return MediaInfo(
            duration_seconds=1,
            format_name="wav",
            audio_codec="pcm_s16le",
            audio_channels=1,
            sample_rate=16_000,
            source_path=path,
        )

    monkeypatch.setattr("app.worker.resolve_douyin_share", fake_resolve)
    monkeypatch.setattr("app.worker.download_video", fake_download)
    monkeypatch.setattr("app.worker.probe_media", fake_probe)

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/tasks",
            json={
                "source_url": "https://v.douyin.com/FAV7NYgWNuE/",
                "model": "tiny",
                "language": "zh",
            },
        ).json()
        assert created["status"] == "queued"
        completed = wait_for_status(client, created["id"], {"succeeded"})
        assert completed["original_name"] == "端到端测试标题"
        assert completed["size_bytes"] == len(payload)
        assert completed["sha256"] == "ab" * 32
        assert completed["source_url"] == "https://v.douyin.com/FAV7NYgWNuE"
        assert completed["language_detected"] == "zh"


def test_link_task_download_failure_can_retry(settings: Settings, monkeypatch) -> None:
    def failing_resolve(text: str, *, is_cancelled, is_stopping) -> DouyinVideo:
        del text, is_cancelled, is_stopping
        raise MediaValidationError("无法解析抖音视频信息")

    monkeypatch.setattr("app.worker.resolve_douyin_share", failing_resolve)

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/tasks",
            json={
                "source_url": "https://v.douyin.com/FAV7NYgWNuE/",
                "model": "tiny",
                "language": "auto",
            },
        ).json()
        failed = wait_for_status(client, created["id"], {"failed"})
        assert failed["error_code"] == "INVALID_MEDIA"
        assert failed["actions"]["retry"] is True

        retried = client.post(f"/api/tasks/{created['id']}/retry")
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"


def test_upload_validation_errors(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        unsupported = client.post(
            "/api/tasks",
            json={
                "file_name": "notes.exe",
                "size_bytes": 10,
                "model": "tiny",
                "language": "auto",
            },
        )
        assert unsupported.status_code == 422

        created = client.post(
            "/api/tasks",
            json={
                "file_name": "broken.wav",
                "size_bytes": 12,
                "model": "tiny",
                "language": "auto",
            },
        ).json()
        invalid = client.put(
            f"/api/tasks/{created['id']}/source",
            content=b"not-a-wave!!",
        )
        assert invalid.status_code == 422
        detail = client.get(f"/api/tasks/{created['id']}").json()
        assert detail["status"] == "failed"
        assert detail["error_code"] == "INVALID_MEDIA"


def test_concurrent_upload_accepts_only_one_request(settings: Settings) -> None:
    payload = wav_bytes()
    with TestClient(create_app(settings)) as client:
        task = client.post(
            "/api/tasks",
            json={
                "file_name": "concurrent.wav",
                "size_bytes": len(payload),
                "content_type": "audio/wav",
                "model": "tiny",
                "language": "auto",
            },
        ).json()

        def upload() -> int:
            return client.put(
                f"/api/tasks/{task['id']}/source",
                content=payload,
                headers={"Content-Type": "audio/wav"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            status_codes = sorted(pool.map(lambda _: upload(), range(2)))

        assert status_codes == [200, 409]
        completed = wait_for_status(client, task["id"], {"succeeded"})
        assert completed["sha256"]


def test_slow_media_probe_does_not_block_health(
    settings: Settings, monkeypatch
) -> None:
    payload = wav_bytes()
    probe_started = threading.Event()

    def slow_probe(path, ffprobe_bin, max_duration):
        del ffprobe_bin, max_duration
        probe_started.set()
        time.sleep(0.5)
        return MediaInfo(
            duration_seconds=1,
            format_name="wav",
            audio_codec="pcm_s16le",
            audio_channels=1,
            sample_rate=16_000,
            source_path=path,
        )

    monkeypatch.setattr("app.main.probe_media", slow_probe)
    with TestClient(create_app(settings)) as client:
        task = client.post(
            "/api/tasks",
            json={
                "file_name": "slow-probe.wav",
                "size_bytes": len(payload),
                "content_type": "audio/wav",
                "model": "tiny",
                "language": "auto",
            },
        ).json()
        with ThreadPoolExecutor(max_workers=1) as pool:
            upload = pool.submit(
                client.put,
                f"/api/tasks/{task['id']}/source",
                content=payload,
                headers={"Content-Type": "audio/wav"},
            )
            assert probe_started.wait(timeout=2)
            started_at = time.monotonic()
            health = client.get("/healthz")
            elapsed = time.monotonic() - started_at
            assert health.status_code == 200
            assert elapsed < 0.25
            assert upload.result(timeout=2).status_code == 200


def test_failed_file_delete_remains_visible_and_retriable(
    settings: Settings, monkeypatch
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        task = create_and_upload(client)
        wait_for_status(client, task["id"], {"succeeded"})
        original_delete = app.state.runtime.storage.delete_task
        calls = 0

        def flaky_delete(task_id: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated cleanup failure")
            original_delete(task_id)

        monkeypatch.setattr(app.state.runtime.storage, "delete_task", flaky_delete)
        failed = client.delete(f"/api/tasks/{task['id']}")
        assert failed.status_code == 500
        retained = client.get(f"/api/tasks/{task['id']}")
        assert retained.status_code == 200
        assert retained.json()["status"] == "delete_failed"
        assert retained.json()["actions"]["delete"] is True

        retried = client.delete(f"/api/tasks/{task['id']}")
        assert retried.status_code == 204
        assert client.get(f"/api/tasks/{task['id']}").status_code == 404
