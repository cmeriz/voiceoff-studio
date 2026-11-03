"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    max_upload_mb: int = 100
    max_duration_seconds: int = 15 * 60
    max_pending_jobs: int = 10
    retention_hours: int = 24
    job_timeout_seconds: int = 2 * 60 * 60
    cleanup_interval_seconds: int = 15 * 60
    demucs_model: str = "htdemucs"
    demucs_device: str = "cpu"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", str(defaults.data_dir))),
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", defaults.max_upload_mb)),
            max_duration_seconds=int(os.getenv("MAX_DURATION_SECONDS", defaults.max_duration_seconds)),
            max_pending_jobs=int(os.getenv("MAX_PENDING_JOBS", defaults.max_pending_jobs)),
            retention_hours=int(os.getenv("RETENTION_HOURS", defaults.retention_hours)),
            job_timeout_seconds=int(os.getenv("JOB_TIMEOUT_SECONDS", defaults.job_timeout_seconds)),
            cleanup_interval_seconds=int(os.getenv("CLEANUP_INTERVAL_SECONDS", defaults.cleanup_interval_seconds)),
            demucs_model=os.getenv("DEMUCS_MODEL", defaults.demucs_model),
            demucs_device=os.getenv("DEMUCS_DEVICE", defaults.demucs_device),
        )
