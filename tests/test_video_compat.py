from io import BytesIO
from pathlib import Path

from app.service.video_compat import transcode_hevc_to_h264


def test_non_hevc_video_does_not_create_copy(monkeypatch):
    def fake_run(command, **kwargs):
        class Result:
            stdout = "h264\n"
        return Result()

    monkeypatch.setattr("app.service.video_compat.subprocess.run", fake_run)
    assert transcode_hevc_to_h264(BytesIO(b"source")) is None


def test_hevc_video_is_transcoded_to_seekable_h264_file(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            class ProbeResult:
                stdout = "hevc\n"
            return ProbeResult()
        Path(command[-1]).write_bytes(b"decoded-h264")

        class EncodeResult:
            stdout = ""
        return EncodeResult()

    monkeypatch.setattr("app.service.video_compat.subprocess.run", fake_run)
    result = transcode_hevc_to_h264(BytesIO(b"source-hevc"))
    try:
        assert result.read() == b"decoded-h264"
        ffmpeg_command = next(command for command in commands if command[0] == "ffmpeg")
        assert "libx264" in ffmpeg_command
        assert "+faststart" in ffmpeg_command
        assert "yuv420p" in ffmpeg_command
    finally:
        result.close()


def test_hevc_transcode_reports_ffmpeg_progress(monkeypatch):
    reported = []

    def fake_run(command, **kwargs):
        class Result:
            stdout = "12.5\n" if "format=duration" in command else "hevc\n"
        return Result()

    def fake_progress(command, duration_seconds, callback):
        assert duration_seconds == 12.5
        callback(0)
        callback(42)
        callback(100)
        Path(command[-1]).write_bytes(b"decoded-with-progress")

    monkeypatch.setattr("app.service.video_compat.subprocess.run", fake_run)
    monkeypatch.setattr("app.service.video_compat._run_ffmpeg_with_progress", fake_progress)
    result = transcode_hevc_to_h264(BytesIO(b"source-hevc"), progress_callback=reported.append)
    try:
        assert result.read() == b"decoded-with-progress"
        assert reported == [0, 42, 100]
    finally:
        result.close()
