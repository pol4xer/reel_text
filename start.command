#!/bin/zsh
set -eu
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Install uv: brew install uv"
  read -r "?Press Enter to exit..."
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Install ffmpeg: brew install ffmpeg"
  read -r "?Press Enter to exit..."
  exit 1
fi
exec uv run --frozen reel-text "$@"
