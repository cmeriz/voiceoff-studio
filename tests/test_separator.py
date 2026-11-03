"""Exercises the real subprocess handling with a small stand-in for Demucs."""

import asyncio
import shutil
import sys
import textwrap

import pytest

from app import separator
from app.config import Settings
from app.separator import SeparationError, separate_vocals
from tests.conftest import make_audio


def use_fake_demucs(monkeypatch, tmp_path, body):
    script = tmp_path / "fake_demucs.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr(
        separator,
        "build_command",
        lambda settings, input_path, work_dir: [sys.executable, str(script), str(input_path), str(work_dir)],
    )


SUCCESS = """
    import shutil, sys
    from pathlib import Path
    source, work = Path(sys.argv[1]), Path(sys.argv[2])
    sys.stderr.write("  0%|     | 0/10 [00:00<?]\\r")
    sys.stderr.write(" 50%|##   | 5/10 [00:01<00:01]\\r")
    sys.stderr.write("100%|#####| 10/10 [00:02<00:00]\\n")
    stem = work / "htdemucs" / source.stem
    stem.mkdir(parents=True)
    shutil.copy(source, stem / "no_vocals.wav")
"""


def run(settings, source, output, output_format):
    seen = []
    asyncio.run(separate_vocals(settings, source, output.parent / "work", output, output_format, seen.append))
    return seen


def test_wav_output_and_progress(monkeypatch, tmp_path):
    use_fake_demucs(monkeypatch, tmp_path, SUCCESS)
    source = make_audio(tmp_path / "input.wav")
    output = tmp_path / "karaoke.wav"

    seen = run(Settings(), source, output, "wav")

    assert output.read_bytes() == source.read_bytes()
    assert seen == sorted(seen) and seen[-1] == 95
    assert not (tmp_path / "work").exists()


def test_mp3_output_is_encoded(monkeypatch, tmp_path):
    use_fake_demucs(monkeypatch, tmp_path, SUCCESS)
    source = make_audio(tmp_path / "input.wav")
    output = tmp_path / "karaoke.mp3"

    run(Settings(), source, output, "mp3")

    assert shutil.which("ffprobe")
    from app.audio import probe
    assert asyncio.run(probe(output)).format == "mp3"
    assert (tmp_path / separator.MASTER_FILENAME).read_bytes() == source.read_bytes()


def test_failure_gives_a_user_message_and_cleans_up(monkeypatch, tmp_path):
    use_fake_demucs(monkeypatch, tmp_path, "import sys; sys.stderr.write('boom'); sys.exit(1)")
    source = make_audio(tmp_path / "input.wav")

    with pytest.raises(SeparationError, match="No se pudo separar"):
        run(Settings(), source, tmp_path / "karaoke.wav", "wav")
    assert not (tmp_path / "work").exists()


def test_being_killed_is_reported_as_out_of_memory(monkeypatch, tmp_path):
    use_fake_demucs(monkeypatch, tmp_path, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    source = make_audio(tmp_path / "input.wav")

    with pytest.raises(SeparationError, match="memoria"):
        run(Settings(), source, tmp_path / "karaoke.wav", "wav")


def test_missing_output_is_an_error(monkeypatch, tmp_path):
    use_fake_demucs(monkeypatch, tmp_path, "pass")
    source = make_audio(tmp_path / "input.wav")

    with pytest.raises(SeparationError, match="no generó"):
        run(Settings(), source, tmp_path / "karaoke.wav", "wav")


def test_cancelling_kills_the_process(monkeypatch, tmp_path):
    marker = tmp_path / "survived"
    use_fake_demucs(
        monkeypatch,
        tmp_path,
        f"""
        import time
        time.sleep(1.5)
        open({str(marker)!r}, "w").close()
        """,
    )
    source = make_audio(tmp_path / "input.wav")

    async def scenario():
        task = asyncio.create_task(
            separate_vocals(Settings(), source, tmp_path / "work", tmp_path / "out.wav", "wav", lambda _: None)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.6)

    asyncio.run(scenario())
    assert not marker.exists()
    assert not (tmp_path / "work").exists()
