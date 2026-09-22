import asyncio
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import wave

from smartfetch.media import MediaFailure, inspect_timed_media, run_ffprobe


def _write_wav(path: Path, *, frame_count: int = 800) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * frame_count)


def _chunk(chunk_id: bytes, data: bytes, *, declared_size: int | None = None) -> bytes:
    size = len(data) if declared_size is None else declared_size
    return chunk_id + struct.pack("<I", size) + data + (b"\0" if len(data) & 1 else b"")


def _custom_wav(
    *,
    data_chunks: tuple[bytes, ...] = (b"\0\0",),
    junk_chunks: int = 0,
    format_tag: int = 1,
    sample_rate: int = 8_000,
    byte_rate: int = 16_000,
) -> bytes:
    format_data = struct.pack("<HHIIHH", format_tag, 1, sample_rate, byte_rate, 2, 16)
    body = b"WAVE" + _chunk(b"fmt ", format_data)
    body += b"JUNK\0\0\0\0" * junk_chunks
    body += b"".join(_chunk(b"data", data) for data in data_chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


async def _pipe_only_wav_metadata(_path: Path) -> dict:
    return {
        "format": {"format_name": "wav"},
        "streams": [{"codec_name": "pcm_s16le", "codec_type": "audio"}],
    }


class WavDurationFallbackTests(unittest.TestCase):
    def _inspect(self, content: bytes):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            path.write_bytes(content)
            return asyncio.run(
                inspect_timed_media(
                    path,
                    "audio",
                    "audio/wav",
                    runner=_pipe_only_wav_metadata,
                )
            )

    def test_wav_fallback_accepts_exactly_4096_chunks(self):
        result = self._inspect(_custom_wav(junk_chunks=4_094))

        self.assertAlmostEqual(result.duration_seconds, 1 / 8_000)

    def test_wav_fallback_rejects_chunk_4097(self):
        with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
            self._inspect(_custom_wav(junk_chunks=4_095))

    def test_wav_fallback_rejects_dense_hostile_file_without_full_scan(self):
        content = _custom_wav(junk_chunks=500_000)

        started = time.perf_counter()
        with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
            self._inspect(content)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)

    def test_wav_fallback_rejects_second_misaligned_data_chunk(self):
        with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
            self._inspect(_custom_wav(data_chunks=(b"\0", b"\0")))

    def test_wav_fallback_rejects_truncated_chunk(self):
        body = b"WAVE" + _chunk(
            b"fmt ", struct.pack("<HHIIHH", 1, 1, 8_000, 16_000, 2, 16)
        )
        body += _chunk(b"JUNK", b"x", declared_size=100)
        content = b"RIFF" + struct.pack("<I", len(body)) + body

        with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
            self._inspect(content)

    def test_wav_fallback_rejects_inconsistent_byte_rate(self):
        with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
            self._inspect(_custom_wav(byte_rate=15_999))

    def test_wav_fallback_rejects_unsupported_format_tag(self):
        with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
            self._inspect(_custom_wav(format_tag=17))

    def test_wav_fallback_preserves_zero_and_duration_limits(self):
        self.assertEqual(
            self._inspect(_custom_wav(data_chunks=(b"",))).duration_seconds,
            0.0,
        )
        self.assertEqual(
            self._inspect(
                _custom_wav(
                    data_chunks=(b"\0" * 3_600,),
                    sample_rate=1,
                    byte_rate=2,
                )
            ).duration_seconds,
            1_800.0,
        )
        with self.assertRaisesRegex(MediaFailure, "^source_too_large$"):
            self._inspect(
                _custom_wav(
                    data_chunks=(b"\0" * 3_602,),
                    sample_rate=1,
                    byte_rate=2,
                )
            )


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is required for subprocess integration tests")
class FfprobeSubprocessTests(unittest.TestCase):
    def test_default_runner_accepts_valid_pcm_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            _write_wav(path)

            result = asyncio.run(run_ffprobe(path))

        self.assertEqual(result["format"]["format_name"], "wav")
        self.assertEqual(result["streams"], [{"codec_name": "pcm_s16le", "codec_type": "audio"}])

    def test_default_runner_validates_pipe_only_pcm_wav_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            _write_wav(path)

            result = asyncio.run(inspect_timed_media(path, "audio", "audio/wav"))

        self.assertEqual(result.format_name, "wav")
        self.assertAlmostEqual(result.duration_seconds, 0.1)

    def test_default_runner_tolerates_successful_early_pipe_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-audio.wav"
            _write_wav(path, frame_count=150_000)

            result = asyncio.run(run_ffprobe(path))

        self.assertEqual(result["format"]["format_name"], "wav")
        self.assertEqual(
            result["streams"],
            [{"codec_name": "pcm_s16le", "codec_type": "audio"}],
        )

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required to create the WebM fixture")
    def test_default_runner_accepts_approved_webm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.webm"
            subprocess.run(
                [
                    shutil.which("ffmpeg"),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=16x16:r=10:d=0.2",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=8000:cl=mono",
                    "-t",
                    "0.2",
                    "-c:v",
                    "libvpx-vp9",
                    "-c:a",
                    "libopus",
                    "-y",
                    str(path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )

            result = asyncio.run(inspect_timed_media(path, "video", "video/webm"))

        self.assertEqual(result.format_name, "matroska,webm")
        self.assertGreater(result.duration_seconds, 0)

    def test_default_runner_keeps_malformed_and_disallowed_media_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.wav"
            malformed.write_bytes(b"not media")
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                asyncio.run(run_ffprobe(malformed))

            wav = Path(directory) / "audio.wav"
            _write_wav(wav)
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                asyncio.run(inspect_timed_media(wav, "audio", "audio/ogg"))


if __name__ == "__main__":
    unittest.main()
