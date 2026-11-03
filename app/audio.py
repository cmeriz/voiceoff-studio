"""FFmpeg helpers: inspect uploads by their real content and encode results."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_FORMATS = {"mp3", "wav"}
PROBE_TIMEOUT_SECONDS = 30


class InvalidAudio(Exception):
    """The file is not a readable MP3 or WAV."""


class EncodingError(Exception):
    """FFmpeg could not encode the result."""


@dataclass(frozen=True)
class AudioInfo:
    format: str
    duration: float


async def probe(path: Path) -> AudioInfo:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "format=format_name,duration:stream=codec_type",
        "-of", "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise InvalidAudio("No se pudo analizar el archivo.") from None

    if process.returncode != 0:
        raise InvalidAudio("El archivo no es un audio MP3 o WAV válido.")

    data = json.loads(stdout or b"{}")
    file_format = data.get("format", {}).get("format_name")
    if file_format not in SUPPORTED_FORMATS or not data.get("streams"):
        raise InvalidAudio("El archivo no es un audio MP3 o WAV válido.")

    try:
        duration = float(data["format"]["duration"])
    except (KeyError, ValueError):
        raise InvalidAudio("No se pudo determinar la duración del audio.") from None
    if duration <= 0:
        raise InvalidAudio("El archivo de audio está vacío.")

    return AudioInfo(format=file_format, duration=duration)


async def encode_mp3(source: Path, destination: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin", "-y",
        "-v", "error",
        "-i", str(source),
        "-c:a", "libmp3lame",
        "-b:a", "320k",
        str(destination),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise EncodingError(stderr.decode(errors="replace").strip())
