from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PENDING = ("queued", "downloading", "transcribing")


class QueueFull(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, language TEXT NOT NULL,
                status TEXT NOT NULL, transcript TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self):
        with self.connection() as db:
            db.execute(
                """UPDATE jobs SET status='interrupted', error=?
                WHERE status IN ('queued','downloading','transcribing')""",
                ("Processing was interrupted by a restart. Submit the link again.",),
            )

    def create(self, urls: list[str], language: str) -> list[dict]:
        jobs = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','downloading','transcribing')"
            ).fetchone()[0]
            if count + len(urls) > 100:
                raise QueueFull("The queue is full. Wait for the current links to finish.")
            for url in urls:
                job_id = uuid4().hex
                db.execute(
                    "INSERT INTO jobs(id,url,language,status,created_at) VALUES(?,?,?,'queued',?)",
                    (job_id, url, language, datetime.now(timezone.utc).isoformat()),
                )
                jobs.append(dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))
        return jobs

    def update(self, job_id: str, status: str, *, transcript: str = "", error: str = ""):
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET status=?,transcript=?,error=? WHERE id=?",
                (status, transcript, error, job_id),
            )

    def get(self, job_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list(self, limit: int = 200) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            ]
