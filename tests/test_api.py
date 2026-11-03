import asyncio
import shutil
import time

import pytest

from app.config import Settings
from app.jobs import INTERRUPTED_MESSAGE
from app.main import clean_filename
from app.separator import SeparationError
from tests.conftest import run_client


async def fake_separate(settings, input_path, work_dir, output_path, output_format, on_progress):
    on_progress(50)
    shutil.copy(input_path, output_path)


async def failing_separate(*_):
    raise SeparationError("No se pudo separar la voz de este audio.")


async def crashing_separate(*_):
    raise RuntimeError("secret internal detail")


async def blocking_separate(*_):
    await asyncio.sleep(3600)


def upload(client, content, filename="canción.mp3"):
    return client.post("/api/jobs", params={"filename": filename}, content=content)


def wait_for_status(client, job_id, *statuses):
    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"Job stayed in {job['status']}")


def test_mp3_becomes_mp3_karaoke_with_a_download_name(settings, mp3_bytes):
    with run_client(settings, fake_separate) as client:
        created = upload(client, mp3_bytes)
        assert created.status_code == 201
        job = wait_for_status(client, created.json()["id"], "done")
        assert job["format"] == "mp3"
        assert job["progress"] == 100
        assert job["expires_in_seconds"] > 0

        inline = client.get(f"/api/jobs/{job['id']}/karaoke")
        assert inline.headers["content-type"] == "audio/mpeg"
        assert inline.headers["content-disposition"].startswith("inline")
        attachment = client.get(f"/api/jobs/{job['id']}/karaoke", params={"download": True})
        assert attachment.headers["content-disposition"].startswith("attachment")
        assert "canci%C3%B3n%20%28karaoke%29.mp3" in attachment.headers["content-disposition"]


def test_wav_keeps_its_format(settings, wav_bytes):
    with run_client(settings, fake_separate) as client:
        job = wait_for_status(client, upload(client, wav_bytes, "song.wav").json()["id"], "done")
        assert job["format"] == "wav"
        assert client.get(f"/api/jobs/{job['id']}/karaoke").headers["content-type"] == "audio/wav"


def test_audio_supports_range_requests_for_seeking(settings, mp3_bytes):
    with run_client(settings, fake_separate) as client:
        job = wait_for_status(client, upload(client, mp3_bytes).json()["id"], "done")
        response = client.get(f"/api/jobs/{job['id']}/original", headers={"Range": "bytes=0-99"})
        assert response.status_code == 206
        assert len(response.content) == 100


def test_format_is_detected_from_content_not_extension(settings, wav_bytes):
    with run_client(settings, fake_separate) as client:
        assert upload(client, wav_bytes, "mislabeled.mp3").json()["format"] == "wav"


@pytest.mark.parametrize(
    ("content", "filename", "status"),
    [
        (b"this is not audio at all" * 100, "fake.mp3", 415),
        (b"<html></html>", "fake.wav", 415),
        (b"", "empty.mp3", 400),
        (b"whatever", "song.ogg", 415),
        (b"whatever", "song", 415),
    ],
)
def test_invalid_uploads_are_rejected(settings, content, filename, status):
    with run_client(settings, fake_separate) as client:
        assert upload(client, content, filename).status_code == status
        assert client.get("/api/jobs").json() == []
    assert list(settings.uploads_dir.iterdir()) == []


def test_oversized_upload_is_rejected_by_declared_size(settings):
    with run_client(settings, fake_separate) as client:
        assert upload(client, b"0" * (settings.max_upload_bytes + 1)).status_code == 413


def test_oversized_upload_is_rejected_while_streaming(settings):
    def chunks():
        for _ in range(3):
            yield b"0" * (512 * 1024)

    with run_client(settings, fake_separate) as client:
        response = client.post("/api/jobs", params={"filename": "big.mp3"}, content=chunks())
        assert response.status_code == 413
    assert list(settings.uploads_dir.iterdir()) == []


def test_too_long_audio_is_rejected(settings, tmp_path):
    from tests.conftest import make_audio

    audio = make_audio(tmp_path / "long.mp3", seconds=6).read_bytes()
    with run_client(settings, fake_separate) as client:
        response = upload(client, audio)
        assert response.status_code == 413
        assert "minutos" in response.json()["detail"]


def test_separation_failure_is_reported(settings, mp3_bytes):
    with run_client(settings, failing_separate) as client:
        job = wait_for_status(client, upload(client, mp3_bytes).json()["id"], "failed")
        assert job["error"] == "No se pudo separar la voz de este audio."
        assert client.get(f"/api/jobs/{job['id']}/karaoke").status_code == 409


def test_unexpected_errors_do_not_leak_details_and_worker_survives(settings, mp3_bytes):
    with run_client(settings, crashing_separate) as client:
        first = wait_for_status(client, upload(client, mp3_bytes).json()["id"], "failed")
        assert "secret" not in first["error"]
        wait_for_status(client, upload(client, mp3_bytes).json()["id"], "failed")


def test_jobs_run_one_at_a_time_and_queue_has_a_limit(settings, mp3_bytes):
    with run_client(settings, blocking_separate) as client:
        first = upload(client, mp3_bytes).json()["id"]
        wait_for_status(client, first, "processing")
        second = upload(client, mp3_bytes).json()["id"]
        assert client.get(f"/api/jobs/{second}").json()["status"] == "queued"
        assert upload(client, mp3_bytes).status_code == 503


def test_cancelling_the_running_job_starts_the_next_one(settings, mp3_bytes):
    with run_client(settings, blocking_separate) as client:
        first = upload(client, mp3_bytes).json()["id"]
        wait_for_status(client, first, "processing")
        second = upload(client, mp3_bytes).json()["id"]

        assert client.delete(f"/api/jobs/{first}").status_code == 204
        assert client.get(f"/api/jobs/{first}").status_code == 404
        assert not (settings.jobs_dir / first).exists()
        wait_for_status(client, second, "processing")


def test_deleting_a_queued_job_skips_it(settings, mp3_bytes):
    with run_client(settings, blocking_separate) as client:
        first = upload(client, mp3_bytes).json()["id"]
        wait_for_status(client, first, "processing")
        second = upload(client, mp3_bytes).json()["id"]
        assert client.delete(f"/api/jobs/{second}").status_code == 204
        assert client.delete(f"/api/jobs/{second}").status_code == 404
        assert [job["id"] for job in client.get("/api/jobs").json()] == [first]


def test_restart_keeps_finished_jobs_and_fails_interrupted_ones(settings, mp3_bytes):
    with run_client(settings, fake_separate) as client:
        finished = wait_for_status(client, upload(client, mp3_bytes).json()["id"], "done")["id"]
    with run_client(settings, blocking_separate) as client:
        interrupted = upload(client, mp3_bytes).json()["id"]
        wait_for_status(client, interrupted, "processing")

    with run_client(settings, fake_separate) as client:
        assert client.get(f"/api/jobs/{finished}").json()["status"] == "done"
        job = client.get(f"/api/jobs/{interrupted}").json()
        assert job["status"] == "failed"
        assert job["error"] == INTERRUPTED_MESSAGE


def test_expired_jobs_are_removed(tmp_path, mp3_bytes):
    settings = Settings(data_dir=tmp_path / "data", retention_hours=0, cleanup_interval_seconds=0)
    with run_client(settings, fake_separate) as client:
        job_id = upload(client, mp3_bytes).json()["id"]
        deadline = time.time() + 5
        while client.get(f"/api/jobs/{job_id}").status_code != 404 or (settings.jobs_dir / job_id).exists():
            assert time.time() < deadline
            time.sleep(0.02)


def test_job_ids_cannot_traverse_paths(settings):
    with run_client(settings, fake_separate) as client:
        assert client.get("/api/jobs/..%2F..%2Fetc%2Fpasswd/original").status_code == 404
        assert client.delete("/api/jobs/....").status_code == 404


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("song.mp3", "song.mp3"),
        ("../../etc/passwd.mp3", "passwd.mp3"),
        ("C:\\Users\\me\\song.wav", "song.wav"),
        ("bad\r\nname.mp3", "badname.mp3"),
    ],
)
def test_clean_filename(raw, expected):
    assert clean_filename(raw) == expected


def test_frontend_is_served(settings):
    with run_client(settings, fake_separate) as client:
        assert "VoiceOff Studio" in client.get("/").text
        assert client.get("/brand/logo-dark.svg").status_code == 200
