"""Job queue. One worker processes one song at a time; state lives in job.json files."""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.audio import AudioInfo, EncodingError, shift_pitch
from app.config import Settings
from app.separator import MASTER_FILENAME, SeparationError, separate_vocals

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
    pitch_versions: dict[str, str] = field(default_factory=dict)  # Semitones -> status.


class JobManager:
    def __init__(self, settings: Settings, separate=separate_vocals):
        self.settings = settings
        self._separate = separate
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._current_id: str | None = None
        self._current_task: asyncio.Task | None = None
        self._background: list[asyncio.Task] = []
        self._pitch_tasks: dict[str, set[asyncio.Task]] = {}
        self._pitch_slot = asyncio.Semaphore(1)

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
        for pitch_tasks in self._pitch_tasks.values():
            tasks.extend(pitch_tasks)
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
        pitch_tasks = self._pitch_tasks.pop(job_id, set())
        for task in pitch_tasks:
            task.cancel()
        await asyncio.gather(*pitch_tasks, return_exceptions=True)
        await asyncio.to_thread(shutil.rmtree, self.job_dir(job_id), ignore_errors=True)
        return True

    async def delete_all(self) -> int:
        # The running job goes last, so the worker has nothing left to start when it stops.
        job_ids = [job.id for job in self.list() if job.id != self._current_id]
        if self._current_id in self._jobs:
            job_ids.append(self._current_id)
        for job_id in job_ids:
            await self.delete(job_id)
        return len(job_ids)

    async def delete_pitch_versions(self) -> int:
        """Removes every pitch version and keeps the songs. Returns how many were kept or running."""
        jobs = list(self._jobs.values())
        removed = sum(status in (DONE, PROCESSING) for job in jobs for status in job.pitch_versions.values())
        tasks = [task for job_tasks in self._pitch_tasks.values() for task in job_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in jobs:
            if not job.pitch_versions:
                continue
            job.pitch_versions = {}
            self._save(job)
            for path in self.job_dir(job.id).glob("karaoke[+-]*"):
                path.unlink(missing_ok=True)
        return removed

    def request_pitch(self, job: Job, semitones: int) -> str:
        """Returns the version's status, starting its generation if needed."""
        if semitones == 0:
            return DONE
        key = str(semitones)
        status = job.pitch_versions.get(key)
        if status in (DONE, PROCESSING):
            return status
        job.pitch_versions[key] = PROCESSING
        task = asyncio.create_task(self._generate_pitch(job, semitones))
        tasks = self._pitch_tasks.setdefault(job.id, set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return PROCESSING

    def karaoke_path(self, job: Job, semitones: int = 0) -> Path:
        if semitones == 0:
            return self.output_path(job)
        return self.job_dir(job.id) / f"karaoke{semitones:+d}.{job.format}"

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
            "pitch_versions": job.pitch_versions,
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
            job.pitch_versions = {key: status for key, status in job.pitch_versions.items() if status == DONE}
            for partial in directory.glob("*.partial.*"):
                partial.unlink(missing_ok=True)
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

    def _pitch_source(self, job: Job) -> Path:
        """Prefers the lossless master. Songs processed before it existed only have their output."""
        master = self.job_dir(job.id) / MASTER_FILENAME
        return master if master.is_file() else self.output_path(job)

    async def _generate_pitch(self, job: Job, semitones: int) -> None:
        status = DONE
        try:
            async with self._pitch_slot:
                await asyncio.wait_for(
                    shift_pitch(self._pitch_source(job), self.karaoke_path(job, semitones), semitones),
                    self.settings.job_timeout_seconds,
                )
        except (EncodingError, TimeoutError, OSError):
            logger.exception("Pitch shift %+d failed for job %s", semitones, job.id)
            status = FAILED
        job.pitch_versions[str(semitones)] = status
        self._save(job)

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
