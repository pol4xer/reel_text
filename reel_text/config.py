from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import threading
from pathlib import Path

from dotenv import dotenv_values


class Settings:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.env_path = self.root / ".env"
        self.lock = threading.Lock()

    def values(self) -> dict:
        return {**os.environ, **{k: v for k, v in dotenv_values(self.env_path).items() if v}}

    @property
    def api_key(self) -> str:
        return self.values().get("DEEPGRAM_API_KEY", "").strip()

    @property
    def model(self) -> str:
        return self.values().get("DEEPGRAM_MODEL", "nova-3")

    @property
    def cookies_file(self) -> Path | None:
        value = self.values().get("INSTAGRAM_COOKIES_FILE", "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    def status(self) -> dict:
        return {
            "configured": bool(self.api_key),
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "downloader": importlib.util.find_spec("yt_dlp") is not None,
        }

    def save_api_key(self, value: str) -> None:
        key = value.strip()
        if not key or len(key) > 512 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in key):
            raise ValueError("Enter a valid Deepgram API key without spaces.")
        with self.lock:
            lines = self.env_path.read_text().splitlines() if self.env_path.exists() else []
            lines = [
                line
                for line in lines
                if not line.strip().startswith(("DEEPGRAM_API_KEY=", "export DEEPGRAM_API_KEY="))
            ]
            content = "\n".join([*lines, f"DEEPGRAM_API_KEY={key}", ""])
            fd, temp = tempfile.mkstemp(prefix=".env-", dir=self.root)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(content)
                os.chmod(temp, 0o600)
                os.replace(temp, self.env_path)
            finally:
                Path(temp).unlink(missing_ok=True)
