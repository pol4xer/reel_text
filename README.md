# reel_text

A local application that turns Instagram videos into text using Deepgram.

Paste Reel or video-post links, choose a language, and copy or download the transcripts. Jobs run one at a time, and your history stays on your computer.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and [ffmpeg](https://ffmpeg.org/).

On macOS:

```sh
brew install uv ffmpeg
git clone https://github.com/pol4xer/reel_text.git
cd reel_text
uv sync --frozen
uv run --frozen reel-text
```

The application opens at **http://127.0.0.1:8765**. You can also double-click **`start.command`** in Finder or run `./start.command` from the terminal.

Keep the terminal window open while using the app. Press `Ctrl+C` to stop it; an active request may continue until it finishes or reaches its timeout.

If the port is already in use:

```sh
./start.command --port 8766
```

## Use

1. Open **Deepgram settings**, enter your own API key, and click **Save key**.
2. Paste links to individual Instagram Reels or video posts, one per line. Submit up to 50 links at a time; duplicates within the submission are removed.
3. Choose **Video language** or leave **Detect automatically** selected, then click **Get transcripts**.
4. Copy individual results or download them as `.txt`. **Download all .txt** includes successful transcripts among the latest 200 items.
5. Click **Delete** on a completed, failed, or interrupted item to remove it from your local history. **Clean All Transcripts** removes all completed transcripts, including older ones outside the current list, while keeping queued, processing, failed, and interrupted items. Download anything you want to keep before deleting it.

Profile and collection URLs are not supported. If a video is private, deleted, inaccessible, or has no recognizable speech, that item shows an error and the remaining links continue processing.

The interface is in English. Transcripts preserve the spoken language; the application does not translate speech into English.

## Configuration and local data

The repository contains **no API keys, cookies, transcripts, or local database**. Configure your own credentials after cloning.

- `.env`: API key, model, and optional cookies-file path. Ignored by Git. A key saved through the interface is written with owner-only file permissions.
- `data/reel_text.sqlite3`: local history and transcripts. To back up your history, stop the app and copy the `data/` directory.
- Audio downloads: stored in temporary directories and removed after processing.

The server listens only on `127.0.0.1`. Audio is sent to Deepgram for transcription, and your Deepgram account's usage charges apply.

See `.env.example` for the supported settings. You can also provide `DEEPGRAM_API_KEY` through the environment; a nonempty value in `.env` takes precedence. The default model is `nova-3`.

If Instagram requires authentication, provide the path to a Netscape-format cookies file for **your own** Instagram session:

```dotenv
INSTAGRAM_COOKIES_FILE=/absolute/path/to/cookies.txt
```

The app does not read browser cookies automatically. Keep cookie files and credentials outside Git. Instagram availability also depends on the source and yt-dlp's current extractor support.

To update the downloader after changes to Instagram:

```sh
uv lock --upgrade-package yt-dlp
uv sync --frozen
```

## How it works

`yt-dlp` downloads the available audio track → `ffmpeg` extracts MP3 → the audio is sent to [Deepgram /v1/listen](https://developers.deepgram.com/docs/pre-recorded-audio) → the transcript is stored locally.

| File | Purpose |
| --- | --- |
| `reel_text/pipeline.py` | Link validation, audio downloads, and transcription |
| `reel_text/server.py` | Local HTTP API and sequential processing queue |
| `reel_text/store.py` | SQLite history; interrupted jobs are marked and are not automatically resubmitted |
| `reel_text/static/` | Browser interface without external CDNs or JavaScript dependencies |
| `tests/` | Processing, API, error handling, persistence, and credential-isolation checks |

The queue holds up to 100 links. Each download has a 10-minute timeout and an audio-file limit of 200 MB. Deepgram HTTP 429/500/502/503/504 responses are retried up to three attempts. Ambiguous network timeouts are not automatically retried to avoid unintended duplicate charges.

## Development

```sh
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
```

Tests use synthetic data and mocked Instagram/Deepgram requests. They do not download real videos or use your Deepgram balance. To check the external services, submit a video link through the interface.
