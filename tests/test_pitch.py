import asyncio
import shutil
import time

import numpy as np
import pytest
import soundfile as sf

from app import jobs
from app.audio import EncodingError, probe, shift_pitch
from app.config import Settings
from app.separator import MASTER_FILENAME
from tests.conftest import make_audio, run_client
from tests.test_api import upload, wait_for_status


async def fake_separate(settings, input_path, work_dir, output_path, output_format, on_progress):
    shutil.copy(input_path, output_path)


async def fake_separate_with_master(settings, input_path, work_dir, output_path, output_format, on_progress):
    shutil.copy(input_path, output_path)
    make_audio(output_path.with_name(MASTER_FILENAME))


def done_job(client, content, filename="song.mp3"):
    return wait_for_status(client, upload(client, content, filename).json()["id"], "done")


def wait_for_pitch(client, job_id, semitones, *statuses):
    deadline = time.time() + 10
    while time.time() < deadline:
        status = client.get(f"/api/jobs/{job_id}").json()["pitch_versions"].get(str(semitones))
        if status in statuses:
            return status
        time.sleep(0.02)
    raise AssertionError(f"Pitch version stayed in {status}")


def dominant_frequency(path):
    audio, rate = sf.read(path, always_2d=True)
    mono = audio.mean(axis=1)[rate // 4 : -rate // 4]
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    return np.argmax(spectrum) * rate / len(mono), len(audio) / rate


@pytest.mark.parametrize("semitones", [-6, -3, 1, 2, 6])
def test_shift_changes_pitch_and_keeps_duration(tmp_path, semitones):
    source = make_audio(tmp_path / "input.wav", seconds=3)
    output = tmp_path / "output.wav"

    asyncio.run(shift_pitch(source, output, semitones))

    frequency, duration = dominant_frequency(output)
    assert frequency == pytest.approx(440 * 2 ** (semitones / 12), rel=0.01)
    assert duration == pytest.approx(3, abs=0.05)
    assert list(tmp_path.glob("*.partial.*")) == []


def test_shift_writes_mp3_from_a_wav_source(tmp_path):
    output = tmp_path / "output.mp3"
    asyncio.run(shift_pitch(make_audio(tmp_path / "input.wav"), output, 2))
    assert asyncio.run(probe(output)).format == "mp3"


def test_failed_shift_leaves_no_partial_file(tmp_path):
    bad_source = tmp_path / "input.wav"
    bad_source.write_bytes(b"not audio")
    with pytest.raises(EncodingError):
        asyncio.run(shift_pitch(bad_source, tmp_path / "output.wav", 2))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.wav"]


def test_pitch_version_is_generated_cached_and_downloadable(settings, mp3_bytes):
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes, "canción.mp3")

        first = client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 2})
        assert first.status_code == 202
        assert first.json() == {"semitones": 2, "status": "processing"}
        assert wait_for_pitch(client, job["id"], 2, "done", "failed") == "done"

        again = client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 2})
        assert again.status_code == 200
        assert again.json()["status"] == "done"

        response = client.get(f"/api/jobs/{job['id']}/karaoke", params={"semitones": 2, "download": True})
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert "canci%C3%B3n%20%28karaoke%20%2B2%29.mp3" in response.headers["content-disposition"]


def test_original_tone_needs_no_processing(settings, mp3_bytes):
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        response = client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 0})
        assert response.status_code == 200
        assert response.json()["status"] == "done"
        assert client.get(f"/api/jobs/{job['id']}").json()["pitch_versions"] == {}


def test_lossless_master_is_used_when_it_exists(settings, mp3_bytes, monkeypatch):
    sources = []

    async def recording_shift(source, destination, semitones):
        sources.append(source.name)
        destination.touch()

    monkeypatch.setattr(jobs, "shift_pitch", recording_shift)
    with run_client(settings, fake_separate_with_master) as client:
        job = done_job(client, mp3_bytes)
        client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 1})
        wait_for_pitch(client, job["id"], 1, "done")
    assert sources == [MASTER_FILENAME]


def test_songs_without_master_use_their_output(settings, mp3_bytes, monkeypatch):
    sources = []

    async def recording_shift(source, destination, semitones):
        sources.append(source.name)
        destination.touch()

    monkeypatch.setattr(jobs, "shift_pitch", recording_shift)
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 1})
        wait_for_pitch(client, job["id"], 1, "done")
    assert sources == ["karaoke.mp3"]


@pytest.mark.parametrize("semitones", [-7, 7, "dos"])
def test_out_of_range_semitones_are_rejected(settings, mp3_bytes, semitones):
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        assert client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": semitones}).status_code == 422
        assert client.get(f"/api/jobs/{job['id']}/karaoke", params={"semitones": semitones}).status_code == 422


def test_version_cannot_be_fetched_before_it_is_requested(settings, mp3_bytes):
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        assert client.get(f"/api/jobs/{job['id']}/karaoke", params={"semitones": 2}).status_code == 409


def test_pitch_requires_a_finished_song(settings, mp3_bytes):
    async def blocking_separate(*_):
        await asyncio.sleep(3600)

    with run_client(settings, blocking_separate) as client:
        job_id = upload(client, mp3_bytes).json()["id"]
        assert client.post(f"/api/jobs/{job_id}/pitch", params={"semitones": 1}).status_code == 409
        assert client.post("/api/jobs/missing/pitch", params={"semitones": 1}).status_code == 404


def test_failure_is_reported_and_can_be_retried(settings, mp3_bytes, monkeypatch):
    attempts = []

    async def flaky_shift(source, destination, semitones):
        attempts.append(semitones)
        if len(attempts) == 1:
            raise EncodingError("boom")
        destination.touch()

    monkeypatch.setattr(jobs, "shift_pitch", flaky_shift)
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 3})
        assert wait_for_pitch(client, job["id"], 3, "failed") == "failed"
        assert client.get(f"/api/jobs/{job['id']}/karaoke", params={"semitones": 3}).status_code == 409

        assert client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 3}).status_code == 202
        assert wait_for_pitch(client, job["id"], 3, "done") == "done"


def test_versions_run_one_at_a_time(settings, mp3_bytes, monkeypatch):
    running = []
    peak = []

    async def tracking_shift(source, destination, semitones):
        running.append(semitones)
        peak.append(len(running))
        await asyncio.sleep(0.05)
        running.remove(semitones)
        destination.touch()

    monkeypatch.setattr(jobs, "shift_pitch", tracking_shift)
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        for semitones in (1, 2, 3):
            client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": semitones})
        for semitones in (1, 2, 3):
            wait_for_pitch(client, job["id"], semitones, "done")
    assert max(peak) == 1


def test_deleting_a_song_cancels_its_pitch_generation(settings, mp3_bytes, monkeypatch):
    cancelled = []

    async def blocking_shift(source, destination, semitones):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(semitones)
            raise

    monkeypatch.setattr(jobs, "shift_pitch", blocking_shift)
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 1})
        time.sleep(0.1)
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert cancelled == [1]
    assert not (settings.jobs_dir / job["id"]).exists()


def test_restart_keeps_finished_versions_and_forgets_interrupted_ones(settings, mp3_bytes, monkeypatch):
    async def blocking_after_first(source, destination, semitones):
        if semitones == 2:
            await asyncio.sleep(3600)
        destination.touch()

    monkeypatch.setattr(jobs, "shift_pitch", blocking_after_first)
    with run_client(settings, fake_separate) as client:
        job = done_job(client, mp3_bytes)
        client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 1})
        wait_for_pitch(client, job["id"], 1, "done")
        client.post(f"/api/jobs/{job['id']}/pitch", params={"semitones": 2})
    (settings.jobs_dir / job["id"] / "karaoke+2.partial.mp3").touch()

    with run_client(settings, fake_separate) as client:
        assert client.get(f"/api/jobs/{job['id']}").json()["pitch_versions"] == {"1": "done"}
        assert client.get(f"/api/jobs/{job['id']}/karaoke", params={"semitones": 1}).status_code == 200
    assert not (settings.jobs_dir / job["id"] / "karaoke+2.partial.mp3").exists()


def test_config_exposes_the_range(tmp_path):
    with run_client(Settings(data_dir=tmp_path / "data"), fake_separate) as client:
        assert client.get("/api/config").json()["max_semitones"] == 6
