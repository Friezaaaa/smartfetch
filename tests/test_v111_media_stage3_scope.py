from pathlib import Path
import unittest

from smartfetch.media import MEDIA_LIMITS


ROOT = Path(__file__).resolve().parents[1]


class Stage3ScopeTests(unittest.TestCase):
    def test_exact_dependencies_and_ffprobe_container_package(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("Pillow==12.3.0", requirements)
        self.assertIn("pypdf==6.18.0", requirements)
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ffmpeg=7:7.1.5-0+deb13u1", dockerfile)
        self.assertIn('CMD ["python", "-m", "smartfetch.server"]', dockerfile)

    def test_stage3_is_not_imported_by_public_surfaces(self):
        for name in ("smartfetch/server.py", "smartfetch/mcp_server.py", "smartfetch/payments.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("smartfetch.media", text)
            self.assertNotIn("from .media", text)

    def test_no_independent_video_frame_or_rate_limit(self):
        video = MEDIA_LIMITS["video"]
        self.assertIsNone(video.max_frames)
        self.assertEqual(video.max_duration_seconds, 600)


if __name__ == "__main__":
    unittest.main()
