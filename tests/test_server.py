import time

import pytest
from fastapi.testclient import TestClient

from reel_text import server
from reel_text.config import Settings
from reel_text.pipeline import ProcessingError
from reel_text.store import Store


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setattr(
        Settings,
        "status",
        lambda self: {
            "configured": bool(self.api_key),
            "ffmpeg": True,
            "downloader": True,
        },
    )
    return server.create_app(tmp_path)


def complete(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = next(j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id)
        if job["status"] not in {"queued", "downloading", "transcribing"}:
            return job
        time.sleep(0.01)
    pytest.fail("Job did not complete")


def test_key_is_private_and_required(app):
    with TestClient(app) as client:
        assert client.post("/api/jobs", json={"urls": "https://instagram.com/reel/abc/"}).status_code == 400
        secret = "test-private-key-123"
        response = client.post("/api/settings", json={"api_key": secret})
        assert response.status_code == 200
        assert response.json()["configured"]
        assert secret not in response.text
        assert secret not in client.get("/api/status").text
        assert app.state.settings.env_path.stat().st_mode & 0o777 == 0o600
        assert secret not in client.post("/api/settings", json={"api_key": secret * 100}).text


def test_batch_success_error_export_and_restart(app, monkeypatch, tmp_path):
    directories = []

    def download(url, directory, **kwargs):
        directories.append(directory)
        if "bad" in url:
            raise ProcessingError("Instagram did not return a video.")
        path = directory / "audio.mp3"
        path.write_bytes(b"synthetic audio")
        return path

    monkeypatch.setattr(server, "download_audio", download)
    monkeypatch.setattr(server, "transcribe_audio", lambda *a, **k: "Hello! This is a transcript.")
    with TestClient(app) as client:
        client.post("/api/settings", json={"api_key": "test-key"})
        response = client.post(
            "/api/jobs",
            json={
                "urls": "https://instagram.com/reel/good/\nhttps://instagram.com/reel/bad/",
                "language": "ru",
            },
        )
        assert response.status_code == 202
        first, second = response.json()["jobs"]
        done = complete(client, first["id"])
        failed = complete(client, second["id"])
        assert done["status"] == "done"
        assert failed["status"] == "error"
        assert failed["error"] == "Instagram did not return a video."
        assert client.get(f"/api/jobs/{first['id']}/text").text == done["transcript"]
        assert client.get(f"/api/jobs/{second['id']}/text").status_code == 409
        export = client.get("/api/export")
        assert "good" in export.text and "bad" not in export.text
    assert all(not directory.exists() for directory in directories)
    with TestClient(server.create_app(tmp_path)) as restarted:
        assert restarted.get("/api/jobs").json()["jobs"][1]["transcript"] == done["transcript"]


def test_interrupted_job_marked_on_restart(tmp_path):
    store = Store(tmp_path / "data" / "reel_text.sqlite3")
    job = store.create(["https://www.instagram.com/reel/abc/"], "auto")[0]
    store.update(job["id"], "transcribing")
    with TestClient(server.create_app(tmp_path)) as client:
        assert client.get("/api/jobs").json()["jobs"][0]["status"] == "interrupted"


def test_cross_origin_writes_and_rebinding_rejected(app):
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/settings", json={"api_key": "test"}, headers={"Origin": "https://unrelated.example"}
            ).status_code
            == 403
        )
        assert client.get("/api/status", headers={"Host": "unrelated.example"}).status_code == 400
        assert (
            client.post(
                "/api/settings", json={"api_key": "test"}, headers={"Origin": "http://testserver"}
            ).status_code
            == 200
        )


def test_unknown_error_does_not_expose_key(app, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("private-test-key")

    monkeypatch.setattr(server, "download_audio", failed)
    with TestClient(app) as client:
        client.post("/api/settings", json={"api_key": "private-test-key"})
        response = client.post("/api/jobs", json={"urls": "https://instagram.com/reel/abc/"})
        job = complete(client, response.json()["jobs"][0]["id"])
        assert job["status"] == "error"
        assert "private-test-key" not in job["error"]


def test_static_page_and_missing_export(app):
    with TestClient(app) as client:
        assert "Turn videos into words" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/export").status_code == 404


def test_delete_completed_job_persists_and_removes_export(app, tmp_path):
    with TestClient(app) as client:
        store = app.state.store
        deleted, retained = store.create(
            ["https://instagram.com/reel/deleted/", "https://instagram.com/reel/retained/"], "auto"
        )
        store.update(deleted["id"], "done", transcript="Transcript to delete.")
        store.update(retained["id"], "done", transcript="Transcript to retain.")
        response = client.delete(f"/api/jobs/{deleted['id']}")
        assert response.status_code == 200
        assert response.json() == {"deleted": 1}
        assert client.get(f"/api/jobs/{deleted['id']}/text").status_code == 404
        assert [job["id"] for job in client.get("/api/jobs").json()["jobs"]] == [retained["id"]]
        exported = client.get("/api/export").text
        assert "Transcript to delete." not in exported
        assert "Transcript to retain." in exported
    with TestClient(server.create_app(tmp_path)) as restarted:
        assert restarted.get(f"/api/jobs/{deleted['id']}/text").status_code == 404
        assert restarted.get("/api/jobs").json()["jobs"][0]["id"] == retained["id"]


@pytest.mark.parametrize("status", ["error", "interrupted"])
def test_delete_failed_or_interrupted_job(app, status):
    with TestClient(app) as client:
        store = app.state.store
        job = store.create(["https://instagram.com/reel/failed/"], "auto")[0]
        store.update(job["id"], status, error="Processing did not complete.")
        response = client.delete(f"/api/jobs/{job['id']}")
        assert response.status_code == 200
        assert response.json() == {"deleted": 1}
        assert store.get(job["id"]) is None


def test_delete_missing_job_returns_not_found(app):
    with TestClient(app) as client:
        assert client.delete("/api/jobs/does-not-exist").status_code == 404


@pytest.mark.parametrize("status", ["queued", "downloading", "transcribing"])
def test_delete_pending_job_returns_conflict_and_preserves_item(app, status):
    with TestClient(app) as client:
        store = app.state.store
        job = store.create(["https://instagram.com/reel/pending/"], "auto")[0]
        store.update(job["id"], status)
        response = client.delete(f"/api/jobs/{job['id']}")
        assert response.status_code == 409
        assert store.get(job["id"])["status"] == status


def test_delete_all_transcripts_includes_hidden_rows_and_preserves_other_statuses(app):
    with TestClient(app) as client:
        store = app.state.store
        completed_ids = []
        for index in range(205):
            job = store.create([f"https://instagram.com/reel/completed{index}/"], "auto")[0]
            store.update(job["id"], "done", transcript=f"Transcript {index}.")
            completed_ids.append(job["id"])
        preserved = {}
        for status in ("queued", "downloading", "transcribing", "error", "interrupted"):
            job = store.create([f"https://instagram.com/reel/{status}/"], "auto")[0]
            store.update(job["id"], status)
            preserved[job["id"]] = status
        listing = client.get("/api/jobs").json()
        assert len(listing["jobs"]) == 200
        assert listing["completed_count"] == 205
        response = client.delete("/api/transcripts")
        assert response.status_code == 200
        assert response.json() == {"deleted": 205}
        assert all(store.get(job_id) is None for job_id in completed_ids)
        listing = client.get("/api/jobs").json()
        assert {job["id"]: job["status"] for job in listing["jobs"]} == preserved
        assert listing["completed_count"] == 0
        assert client.get("/api/export").status_code == 404
        assert client.delete("/api/transcripts").json() == {"deleted": 0}


@pytest.mark.parametrize("endpoint", ["/api/jobs/{job_id}", "/api/transcripts"])
def test_cross_origin_delete_is_rejected(app, endpoint):
    with TestClient(app) as client:
        store = app.state.store
        job = store.create(["https://instagram.com/reel/completed/"], "auto")[0]
        store.update(job["id"], "done", transcript="Keep this transcript.")
        response = client.delete(
            endpoint.format(job_id=job["id"]), headers={"Origin": "https://unrelated.example"}
        )
        assert response.status_code == 403
        assert store.get(job["id"])["transcript"] == "Keep this transcript."
