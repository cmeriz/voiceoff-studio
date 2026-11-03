import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import JobManager
from app.main import create_app


def make_audio(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-ac", "2", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def mp3_bytes(tmp_path) -> bytes:
    return make_audio(tmp_path / "sample.mp3").read_bytes()


@pytest.fixture
def wav_bytes(tmp_path) -> bytes:
    return make_audio(tmp_path / "sample.wav").read_bytes()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", max_upload_mb=1, max_duration_seconds=5, max_pending_jobs=2)


def run_client(settings: Settings, separate):
    return TestClient(create_app(settings, JobManager(settings, separate=separate)))
