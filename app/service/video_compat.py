"""Generate a browser-compatible H.264 copy when an uploaded video is HEVC."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable


def _copy_to_path(fileobj, path: str) -> None:
    original_position = None
    try:
        original_position = fileobj.tell()
    except Exception:
        pass
    fileobj.seek(0)
    with open(path, "wb") as target:
        shutil.copyfileobj(fileobj, target, length=1024 * 1024)
    if original_position is not None:
        fileobj.seek(original_position)


def _run_ffmpeg_with_progress(command: list[str], duration_seconds: float,
                              progress_callback: Callable[[int], None]) -> None:
    """Run ffmpeg and report integer progress from 0 through 100."""
    progress_command = command[:-1] + [
        "-progress", "pipe:1", "-stats_period", "0.5", command[-1],
    ]
    process = subprocess.Popen(
        progress_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timer = threading.Timer(7200, process.kill)
    timer.daemon = True
    timer.start()
    output_tail: list[str] = []
    last_progress = -1
    try:
        progress_callback(0)
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            output_tail.append(line)
            output_tail = output_tail[-30:]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key not in {"out_time_us", "out_time_ms"}:
                continue
            try:
                percent = min(99, max(0, int(float(value) / 1_000_000 / duration_seconds * 100)))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if percent != last_progress:
                last_progress = percent
                progress_callback(percent)
        return_code = process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(
                return_code, progress_command, output="\n".join(output_tail),
            )
        progress_callback(100)
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()


def transcode_hevc_to_h264(fileobj, progress_callback: Callable[[int], None] | None = None):
    """Return a seekable H.264/AAC MP4 file, or ``None`` for non-HEVC input.

    The returned temporary file is owned by the caller and must be closed.
    ``ffprobe``/``ffmpeg`` failures are raised so the API layer can log the
    failure while preserving the already-uploaded source object.
    """
    source_fd, source_path = tempfile.mkstemp(suffix=".mp4")
    output_fd, output_path = tempfile.mkstemp(suffix=".mp4")
    os.close(source_fd)
    os.close(output_fd)
    os.unlink(output_path)
    try:
        _copy_to_path(fileobj, source_path)
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
                source_path,
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        if probe.stdout.strip().lower() not in {"hevc", "h265"}:
            return None

        ffmpeg_command = [
            "ffmpeg", "-v", "error", "-y", "-i", source_path,
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", output_path,
        ]
        if progress_callback is None:
            subprocess.run(
                ffmpeg_command,
                check=True, capture_output=True, text=True, timeout=7200,
            )
        else:
            duration_probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", source_path,
                ],
                check=True, capture_output=True, text=True, timeout=30,
            )
            duration_seconds = float(duration_probe.stdout.strip())
            if duration_seconds <= 0:
                raise RuntimeError("无法读取源视频时长")
            _run_ffmpeg_with_progress(ffmpeg_command, duration_seconds, progress_callback)
        result = tempfile.TemporaryFile(mode="w+b")
        with open(output_path, "rb") as source:
            shutil.copyfileobj(source, result, length=1024 * 1024)
        result.seek(0)
        return result
    finally:
        for path in (source_path, output_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
