"""Download individual Instagram posts and transcribe their audio with Deepgram."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

MAX_URLS = 50
MAX_AUDIO_BYTES = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT = 600
DEEPGRAM_ENDPOINT = "https://api.deepgram.com/v1/listen"
_POST_PATH = re.compile(r"^/(reel|reels|p|tv)/([A-Za-z0-9_-]+)/?$")
_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class ProcessingError(Exception):
    """An actionable, credential-safe message that can be displayed to the user."""


def normalize_urls(text: str) -> list[str]:
    """Validate post URLs, remove tracking, and deduplicate in insertion order."""
    tokens = text.split()
    if not tokens:
        raise ProcessingError("Add at least one Instagram post URL.")
    if len(tokens) > MAX_URLS:
        raise ProcessingError(f"You can process up to {MAX_URLS} URLs at a time.")
    result: list[str] = []
    for token in tokens:
        try:
            parsed = urlsplit(token)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ProcessingError("Invalid URL. Paste the complete Instagram post URL.") from None
        if (
            parsed.scheme.lower() not in {"https", "http"}
            or host not in _HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
        ):
            raise ProcessingError("Enter a complete URL such as https://www.instagram.com/reel/CODE/.")
        match = _POST_PATH.fullmatch(parsed.path)
        if not match:
            raise ProcessingError(
                "Use an individual post URL containing /reel/, /reels/, /p/, or /tv/. "
                "Profile and collection URLs are not supported."
            )
        kind, code = match.groups()
        kind = "reel" if kind == "reels" else kind
        normalized = f"https://www.instagram.com/{kind}/{code}/"
        if normalized not in result:
            result.append(normalized)
    return result


def _check_audio(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        raise ProcessingError("The audio file is unavailable. Download the post again.") from None
    if not path.is_file() or size == 0:
        raise ProcessingError("The audio file is empty or unavailable.")
    if size > MAX_AUDIO_BYTES:
        raise ProcessingError("The audio file exceeds 200 MB. Choose a shorter post.")


def _ffmpeg_location() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        homebrew_ffmpeg = Path("/opt/homebrew/bin/ffmpeg")
        if homebrew_ffmpeg.is_file():
            ffmpeg = str(homebrew_ffmpeg)
    if not ffmpeg:
        raise ProcessingError("ffmpeg was not found. Install it with brew install ffmpeg.")
    return ffmpeg


def _download_error(stderr: str) -> str:
    """Interpret known failures without reflecting secrets, cookies, or raw output."""
    error = stderr.lower()
    if "no module named yt_dlp" in error:
        return "yt-dlp is not installed. Run uv sync in the project folder."
    if any(marker in error for marker in ("login", "log in", "cookies", "private", "checkpoint")):
        return (
            "Instagram requires login or has restricted access to this post. "
            "Provide your session cookies file and check that you can open the URL in your browser."
        )
    if any(marker in error for marker in ("429", "too many requests", "rate limit")):
        return "Instagram has temporarily limited downloads. Try again later."
    if any(marker in error for marker in ("max-filesize", "larger than max", "file is larger")):
        return "The video exceeds the 200 MB download limit. Choose a shorter post."
    if "ffmpeg" in error:
        return "Could not extract audio. Check that ffmpeg is installed and the video contains audio."
    return (
        "Could not download the Instagram video. Check that the post is available; "
        "if needed, provide cookies or update yt-dlp with uv lock --upgrade-package yt-dlp && uv sync."
    )


def download_audio(
    url: str,
    directory: Path,
    *,
    cookies_file: Path | None = None,
) -> Path:
    """Download one post to a caller-owned temporary directory, then extract MP3."""
    urls = normalize_urls(url)
    if len(urls) != 1:
        raise ProcessingError("Provide exactly one post URL for each download.")
    try:
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ProcessingError("Could not create a temporary folder for the audio.") from None
    output = directory / "audio.mp3"
    if output.exists() or output.is_symlink():
        raise ProcessingError("The temporary folder already contains audio. Use a new empty folder.")
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-progress",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--max-filesize",
        "200M",
        "--ffmpeg-location",
        _ffmpeg_location(),
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "mp3",
        "--output",
        str(directory / "audio.%(ext)s"),
    ]
    if cookies_file is not None:
        cookies_file = Path(cookies_file).expanduser().resolve()
        if not cookies_file.is_file():
            raise ProcessingError("The cookies file was not found. Check the configured path.")
        command.extend(["--cookies", str(cookies_file)])
    command.extend(["--", urls[0]])
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name == "posix"),
        )
    except OSError:
        raise ProcessingError("Could not start the yt-dlp downloader.") from None
    try:
        _, stderr = process.communicate(timeout=DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        # yt-dlp can have an ffmpeg child; killing only yt-dlp leaves it running.
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.communicate()
        raise ProcessingError("The download exceeded 10 minutes and was stopped. Try again later.") from None
    if process.returncode != 0:
        raise ProcessingError(_download_error(stderr or ""))
    if output.is_symlink() or output.resolve().parent != directory:
        raise ProcessingError("The downloader returned an invalid audio file path.")
    _check_audio(output)
    return output


def _transcript(payload: object) -> str:
    try:
        alternative = payload["results"]["channels"][0]["alternatives"][0]  # type: ignore[index]
        paragraphs = alternative.get("paragraphs") or {}
        text = paragraphs.get("transcript") or alternative.get("transcript")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProcessingError("Deepgram returned an unexpected response format. Try again later.") from None
    if not isinstance(text, str) or not text.strip():
        raise ProcessingError(
            "No speech was recognized. The video may contain only music or no audible speech."
        )
    return text.strip()


def transcribe_audio(
    path: Path,
    *,
    api_key: str,
    language: str = "auto",
    model: str = "nova-3",
) -> str:
    """Stream audio to Deepgram; retry only explicit transient HTTP responses."""
    api_key = api_key.strip()
    if not api_key or "\n" in api_key or "\r" in api_key:
        raise ProcessingError("Set a valid DEEPGRAM_API_KEY in the .env file.")
    path = Path(path)
    _check_audio(path)
    params = {"model": model, "smart_format": "true", "paragraphs": "true"}
    params.update({"detect_language": "true"} if language == "auto" else {"language": language})
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "audio/mpeg"}
    for attempt in range(3):
        try:
            # Reopen for each retry: requests consumes the stream during sending.
            with path.open("rb") as audio:
                response = requests.post(
                    DEEPGRAM_ENDPOINT,
                    params=params,
                    headers=headers,
                    data=audio,
                    timeout=(15, 180),
                    allow_redirects=False,
                )
        except requests.ReadTimeout:
            raise ProcessingError(
                "Deepgram did not respond in time. The request may have been processed: "
                "it will not be retried automatically to avoid duplicate charges."
            ) from None
        except requests.RequestException:
            raise ProcessingError(
                "Could not connect to Deepgram. Check your connection and retry manually."
            ) from None
        except OSError:
            raise ProcessingError("Could not read the audio file.") from None
        try:
            status = response.status_code
            if status in _RETRYABLE_STATUSES and attempt < 2:
                retry = True
            else:
                retry = False
                if status in {401, 403}:
                    raise ProcessingError("Deepgram rejected the API key. Check the key and its permissions.")
                if status == 402:
                    raise ProcessingError(
                        "Deepgram: insufficient funds or the account quota has been reached."
                    )
                if status == 429:
                    raise ProcessingError("Deepgram has limited the request rate. Try again later.")
                if status == 413:
                    raise ProcessingError("Deepgram rejected the audio file because it is too large.")
                if status == 400:
                    raise ProcessingError("Deepgram rejected the audio or the language and model settings.")
                if not 200 <= status < 300:
                    raise ProcessingError("Deepgram returned an error. Try again later.")
                try:
                    payload = response.json()
                except ValueError:
                    raise ProcessingError("Deepgram returned an invalid response. Try again later.") from None
                return _transcript(payload)
        finally:
            response.close()
        if retry:
            time.sleep(2**attempt)
    raise ProcessingError("Deepgram is temporarily unavailable. Try again later.")
