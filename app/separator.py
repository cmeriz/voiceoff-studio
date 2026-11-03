"""Runs Demucs in a subprocess, so its memory is released after every song."""

import asyncio
import logging
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from app.audio import encode_mp3
from app.config import Settings

logger = logging.getLogger(__name__)

# tqdm writes lines such as " 45%|████      | 31.2/70.2 [00:10<00:12]" to stderr.
PROGRESS_PATTERN = re.compile(rb"(\d{1,3})%\|")
STDERR_TAIL_BYTES = 4000
# Share of the progress bar reserved for Demucs; the rest covers encoding.
SEPARATION_PROGRESS_SHARE = 0.95
OUT_OF_MEMORY_RETURN_CODES = {-9, 137}


class SeparationError(Exception):
    """Demucs failed; the message is safe to show to the user."""


def build_command(settings: Settings, input_path: Path, work_dir: Path) -> list[str]:
    return [
        sys.executable, "-m", "demucs.separate",
        "-n", settings.demucs_model,
        "-d", settings.demucs_device,
        "--two-stems", "vocals",
        "--int24",
        "-o", str(work_dir),
        str(input_path),
    ]


async def _read_progress(
    stream: asyncio.StreamReader, on_progress: Callable[[int], None]
) -> bytes:
    tail = b""
    while chunk := await stream.read(4096):
        tail = (tail + chunk)[-STDERR_TAIL_BYTES:]
        matches = PROGRESS_PATTERN.findall(chunk)
        if matches:
            on_progress(min(int(matches[-1]), 100))
    return tail


async def separate_vocals(
    settings: Settings,
    input_path: Path,
    work_dir: Path,
    output_path: Path,
    output_format: str,
    on_progress: Callable[[int], None],
) -> None:
    """Writes the song without vocals to output_path, in the requested format."""
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            *build_command(settings, input_path, work_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            tail = await _read_progress(
                process.stderr,
                lambda percent: on_progress(int(percent * SEPARATION_PROGRESS_SHARE)),
            )
            await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise

        if process.returncode != 0:
            logger.error(
                "Demucs exited with %s: %s",
                process.returncode,
                tail.decode(errors="replace"),
            )
            if process.returncode in OUT_OF_MEMORY_RETURN_CODES:
                raise SeparationError("El sistema se quedó sin memoria al procesar el audio.")
            raise SeparationError("No se pudo separar la voz de este audio.")

        stem = work_dir / settings.demucs_model / input_path.stem / "no_vocals.wav"
        if not stem.is_file():
            raise SeparationError("El modelo no generó ninguna pista.")

        if output_format == "mp3":
            await encode_mp3(stem, output_path)
        else:
            shutil.move(stem, output_path)
    finally:
        await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)
