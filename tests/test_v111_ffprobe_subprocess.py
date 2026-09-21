import asyncio
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave

from smartfetch.media import MediaFailure, inspect_timed_media, run_ffprobe


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 800)


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is required for subprocess integration tests")
class FfprobeSubprocessTests(unittest.TestCase):
    def test_default_runner_accepts_valid_pcm_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            _write_wav(path)

            result = asyncio.run(run_ffprobe(path))

        self.assertEqual(result["format"]["format_name"], "wav")
        self.assertEqual(result["streams"], [{"codec_name": "pcm_s16le", "codec_type": "audio"}])

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
