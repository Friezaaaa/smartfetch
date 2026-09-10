import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from PIL import Image
from pypdf import PdfWriter

from smartfetch.media import (
    GEMINI_INLINE_REQUEST_MAX_BYTES,
    MEDIA_LIMITS,
    DownloadedMedia,
    MediaFailure,
    PinnedTarget,
    choose_delivery,
    inspect_image,
    inspect_pdf,
    inspect_timed_media,
    ingest_remote_media,
    media_download,
    run_ffprobe,
    retrieve_webpage,
)


def _target(value):
    parsed = urlsplit(value)
    return PinnedTarget(value, parsed.hostname or "example.com", parsed.port or 443, ("8.8.8.8",))


def _echo_fetch(url, *, force_browser, max_chars):
    return {"success": True, "url": url, "force_browser": force_browser, "max_chars": max_chars}


class _Response:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def open(self, target, *, connect_timeout, read_timeout):
        self.urls.append(target.url)
        return self.responses.pop(0)


class MediaContractTests(unittest.TestCase):
    def test_exact_limits(self):
        self.assertEqual(MEDIA_LIMITS["image"].max_bytes, 10 * 1024 * 1024)
        self.assertEqual(MEDIA_LIMITS["image"].max_pixels, 20_000_000)
        self.assertEqual(MEDIA_LIMITS["image"].max_frames, 1)
        self.assertEqual(MEDIA_LIMITS["pdf"].max_bytes, 20 * 1024 * 1024)
        self.assertEqual(MEDIA_LIMITS["pdf"].max_pages, 20)
        self.assertEqual(MEDIA_LIMITS["audio"].max_bytes, 25 * 1024 * 1024)
        self.assertEqual(MEDIA_LIMITS["audio"].max_duration_seconds, 1800)
        self.assertEqual(MEDIA_LIMITS["video"].max_bytes, 50 * 1024 * 1024)
        self.assertEqual(MEDIA_LIMITS["video"].max_duration_seconds, 600)

    def test_only_finite_failure_codes(self):
        with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
            raise MediaFailure("unsupported_media_type")
        with self.assertRaises(ValueError):
            MediaFailure("CANARY-secret")

    def test_inline_accounting_and_image_never_uploads(self):
        content = b"abc"
        inline = choose_delivery(
            source_type="image", mime_type="image/png", content=content,
            schema={"type": "object"}, instructions="extract", prompt="p",
        )
        self.assertEqual(inline.kind, "inline")
        encoded = json.dumps(inline.payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), GEMINI_INLINE_REQUEST_MAX_BYTES)
        with patch("smartfetch.media.GEMINI_INLINE_REQUEST_MAX_BYTES", 8):
            with self.assertRaisesRegex(MediaFailure, "^source_too_large$"):
                choose_delivery(source_type="image", mime_type="image/png", content=content,
                                schema={}, instructions=None, prompt="p")

    def test_permitted_non_image_uses_inline_delivery(self):
        result = choose_delivery(
            source_type="pdf", mime_type="application/pdf",
            content=b"x" * 20, schema={}, instructions=None, prompt="p",
        )
        self.assertEqual(result.kind, "inline")


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingestion_inspects_before_yield_and_cleans_afterward(self):
        output = io.BytesIO(); Image.new("RGB", (4, 5), "white").save(output, format="PNG")
        response = _Response(headers={"content-type": "image/png"}, chunks=[output.getvalue()])
        with patch("smartfetch.media._resolve_public_target", side_effect=_target):
            async with ingest_remote_media(
                "https://public.example/image", "image", transport=_Transport([response])
            ) as item:
                path = item.download.path
                self.assertEqual((item.metadata.width, item.metadata.height), (4, 5))
                self.assertTrue(path.exists())
        self.assertFalse(path.exists())

    async def test_http_source_is_rejected_before_transport(self):
        transport = _Transport([])
        with self.assertRaisesRegex(MediaFailure, "^invalid_source_url$"):
            async with media_download("http://public.example/media", "image", transport=transport):
                pass
        self.assertEqual(transport.urls, [])

    async def test_streams_to_restrictive_random_temp_and_cleans(self):
        response = _Response(headers={"content-type": "image/png", "content-length": "8"},
                             chunks=[b"\x89PNG", b"data"])
        transport = _Transport([response])
        with patch("smartfetch.media._resolve_public_target", side_effect=_target) as validate:
            async with media_download("https://public.example/a?secret=CANARY", "image", transport=transport) as item:
                path = item.path
                self.assertTrue(path.exists())
                self.assertNotIn("secret", path.name)
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(item.size_bytes, 8)
            self.assertFalse(path.exists())
        validate.assert_called_once()
        self.assertTrue(response.closed)

    async def test_redirects_are_each_validated_and_bounded(self):
        redirect = _Response(status=302, headers={"location": "https://next.example/media"})
        final = _Response(headers={"content-type": "application/pdf"}, chunks=[b"%PDF-1.7"])
        transport = _Transport([redirect, final])
        with patch("smartfetch.media._resolve_public_target", side_effect=_target) as validate:
            async with media_download("https://first.example/start", "pdf", transport=transport):
                pass
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(transport.urls, ["https://first.example/start", "https://next.example/media"])

    async def test_redirect_ssrf_rejection_stops_before_second_open(self):
        transport = _Transport([_Response(status=302, headers={"location": "https://blocked.invalid/media"})])
        with patch("smartfetch.media._resolve_public_target", side_effect=[
            _target("https://public.example/media"), MediaFailure("invalid_source_url")
        ]):
            with self.assertRaisesRegex(MediaFailure, "^invalid_source_url$") as caught:
                async with media_download("https://public.example/media", "video", transport=transport):
                    pass
        self.assertEqual(len(transport.urls), 1)
        self.assertNotIn("CANARY", str(caught.exception))

    async def test_sixth_redirect_is_rejected_without_opening_it(self):
        responses = [
            _Response(status=302, headers={"location": f"https://example.com/{index + 1}"})
            for index in range(6)
        ]
        transport = _Transport(responses)
        with patch("smartfetch.media._resolve_public_target", side_effect=_target):
            with self.assertRaisesRegex(MediaFailure, "^retrieval_failed$"):
                async with media_download("https://example.com/0", "pdf", transport=transport):
                    pass
        self.assertEqual(len(transport.urls), 6)

    async def test_declared_and_streamed_oversize_fail_and_clean(self):
        cap = MEDIA_LIMITS["image"].max_bytes
        for response in (
            _Response(headers={"content-type": "image/png", "content-length": str(cap + 1)}),
            _Response(headers={"content-type": "image/png"}, chunks=[b"x" * (cap + 1)]),
        ):
            with self.subTest(headers=response.headers):
                transport = _Transport([response])
                with tempfile.TemporaryDirectory() as directory, \
                     patch("smartfetch.media._resolve_public_target", side_effect=_target), \
                     patch("smartfetch.media.tempfile.tempdir", directory):
                    with self.assertRaisesRegex(MediaFailure, "^source_too_large$"):
                        async with media_download("https://example.com/media", "image", transport=transport):
                            pass
                    self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_false_content_type_fails_before_body(self):
        response = _Response(headers={"content-type": "text/html"}, chunks=[b"CANARY"])
        with patch("smartfetch.media._resolve_public_target", side_effect=_target):
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                async with media_download("https://example.com/media", "pdf", transport=_Transport([response])):
                    pass

    async def test_concurrent_downloads_use_distinct_paths(self):
        async def one(byte):
            transport = _Transport([_Response(headers={"content-type": "image/png"}, chunks=[byte])])
            async with media_download("https://example.com/media", "image", transport=transport) as item:
                await asyncio.sleep(0)
                return item.path
        with patch("smartfetch.media._resolve_public_target", side_effect=_target):
            first, second = await asyncio.gather(one(b"a"), one(b"b"))
        self.assertNotEqual(first, second)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    async def test_cleanup_failure_prevents_success(self):
        response = _Response(headers={"content-type": "image/png"}, chunks=[b"data"])
        path = None
        with patch("smartfetch.media._resolve_public_target", side_effect=_target):
            with self.assertRaisesRegex(MediaFailure, "^retrieval_failed$"):
                with patch("smartfetch.media._cleanup_workspace", return_value=False):
                    async with media_download(
                        "https://example.com/media", "image", transport=_Transport([response])
                    ) as item:
                        path = item.path
        self.assertIsNotNone(path)
        import shutil
        shutil.rmtree(path.parent)


class InspectionTests(unittest.TestCase):
    def _image(self, fmt="PNG", size=(8, 8), frames=1):
        output = io.BytesIO()
        images = [Image.new("RGB", size, "white" if index == 0 else "black") for index in range(frames)]
        images[0].save(output, format=fmt, save_all=frames > 1,
                       append_images=images[1:], duration=100, loop=0)
        return output.getvalue()

    def test_image_signature_pixels_and_single_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "random.bin"
            path.write_bytes(self._image())
            info = inspect_image(path, "image/png")
            self.assertEqual((info.width, info.height, info.frame_count), (8, 8, 1))
            path.write_bytes(self._image(fmt="WEBP", frames=2))
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                inspect_image(path, "image/webp")
            path.write_bytes(self._image(size=(5000, 4001)))
            with self.assertRaisesRegex(MediaFailure, "^source_too_large$"):
                inspect_image(path, "image/png")

    def test_image_mime_must_match_bytes_and_malformed_is_finite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "random.bin"
            path.write_bytes(self._image("JPEG"))
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                inspect_image(path, "image/png")
            path.write_bytes(b"CANARY-not-an-image")
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                inspect_image(path, "image/png")

    def test_pdf_page_boundary_encryption_and_malformed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "random.bin"
            writer = PdfWriter()
            for _ in range(20): writer.add_blank_page(width=72, height=72)
            with path.open("wb") as handle: writer.write(handle)
            self.assertEqual(inspect_pdf(path, "application/pdf").page_count, 20)
            writer.add_blank_page(width=72, height=72)
            with path.open("wb") as handle: writer.write(handle)
            with self.assertRaisesRegex(MediaFailure, "^source_too_large$"):
                inspect_pdf(path, "application/pdf")
            encrypted = PdfWriter(); encrypted.add_blank_page(width=72, height=72); encrypted.encrypt("secret")
            with path.open("wb") as handle: encrypted.write(handle)
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                inspect_pdf(path, "application/pdf")
            path.write_bytes(b"%PDF-CANARY")
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$"):
                inspect_pdf(path, "application/pdf")

    def test_ffprobe_duration_and_format_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "random.bin"; path.write_bytes(b"media")
            async def valid(*args, **kwargs):
                return {"format": {"format_name": "mp3", "duration": "1800"},
                        "streams": [{"codec_type": "audio", "codec_name": "mp3"}]}
            info = asyncio.run(inspect_timed_media(path, "audio", "audio/mpeg", runner=valid))
            self.assertEqual(info.duration_seconds, 1800)
            for duration in ("NaN", "Infinity", "-1", None, "1800.1"):
                async def invalid(*args, _duration=duration, **kwargs):
                    return {"format": {"format_name": "mp3", "duration": _duration}, "streams": []}
                with self.subTest(duration=duration), self.assertRaises(MediaFailure):
                    asyncio.run(inspect_timed_media(path, "audio", "audio/mpeg", runner=invalid))

    def test_ffprobe_runner_failure_is_finite_and_private(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("CANARY /private/temp/path?token=secret")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-CANARY"; path.write_bytes(b"media")
            with self.assertRaisesRegex(MediaFailure, "^retrieval_failed$") as caught:
                asyncio.run(inspect_timed_media(path, "video", "video/mp4", runner=broken))
        self.assertEqual(str(caught.exception), "retrieval_failed")


class FfprobeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    class Writer:
        def write(self, value):
            return None
        async def drain(self):
            return None
        def close(self):
            return None
        async def wait_closed(self):
            return None

    class Process:
        def __init__(self, stdout, stderr=b"", returncode=0):
            self.stdin = FfprobeBoundaryTests.Writer()
            self.stdout = asyncio.StreamReader(); self.stdout.feed_data(stdout); self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader(); self.stderr.feed_data(stderr); self.stderr.feed_eof()
            self.returncode = None
            self._code = returncode
            self.killed = False
        async def wait(self):
            self.returncode = self._code
            return self._code
        def kill(self):
            self.killed = True; self.returncode = -9

    async def test_ffprobe_uses_direct_restricted_bounded_invocation(self):
        payload = json.dumps({"format": {"format_name": "mp3", "duration": "1"}, "streams": []}).encode()
        process = self.Process(payload)
        captured = {}
        async def create(*args, **kwargs):
            captured["args"] = args; captured["kwargs"] = kwargs; return process
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-file.bin"
            path.write_bytes(b"media")
            with patch("smartfetch.media.shutil.which", return_value="/usr/bin/ffprobe"), \
                 patch("smartfetch.media.asyncio.create_subprocess_exec", side_effect=create):
                result = await run_ffprobe(path)
        self.assertEqual(result["format"]["duration"], "1")
        self.assertEqual(captured["args"][0], "/usr/bin/ffprobe")
        self.assertIn("pipe", captured["args"])
        self.assertIn("pipe:0", captured["args"])
        self.assertNotIn("shell", captured["kwargs"])
        self.assertEqual(captured["kwargs"]["env"], {"LANG": "C", "LC_ALL": "C"})

    async def test_ffprobe_rejects_oversized_output_without_leaking_it(self):
        process = self.Process(b"CANARY" * 20_000)
        with patch("smartfetch.media.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch("smartfetch.media.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaisesRegex(MediaFailure, "^unsupported_media_type$") as caught:
                await run_ffprobe(Path("private-file.bin"))
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertTrue(process.killed)

    async def test_ffprobe_timeout_kills_process(self):
        class HungProcess(self.Process):
            async def wait(self):
                if self.killed:
                    self.returncode = -9
                    return -9
                await asyncio.Future()
        process = HungProcess(b"{}")
        with patch("smartfetch.media.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch("smartfetch.media.asyncio.create_subprocess_exec", return_value=process), \
             patch("smartfetch.media.FFPROBE_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(MediaFailure, "^retrieval_timeout$"):
                await run_ffprobe(Path("private-file.bin"))
        self.assertTrue(process.killed)


class WebpageModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_and_always_reuse_existing_engine(self):
        automatic = await retrieve_webpage("https://example.com", "auto", fetcher=_echo_fetch)
        forced = await retrieve_webpage("https://example.com", "always", fetcher=_echo_fetch)
        self.assertEqual((automatic["force_browser"], automatic["max_chars"]), (False, 50_000))
        self.assertEqual((forced["force_browser"], forced["max_chars"]), (True, 50_000))
        with self.assertRaisesRegex(MediaFailure, "^invalid_request$"):
            await retrieve_webpage("https://example.com", "never", fetcher=_echo_fetch)


if __name__ == "__main__":
    unittest.main()
