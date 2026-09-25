import argparse
import os
import threading
import webbrowser
from pathlib import Path

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="reel_text — Instagram → text")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    os.umask(0o077)
    from reel_text.server import create_app

    app = create_app(args.project_dir)
    url = f"http://127.0.0.1:{args.port}"
    print(f"reel_text: {url}\nStop: Ctrl+C", flush=True)
    if not args.no_browser:
        timer = threading.Timer(1.5, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
