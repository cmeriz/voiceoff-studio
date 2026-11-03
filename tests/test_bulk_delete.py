import asyncio
import shutil
import time

from app import jobs
from app.separator import MASTER_FILENAME
from tests.conftest import make_audio, run_client
from tests.test_api import upload, wait_for_status
from tests.test_pitch import done_job, wait_for_pitch


async def fake_separate(settings, input_path, work_dir, output_path, output_format, on_progress):
    shutil.copy(input_path, output_path)
    make_audio(output_path.with_name(MASTER_FILENAME))


async def touching_shift(source, destination, semitones):
    destination.touch()


def file_names(settings, job_id):
    return sorted(path.name for path in (settings.jobs_dir / job_id).iterdir())


def test_delete_all_removes_every_song_from_disk(settings, mp3_bytes, monkeypatch):
    monkeypatch.setattr(jobs, "shift_pitch", touching_shift)
    with run_client(settings, fake_separate) as client:
        first = done_job(client, mp3_bytes)["id"]
        second = done_job(client, mp3_bytes)["id"]
        client.post(f"/api/jobs/{first}/pitch", params={"semitones": 2})
        wait_for_pitch(client, first, 2, "done")

        response = client.delete("/api/jobs")

        assert response.status_code == 200
        assert response.json() == {"deleted": 2}
        assert client.get("/api/jobs").json() == []
        assert not (settings.jobs_dir / first).exists()
        assert not (settings.jobs_dir / second).exists()


def test_delete_all_stops_running_and_queued_songs_and_the_worker_survives(settings, mp3_bytes):
    started = []

    async def blocking_separate(settings, input_path, *_):
        started.append(input_path.parent.name)
        await asyncio.sleep(3600)

    with run_client(settings, blocking_separate) as client:
        running = upload(client, mp3_bytes).json()["id"]
        wait_for_status(client, running, "processing")
        queued = upload(client, mp3_bytes).json()["id"]

        assert client.delete("/api/jobs").json() == {"deleted": 2}
        assert list(settings.jobs_dir.iterdir()) == []
        time.sleep(0.1)
        assert started == [running]

        following = upload(client, mp3_bytes).json()["id"]
        wait_for_status(client, following, "processing")
        assert queued not in started


def test_delete_all_with_nothing_to_delete(settings):
    with run_client(settings, fake_separate) as client:
        assert client.delete("/api/jobs").json() == {"deleted": 0}


def test_delete_pitch_versions_keeps_the_songs(settings, mp3_bytes, monkeypatch):
    monkeypatch.setattr(jobs, "shift_pitch", touching_shift)
    with run_client(settings, fake_separate) as client:
        first = done_job(client, mp3_bytes)["id"]
        second = done_job(client, mp3_bytes)["id"]
        for job_id, semitones in ((first, 2), (first, -3), (second, 1)):
            client.post(f"/api/jobs/{job_id}/pitch", params={"semitones": semitones})
            wait_for_pitch(client, job_id, semitones, "done")
        expected_files = ["input.mp3", "job.json", "karaoke.mp3", MASTER_FILENAME]

        response = client.delete("/api/pitch-versions")

        assert response.status_code == 200
        assert response.json() == {"deleted": 3}
        for job_id in (first, second):
            assert file_names(settings, job_id) == expected_files
            job = client.get(f"/api/jobs/{job_id}").json()
            assert job["status"] == "done"
            assert job["pitch_versions"] == {}
        assert client.get(f"/api/jobs/{first}/karaoke").status_code == 200
        assert client.get(f"/api/jobs/{first}/original").status_code == 200
        assert client.get(f"/api/jobs/{first}/karaoke", params={"semitones": 2}).status_code == 409


def test_deleted_versions_can_be_generated_again(settings, mp3_bytes, monkeypatch):
    monkeypatch.setattr(jobs, "shift_pitch", touching_shift)
    with run_client(settings, fake_separate) as client:
        job_id = done_job(client, mp3_bytes)["id"]
        client.post(f"/api/jobs/{job_id}/pitch", params={"semitones": 2})
        wait_for_pitch(client, job_id, 2, "done")
        client.delete("/api/pitch-versions")

        assert client.post(f"/api/jobs/{job_id}/pitch", params={"semitones": 2}).status_code == 202
        wait_for_pitch(client, job_id, 2, "done")
        assert client.get(f"/api/jobs/{job_id}/karaoke", params={"semitones": 2}).status_code == 200


def test_delete_pitch_versions_cancels_generation_in_progress(settings, mp3_bytes, monkeypatch):
    cancelled = []

    async def blocking_shift(source, destination, semitones):
        destination.with_name(f"{destination.stem}.partial{destination.suffix}").touch()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(semitones)
            raise

    monkeypatch.setattr(jobs, "shift_pitch", blocking_shift)
    with run_client(settings, fake_separate) as client:
        job_id = done_job(client, mp3_bytes)["id"]
        client.post(f"/api/jobs/{job_id}/pitch", params={"semitones": 1})
        client.post(f"/api/jobs/{job_id}/pitch", params={"semitones": 2})
        wait_for_pitch(client, job_id, 1, "processing")
        time.sleep(0.1)

        assert client.delete("/api/pitch-versions").json() == {"deleted": 2}

        assert cancelled == [1]
        assert client.get(f"/api/jobs/{job_id}").json()["pitch_versions"] == {}
        assert not [name for name in file_names(settings, job_id) if "partial" in name]


def test_delete_pitch_versions_with_nothing_to_delete(settings, mp3_bytes):
    with run_client(settings, fake_separate) as client:
        done_job(client, mp3_bytes)
        assert client.delete("/api/pitch-versions").json() == {"deleted": 0}


def test_deleted_versions_stay_deleted_after_a_restart(settings, mp3_bytes, monkeypatch):
    monkeypatch.setattr(jobs, "shift_pitch", touching_shift)
    with run_client(settings, fake_separate) as client:
        job_id = done_job(client, mp3_bytes)["id"]
        client.post(f"/api/jobs/{job_id}/pitch", params={"semitones": 2})
        wait_for_pitch(client, job_id, 2, "done")
        client.delete("/api/pitch-versions")

    with run_client(settings, fake_separate) as client:
        assert client.get(f"/api/jobs/{job_id}").json()["pitch_versions"] == {}
