"""FFmpeg helpers: inspect uploads by their real content and encode results."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_FORMATS = {"mp3", "wav"}
PROBE_TIMEOUT_SECONDS = 30
ENCODERS = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    ".wav": ["-c:a", "pcm_s24le"],
}


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
    await _encode(source, destination)


async def shift_pitch(source: Path, destination: Path, semitones: int) -> None:
    """Writes source to destination, in destination's format, keeping its tempo."""
    partial = destination.with_name(f"{destination.stem}.partial{destination.suffix}")
    ratio = 2 ** (semitones / 12)
    try:
        await _encode(source, partial, "-af", f"rubberband=pitch={ratio:.6f}:pitchq=quality:channels=together")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


async def _encode(source: Path, destination: Path, *filters: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin", "-y",
        "-v", "error",
        "-i", str(source),
        *filters,
        *ENCODERS[destination.suffix],
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
