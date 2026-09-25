import signal
import subprocess
from unittest.mock import Mock

import pytest
import requests

from reel_text import pipeline


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "input.mp3"
    path.write_bytes(b"example audio")
    return path


def response(status=200, payload=None):
    item = Mock(status_code=status)
    item.json.return_value = (
        payload
        if payload is not None
        else {"results": {"channels": [{"alternatives": [{"transcript": "Hello, world!"}]}]}}
    )
    return item


def test_normalize_deduplicates_and_removes_tracking():
    assert pipeline.normalize_urls(
        "https://instagram.com/reels/ABC_123/?igsh=secret#fragment\n"
        "https://www.instagram.com/reel/ABC_123/\n"
        "http://m.instagram.com/p/DEF-321"
    ) == ["https://www.instagram.com/reel/ABC_123/", "https://www.instagram.com/p/DEF-321/"]


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com.evil.example/reel/abc/",
        "https://instagram.com@evil.example/reel/abc/",
        "https://user:secret@instagram.com/reel/abc/",
        "https://www.instagram.com:443/reel/abc/",
        "file:///etc/passwd",
        "https://[malformed/reel/abc/",
        "https://www.instagram.com/reel/abc/../../profile/",
        "https://www.instagram.com/reel/%2e%2e/",
        "https://www.instagram.com/some_profile/",
        "https://www.instagram.com/some_profile/reels/",
        "https://www.instagram.com/stories/abc/",
        "instagram.com/reel/abc/",
        "",
    ],
)
def test_rejects_non_post_and_unsafe_urls(url):
    with pytest.raises(pipeline.ProcessingError):
        pipeline.normalize_urls(url)


def test_url_limit():
    with pytest.raises(pipeline.ProcessingError, match="50"):
        pipeline.normalize_urls("\n".join(f"https://instagram.com/p/{i}/" for i in range(51)))


def downloader(monkeypatch, *, resultcode=0, stderr="", communicate=None):
    process = Mock(pid=123456, returncode=resultcode)
    process.communicate = communicate or Mock(return_value=("", stderr))
    popen = Mock(return_value=process)
    monkeypatch.setattr(pipeline.subprocess, "Popen", popen)
    monkeypatch.setattr(pipeline, "_ffmpeg_location", lambda: "/usr/bin/ffmpeg")
    return process, popen


def test_download_command_and_bounded_output(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("cookies")

    def completed(**kwargs):
        (scratch / "audio.mp3").write_bytes(b"audio")
        return ("untrusted output path\n", "")

    process, popen = downloader(monkeypatch, communicate=Mock(side_effect=completed))
    result = pipeline.download_audio(
        "https://instagram.com/reel/ABC/?igsh=tracking", scratch, cookies_file=cookies
    )
    assert result == scratch / "audio.mp3"
    command = popen.call_args.args[0]
    assert command[:3] == [pipeline.sys.executable, "-m", "yt_dlp"]
    assert command[-2:] == ["--", "https://www.instagram.com/reel/ABC/"]
    assert "--ignore-config" in command and "--no-playlist" in command
    assert command[command.index("--cookies") + 1] == str(cookies)
    assert popen.call_args.kwargs["start_new_session"] is True
    process.communicate.assert_called_once_with(timeout=600)


def test_download_failure_does_not_leak_output(tmp_path, monkeypatch):
    downloader(monkeypatch, resultcode=1, stderr="secret_cookie_token private video")
    with pytest.raises(pipeline.ProcessingError, match="requires login") as exc:
        pipeline.download_audio("https://instagram.com/reel/ABC/", tmp_path)
    assert "secret_cookie_token" not in str(exc.value)


def test_download_timeout_kills_process_group(tmp_path, monkeypatch):
    process, _ = downloader(
        monkeypatch,
        communicate=Mock(side_effect=[subprocess.TimeoutExpired("yt_dlp", 600), ("", "")]),
    )
    kill = Mock()
    monkeypatch.setattr(pipeline.os, "killpg", kill)
    with pytest.raises(pipeline.ProcessingError, match="10 minutes"):
        pipeline.download_audio("https://instagram.com/reel/ABC/", tmp_path)
    kill.assert_called_once_with(process.pid, signal.SIGKILL)
    assert process.communicate.call_count == 2


def test_download_refuses_output_symlink(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")

    def completed(**kwargs):
        (scratch / "audio.mp3").symlink_to(outside)
        return ("", "")

    downloader(monkeypatch, communicate=Mock(side_effect=completed))
    with pytest.raises(pipeline.ProcessingError, match="invalid audio file path"):
        pipeline.download_audio("https://instagram.com/reel/ABC/", scratch)


def test_download_missing_output_is_failure(tmp_path, monkeypatch):
    downloader(monkeypatch)
    with pytest.raises(pipeline.ProcessingError, match="unavailable"):
        pipeline.download_audio("https://instagram.com/reel/ABC/", tmp_path)


def test_download_missing_cookies_does_not_launch(tmp_path, monkeypatch):
    _, popen = downloader(monkeypatch)
    with pytest.raises(pipeline.ProcessingError, match="cookies file was not found"):
        pipeline.download_audio(
            "https://instagram.com/reel/ABC/", tmp_path, cookies_file=tmp_path / "missing"
        )
    popen.assert_not_called()


def test_deepgram_streams_audio_and_prefers_paragraphs(audio, monkeypatch):
    sent = []
    reply = response(
        payload={
            "results": {
                "channels": [
                    {
                        "alternatives": [
                            {
                                "transcript": "flat text",
                                "paragraphs": {"transcript": " First paragraph.\n\nSecond paragraph. "},
                            }
                        ]
                    }
                ]
            }
        }
    )

    def post(url, **kwargs):
        assert url == "https://api.deepgram.com/v1/listen"
        assert kwargs["data"].read() == b"example audio"
        sent.append(kwargs)
        return reply

    monkeypatch.setattr(pipeline.requests, "post", post)
    assert pipeline.transcribe_audio(audio, api_key="secret") == "First paragraph.\n\nSecond paragraph."
    assert sent[0]["headers"] == {"Authorization": "Token secret", "Content-Type": "audio/mpeg"}
    assert sent[0]["params"] == {
        "model": "nova-3",
        "smart_format": "true",
        "paragraphs": "true",
        "detect_language": "true",
    }
    assert sent[0]["data"].closed
    assert sent[0]["allow_redirects"] is False
    reply.close.assert_called_once()


def test_language_option_and_plain_transcript(audio, monkeypatch):
    post = Mock(return_value=response())
    monkeypatch.setattr(pipeline.requests, "post", post)
    assert pipeline.transcribe_audio(audio, api_key="secret", language="ru") == "Hello, world!"
    assert post.call_args.kwargs["params"]["language"] == "ru"
    assert "detect_language" not in post.call_args.kwargs["params"]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_retry_reopens_stream(audio, monkeypatch, status):
    bodies = []
    responses = [response(status), response()]

    def post(url, **kwargs):
        bodies.append(kwargs["data"].read())
        return responses[len(bodies) - 1]

    monkeypatch.setattr(pipeline.requests, "post", post)
    sleep = Mock()
    monkeypatch.setattr(pipeline.time, "sleep", sleep)
    assert pipeline.transcribe_audio(audio, api_key="secret") == "Hello, world!"
    assert bodies == [b"example audio", b"example audio"]
    sleep.assert_called_once_with(1)
    for item in responses:
        item.close.assert_called_once()


def test_retry_limit(audio, monkeypatch):
    post = Mock(side_effect=[response(503), response(503), response(503)])
    monkeypatch.setattr(pipeline.requests, "post", post)
    monkeypatch.setattr(pipeline.time, "sleep", Mock())
    with pytest.raises(pipeline.ProcessingError):
        pipeline.transcribe_audio(audio, api_key="secret")
    assert post.call_count == 3


@pytest.mark.parametrize("status", [301, 400, 401, 402, 403, 413, 422])
def test_permanent_errors_are_not_retried_or_reflected(audio, monkeypatch, status):
    reply = response(status, {"message": "secret credential data"})
    post = Mock(return_value=reply)
    monkeypatch.setattr(pipeline.requests, "post", post)
    with pytest.raises(pipeline.ProcessingError) as exc:
        pipeline.transcribe_audio(audio, api_key="secret")
    assert "secret" not in str(exc.value)
    post.assert_called_once()
    reply.close.assert_called_once()


def test_read_timeout_is_not_retried(audio, monkeypatch):
    post = Mock(side_effect=requests.ReadTimeout("secret_token"))
    monkeypatch.setattr(pipeline.requests, "post", post)
    with pytest.raises(pipeline.ProcessingError, match="duplicate charges") as exc:
        pipeline.transcribe_audio(audio, api_key="secret")
    assert "secret" not in str(exc.value)
    post.assert_called_once()


def test_empty_transcript_is_not_success(audio, monkeypatch):
    reply = response(payload={"results": {"channels": [{"alternatives": [{"transcript": " "}]}]}})
    monkeypatch.setattr(pipeline.requests, "post", Mock(return_value=reply))
    with pytest.raises(pipeline.ProcessingError, match="No speech was recognized"):
        pipeline.transcribe_audio(audio, api_key="secret")


@pytest.mark.parametrize("payload", [{}, {"results": {"channels": []}}, [], None])
def test_malformed_deepgram_payload(audio, monkeypatch, payload):
    reply = response()
    reply.json.return_value = payload
    monkeypatch.setattr(pipeline.requests, "post", Mock(return_value=reply))
    with pytest.raises(pipeline.ProcessingError, match="response format"):
        pipeline.transcribe_audio(audio, api_key="secret")


def test_size_guard_does_not_send_audio(audio, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_AUDIO_BYTES", 1)
    post = Mock()
    monkeypatch.setattr(pipeline.requests, "post", post)
    with pytest.raises(pipeline.ProcessingError, match="200 MB"):
        pipeline.transcribe_audio(audio, api_key="secret")
    post.assert_not_called()
