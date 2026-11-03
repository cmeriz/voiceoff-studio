"""Job queue. One worker processes one song at a time; state lives in job.json files."""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.audio import AudioInfo
from app.config import Settings
from app.separator import SeparationError, separate_vocals

logger = logging.getLogger(__name__)

QUEUED = "queued"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
PENDING_STATUSES = {QUEUED, PROCESSING}

INTERRUPTED_MESSAGE = "El servicio se reinició durante el procesamiento. Vuelve a subir la canción."
TIMEOUT_MESSAGE = "El procesamiento superó el tiempo máximo permitido."
UNEXPECTED_MESSAGE = "Ocurrió un error inesperado al procesar el audio."


class QueueFull(Exception):
    pass


@dataclass
class Job:
    id: str
    filename: str
    format: str
    duration: float
    created_at: float
    status: str = QUEUED
    progress: int = 0
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class JobManager:
    def __init__(self, settings: Settings, separate=separate_vocals):
        self.settings = settings
        self._separate = separate
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._current_id: str | None = None
        self._current_task: asyncio.Task | None = None
        self._background: list[asyncio.Task] = []

    async def start(self) -> None:
        self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.settings.uploads_dir, ignore_errors=True)
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._load_existing()
        self._background = [
            asyncio.create_task(self._worker()),
            asyncio.create_task(self._cleanup_loop()),
        ]

    async def stop(self) -> None:
        tasks = list(self._background)
        if self._current_task:
            tasks.append(self._current_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def has_capacity(self) -> bool:
        pending = sum(job.status in PENDING_STATUSES for job in self._jobs.values())
        return pending < self.settings.max_pending_jobs

    def create(self, upload_path: Path, filename: str, info: AudioInfo) -> Job:
        if not self.has_capacity():
            raise QueueFull
        job = Job(
            id=uuid.uuid4().hex,
            filename=filename,
            format=info.format,
            duration=info.duration,
            created_at=time.time(),
        )
        self.job_dir(job.id).mkdir(parents=True)
        os.replace(upload_path, self.input_path(job))
        self._save(job)
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    async def delete(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        if self._current_id == job_id and self._current_task:
            self._current_task.cancel()
            await asyncio.gather(self._current_task, return_exceptions=True)
        await asyncio.to_thread(shutil.rmtree, self.job_dir(job_id), ignore_errors=True)
        return True

    def job_dir(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    def input_path(self, job: Job) -> Path:
        return self.job_dir(job.id) / f"input.{job.format}"

    def output_path(self, job: Job) -> Path:
        return self.job_dir(job.id) / f"karaoke.{job.format}"

    def serialize(self, job: Job) -> dict:
        now = time.time()
        return {
            "id": job.id,
            "filename": job.filename,
            "format": job.format,
            "duration": job.duration,
            "status": job.status,
            "progress": job.progress,
            "error": job.error,
            "elapsed_seconds": now - job.started_at if job.status == PROCESSING else None,
            "expires_in_seconds": max(self._expires_at(job) - now, 0) if job.finished_at else None,
        }

    def _expires_at(self, job: Job) -> float:
        return job.finished_at + self.settings.retention_hours * 3600

    def _save(self, job: Job) -> None:
        path = self.job_dir(job.id) / "job.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(job)), encoding="utf-8")
        os.replace(temporary, path)

    def _load_existing(self) -> None:
        """Restores finished jobs. Anything unfinished was interrupted by a restart."""
        for directory in self.settings.jobs_dir.iterdir():
            try:
                job = Job(**json.loads((directory / "job.json").read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                logger.warning("Removing unreadable job directory %s", directory.name)
                shutil.rmtree(directory, ignore_errors=True)
                continue
            if job.status in PENDING_STATUSES:
                self._finish(job, FAILED, error=INTERRUPTED_MESSAGE)
            self._jobs[job.id] = job

    def _finish(self, job: Job, status: str, error: str | None = None) -> None:
        job.status = status
        job.error = error
        job.finished_at = time.time()
        if status == DONE:
            job.progress = 100
        self._save(job)

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:  # Deleted while it was waiting in the queue.
                continue
            self._current_id = job_id
            self._current_task = asyncio.create_task(self._process(job))
            await asyncio.wait([self._current_task])
            self._current_id = None
            self._current_task = None

    async def _process(self, job: Job) -> None:
        job.status = PROCESSING
        job.started_at = time.time()
        self._save(job)

        def on_progress(percent: int) -> None:
            job.progress = max(job.progress, percent)

        try:
            await asyncio.wait_for(
                self._separate(
                    self.settings,
                    self.input_path(job),
                    self.job_dir(job.id) / "work",
                    self.output_path(job),
                    job.format,
                    on_progress,
                ),
                self.settings.job_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._finish(job, FAILED, error=TIMEOUT_MESSAGE)
        except SeparationError as error:
            self._finish(job, FAILED, error=str(error))
        except Exception:
            logger.exception("Job %s failed", job.id)
            self._finish(job, FAILED, error=UNEXPECTED_MESSAGE)
        else:
            self._finish(job, DONE)

    async def _cleanup_loop(self) -> None:
        while True:
            await self._remove_expired()
            await asyncio.sleep(self.settings.cleanup_interval_seconds)

    async def _remove_expired(self) -> None:
        now = time.time()
        expired = [
            job.id for job in self._jobs.values()
            if job.finished_at and self._expires_at(job) <= now
        ]
        for job_id in expired:
            await self.delete(job_id)
