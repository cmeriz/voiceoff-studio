"""HTTP API and static frontend."""

import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath

import anyio
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.audio import InvalidAudio, probe
from app.config import Settings
from app.jobs import DONE, PROCESSING, Job, JobManager, QueueFull

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_SUFFIXES = {".mp3", ".wav"}
MAX_SEMITONES = 6
MEDIA_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav"}
QUEUE_FULL_MESSAGE = "La cola está llena. Espera a que terminen las canciones en curso."


def clean_filename(filename: str) -> str:
    """Keeps only the final path component, without control characters."""
    name = PureWindowsPath(filename.replace("/", "\\")).name
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()


def create_app(settings: Settings | None = None, manager: JobManager | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    manager = manager or JobManager(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        yield
        await manager.stop()

    app = FastAPI(title="VoiceOff Studio", lifespan=lifespan)
    api = APIRouter(prefix="/api")

    def find_job(job_id: str) -> Job:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, "La canción no existe o ya fue eliminada.")
        return job

    @api.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @api.get("/config")
    async def config() -> dict:
        return {
            "max_upload_mb": settings.max_upload_mb,
            "max_duration_seconds": settings.max_duration_seconds,
            "retention_hours": settings.retention_hours,
            "max_semitones": MAX_SEMITONES,
        }

    @api.post("/jobs", status_code=201)
    async def create_job(request: Request, filename: str = Query(min_length=1, max_length=255)) -> dict:
        """Receives the audio as the raw request body, so it can be size-limited while streaming."""
        name = clean_filename(filename)
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(415, "Solo se admiten archivos MP3 o WAV.")
        too_large = HTTPException(413, f"El archivo supera el máximo de {settings.max_upload_mb} MB.")
        declared_size = request.headers.get("content-length", "")
        if declared_size.isdigit() and int(declared_size) > settings.max_upload_bytes:
            raise too_large
        if not manager.has_capacity():
            raise HTTPException(503, QUEUE_FULL_MESSAGE)

        # No extension on purpose: ffprobe must judge the content, not the name.
        temporary = settings.uploads_dir / uuid.uuid4().hex
        try:
            size = 0
            async with await anyio.open_file(temporary, "wb") as file:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise too_large
                    await file.write(chunk)
            if size == 0:
                raise HTTPException(400, "El archivo está vacío.")

            try:
                info = await probe(temporary)
            except InvalidAudio as error:
                raise HTTPException(415, str(error)) from None
            if info.duration > settings.max_duration_seconds:
                minutes = settings.max_duration_seconds // 60
                raise HTTPException(413, f"El audio supera el máximo de {minutes} minutos.")

            try:
                job = manager.create(temporary, name, info)
            except QueueFull:
                raise HTTPException(503, QUEUE_FULL_MESSAGE) from None
        finally:
            temporary.unlink(missing_ok=True)
        return manager.serialize(job)

    @api.get("/jobs")
    async def list_jobs() -> list[dict]:
        return [manager.serialize(job) for job in manager.list()]

    @api.delete("/jobs")
    async def delete_all_jobs() -> dict:
        return {"deleted": await manager.delete_all()}

    @api.delete("/pitch-versions")
    async def delete_pitch_versions() -> dict:
        return {"deleted": await manager.delete_pitch_versions()}

    @api.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        return manager.serialize(find_job(job_id))

    @api.delete("/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: str) -> None:
        if not await manager.delete(job_id):
            raise HTTPException(404, "La canción no existe o ya fue eliminada.")

    @api.get("/jobs/{job_id}/original")
    async def get_original(job_id: str) -> FileResponse:
        job = find_job(job_id)
        return FileResponse(manager.input_path(job), media_type=MEDIA_TYPES[job.format])

    @api.post("/jobs/{job_id}/pitch")
    async def request_pitch(
        job_id: str,
        response: Response,
        semitones: int = Query(ge=-MAX_SEMITONES, le=MAX_SEMITONES),
    ) -> dict:
        job = find_job(job_id)
        if job.status != DONE:
            raise HTTPException(409, "La versión karaoke todavía no está lista.")
        status = manager.request_pitch(job, semitones)
        if status == PROCESSING:
            response.status_code = 202
        return {"semitones": semitones, "status": status}

    @api.get("/jobs/{job_id}/karaoke")
    async def get_karaoke(
        job_id: str,
        download: bool = False,
        semitones: int = Query(default=0, ge=-MAX_SEMITONES, le=MAX_SEMITONES),
    ) -> FileResponse:
        job = find_job(job_id)
        if job.status != DONE:
            raise HTTPException(409, "La versión karaoke todavía no está lista.")
        if semitones != 0 and job.pitch_versions.get(str(semitones)) != DONE:
            raise HTTPException(409, "Ese tono todavía no está listo.")
        suffix = f" {semitones:+d}" if semitones else ""
        return FileResponse(
            manager.karaoke_path(job, semitones),
            media_type=MEDIA_TYPES[job.format],
            filename=f"{Path(job.filename).stem} (karaoke{suffix}).{job.format}",
            content_disposition_type="attachment" if download else "inline",
        )

    app.include_router(api)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
