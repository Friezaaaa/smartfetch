import asyncio
import http.server
import io
import json
import logging
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

import smartfetch.media as media


def _blocking_fetcher(marker, *, force_browser, max_chars):
    Path(marker).write_text("started", encoding="utf-8")
    time.sleep(10)
    Path(marker + ".finished").write_text("finished", encoding="utf-8")
    return {"success": True}


def _crashing_fetcher(url, *, force_browser, max_chars):
    raise RuntimeError("CANARY-raw-worker-error")


def _oversized_fetcher(url, *, force_browser, max_chars):
    return {"success": True, "content": "x" * (media.WORKER_RESULT_MAX_BYTES + 1)}


def _success_fetcher(url, *, force_browser, max_chars):
    return {"success": True, "force_browser": force_browser, "max_chars": max_chars}


class HttpxStreamingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_httpx_response_streams_with_aiter_bytes(self):
        async def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"bounded-media",
            )

        transport = media.HttpxStreamingTransport(transport=httpx.MockTransport(handler))
        target = media.PinnedTarget(
            url="https://public.example/image",
            hostname="public.example",
            port=443,
            addresses=("203.0.113.10",),
        )
        with patch("smartfetch.media._resolve_public_target", return_value=target):
            async with media.media_download(
                target.url, "image", transport=transport
            ) as downloaded:
                self.assertEqual(downloaded.path.read_bytes(), b"bounded-media")

    async def test_pinned_transport_preserves_host_and_tls_name(self):
        captured = {}

        async def handler(request):
            captured["url"] = str(request.url)
            captured["host"] = request.headers["host"]
            captured["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")

        transport = media.HttpxStreamingTransport(transport=httpx.MockTransport(handler))
        target = media.PinnedTarget(
            url="https://public.example/private/path?secret=CANARY",
            hostname="public.example",
            port=443,
            addresses=("203.0.113.10", "203.0.113.11"),
        )
        response = await transport.open(target, connect_timeout=5.0, read_timeout=15.0)
        await response.aclose()
        await transport.aclose()
        self.assertIn("203.0.113.10", captured["url"])
        self.assertEqual(captured["host"], "public.example")
        self.assertEqual(captured["sni"], "public.example")

    async def test_pinned_transport_never_logs_url_or_query_canaries(self):
        async def handler(request):
            return httpx.Response(200, content=b"x")

        output = io.StringIO()
        handler_output = logging.StreamHandler(output)
        logger = logging.getLogger("httpx")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler_output)
        try:
            transport = media.HttpxStreamingTransport(transport=httpx.MockTransport(handler))
            target = media.PinnedTarget(
                url="https://private-host.example/private-path?secret=PRIVATE-QUERY-CANARY",
                hostname="private-host.example",
                port=443,
                addresses=("203.0.113.10",),
            )
            response = await transport.open(target, connect_timeout=5.0, read_timeout=15.0)
            await response.aclose()
            await transport.aclose()
        finally:
            logger.removeHandler(handler_output)
            logger.setLevel(old_level)
        logged = output.getvalue()
        self.assertNotIn("private-host.example", logged)
        self.assertNotIn("private-path", logged)
        self.assertNotIn("PRIVATE-QUERY-CANARY", logged)

    async def test_rebinding_and_each_redirect_are_resolved_and_repinned(self):
        first = media.PinnedTarget(
            url="https://first.example/start",
            hostname="first.example",
            port=443,
            addresses=("203.0.113.10",),
        )
        second = media.PinnedTarget(
            url="https://next.example/media",
            hostname="next.example",
            port=443,
            addresses=("203.0.113.11",),
        )

        class Response:
            def __init__(self, status, headers, chunks=()):
                self.status_code = status
                self.headers = headers
                self._chunks = chunks

            async def aiter_bytes(self):
                for chunk in self._chunks:
                    yield chunk

            async def aclose(self):
                return None

        class Transport:
            def __init__(self):
                self.targets = []
                self.responses = [
                    Response(302, {"location": "https://next.example/media"}),
                    Response(200, {"content-type": "image/png"}, (b"x",)),
                ]

            async def open(self, target, **kwargs):
                self.targets.append(target)
                return self.responses.pop(0)

        transport = Transport()
        with patch("smartfetch.media._resolve_public_target", side_effect=[first, second]) as resolve:
            async with media.media_download(first.url, "image", transport=transport):
                pass
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(transport.targets, [first, second])

        public = [(2, 1, 6, "", ("93.184.216.34", 443))]
        private = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch(
            "smartfetch.media.socket.getaddrinfo", side_effect=[public, private]
        ), self.assertRaisesRegex(media.MediaFailure, "^invalid_source_url$"):
            media._resolve_public_target("https://public.example/media")

    async def test_all_resolved_addresses_are_pinned_and_transport_falls_back(self):
        infos = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ]
        with patch("smartfetch.media.socket.getaddrinfo", side_effect=[infos, infos]):
            target = media._resolve_public_target("https://public.example/media")
        self.assertEqual(target.addresses, ("93.184.216.34", "1.1.1.1"))
        seen = []

        async def handler(request):
            seen.append(request.url.host)
            if len(seen) == 1:
                raise httpx.ConnectError("CANARY", request=request)
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")

        transport = media.HttpxStreamingTransport(transport=httpx.MockTransport(handler))
        response = await transport.open(target, connect_timeout=5.0, read_timeout=15.0)
        await response.aclose()
        await transport.aclose()
        self.assertEqual(seen, ["93.184.216.34", "1.1.1.1"])


class KillableWorkTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_capacity_policy(self):
        self.assertEqual(media.DOWNLOAD_CONCURRENCY, 8)
        self.assertEqual(media.MEDIA_PROCESSING_CONCURRENCY, 2)
        self.assertEqual(media.CAPACITY_WAIT_SECONDS, 5.0)

    async def test_webpage_timeout_terminates_worker_before_return(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = str(Path(directory) / "worker")
            with patch("smartfetch.media.WEBPAGE_TIMEOUT_SECONDS", 4.0):
                with self.assertRaisesRegex(media.MediaFailure, "^retrieval_timeout$"):
                    await media.retrieve_webpage(marker, "auto", fetcher=_blocking_fetcher)
            self.assertTrue(Path(marker).exists())
            await asyncio.sleep(0.3)
            self.assertFalse(Path(marker + ".finished").exists())
            self.assertFalse(any(
                child.name.startswith("smartfetch-media-worker")
                for child in multiprocessing.active_children()
            ))

    async def test_webpage_cancellation_terminates_worker_before_return(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = str(Path(directory) / "worker")
            task = asyncio.create_task(
                media.retrieve_webpage(marker, "always", fetcher=_blocking_fetcher)
            )
            for _ in range(300):
                if any(
                    child.name.startswith("smartfetch-media-worker")
                    for child in multiprocessing.active_children()
                ):
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(any(
                child.name.startswith("smartfetch-media-worker")
                for child in multiprocessing.active_children()
            ))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.2)
            self.assertFalse(Path(marker + ".finished").exists())
            self.assertFalse(any(
                child.name.startswith("smartfetch-media-worker")
                for child in multiprocessing.active_children()
            ))

    async def test_worker_crash_and_oversized_ipc_are_finite(self):
        for fetcher in (_crashing_fetcher, _oversized_fetcher):
            with self.subTest(fetcher=fetcher.__name__):
                with self.assertRaisesRegex(media.MediaFailure, "^retrieval_failed$") as caught:
                    await media.retrieve_webpage("https://example.com", "auto", fetcher=fetcher)
                self.assertNotIn("CANARY", str(caught.exception))

    async def test_media_capacity_is_independent_and_released_after_termination(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("smartfetch.media.MEDIA_PROCESSING_CONCURRENCY", 1), \
             patch("smartfetch.media.CAPACITY_WAIT_SECONDS", 0.05):
            media._reset_capacity_limiters_for_tests()
            marker = str(Path(directory) / "worker")
            first = asyncio.create_task(
                media.retrieve_webpage(marker, "auto", fetcher=_blocking_fetcher)
            )
            for _ in range(300):
                if any(child.name.startswith("smartfetch-media-worker") for child in multiprocessing.active_children()):
                    break
                await asyncio.sleep(0.01)
            with self.assertRaisesRegex(media.MediaFailure, "^capacity_unavailable$"):
                await media.retrieve_webpage("https://example.com", "auto", fetcher=_oversized_fetcher)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            result = await media.retrieve_webpage("https://example.com", "auto", fetcher=_success_fetcher)
            self.assertIs(type(result), dict)
            media._reset_capacity_limiters_for_tests()


class CleanupAndCapacityTests(unittest.IsolatedAsyncioTestCase):
    class Response:
        def __init__(self, chunks):
            self.status_code = 200
            self.headers = {"content-type": "image/png"}
            self.chunks = chunks

        async def aiter_bytes(self):
            for chunk in self.chunks:
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk

        async def aclose(self):
            return None

    class Transport:
        def __init__(self, response):
            self.response = response

        async def open(self, target, **kwargs):
            return self.response

    async def test_failed_download_cleanup_is_enforced(self):
        target = media.PinnedTarget("https://example.com/x", "example.com", 443, ("203.0.113.10",))
        response = self.Response([b"x" * (media.MEDIA_LIMITS["image"].max_bytes + 1)])
        with patch("smartfetch.media._resolve_public_target", return_value=target), \
             patch("smartfetch.media._cleanup_workspace", return_value=False):
            with self.assertRaisesRegex(media.MediaFailure, "^retrieval_failed$"):
                async with media.media_download(target.url, "image", transport=self.Transport(response)):
                    pass

    async def test_download_capacity_times_out_and_releases(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        target = media.PinnedTarget("https://example.com/x", "example.com", 443, ("203.0.113.10",))

        class BlockingResponse(self.Response):
            async def aiter_bytes(inner_self):
                entered.set()
                await release.wait()
                yield b"x"

        with patch("smartfetch.media._resolve_public_target", return_value=target), \
             patch("smartfetch.media.DOWNLOAD_CONCURRENCY", 1), \
             patch("smartfetch.media.CAPACITY_WAIT_SECONDS", 0.05):
            media._reset_capacity_limiters_for_tests()
            async def first_download():
                async with media.media_download(
                    target.url, "image", transport=self.Transport(BlockingResponse([]))
                ) as item:
                    return item.path
            first = asyncio.create_task(first_download())
            await entered.wait()
            with self.assertRaisesRegex(media.MediaFailure, "^capacity_unavailable$"):
                async with media.media_download(target.url, "image", transport=self.Transport(self.Response([b"x"]))):
                    pass
            release.set()
            path = await first
            self.assertFalse(path.exists())
            media._reset_capacity_limiters_for_tests()

    async def test_cancelled_download_closes_and_removes_workspace_before_capacity_release(self):
        entered = asyncio.Event()
        target = media.PinnedTarget("https://example.com/x", "example.com", 443, ("93.184.216.34",))

        class BlockingResponse(self.Response):
            def __init__(inner_self):
                super().__init__([])
                inner_self.closed = False

            async def aiter_bytes(inner_self):
                entered.set()
                await asyncio.Future()
                yield b"unused"

            async def aclose(inner_self):
                inner_self.closed = True

        response = BlockingResponse()
        with tempfile.TemporaryDirectory() as directory, \
             patch("smartfetch.media.tempfile.tempdir", directory), \
             patch("smartfetch.media._resolve_public_target", return_value=target):
            task = asyncio.create_task(
                media.media_download(target.url, "image", transport=self.Transport(response)).__aenter__()
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(response.closed)
            self.assertEqual(list(Path(directory).iterdir()), [])


class TimedMetadataPolicyTests(unittest.TestCase):
    def _inspect(self, *, source_type, mime, value):
        async def runner(*args, **kwargs):
            return value
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.bin"
            path.write_bytes(b"media")
            return asyncio.run(media.inspect_timed_media(path, source_type, mime, runner=runner))

    def test_audio_stream_and_codec_allowlist(self):
        value = {
            "format": {"format_name": "wav", "duration": "1"},
            "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le", "duration": "1"}],
        }
        self.assertEqual(self._inspect(source_type="audio", mime="audio/wav", value=value).duration_seconds, 1)
        for streams in (
            value["streams"] + [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
            [{"codec_type": "audio", "codec_name": "flac"}],
            value["streams"] + [{"codec_type": "subtitle", "codec_name": "text"}],
        ):
            with self.subTest(streams=streams), self.assertRaisesRegex(media.MediaFailure, "^unsupported_media_type$"):
                self._inspect(source_type="audio", mime="audio/wav", value={**value, "streams": streams})

    def test_video_stream_policy_and_maximum_duration(self):
        value = {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "100"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "duration": "599.9"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
        self.assertEqual(self._inspect(source_type="video", mime="video/mp4", value=value).duration_seconds, 599.9)
        value["streams"][0]["duration"] = "600.1"
        with self.assertRaisesRegex(media.MediaFailure, "^source_too_large$"):
            self._inspect(source_type="video", mime="video/mp4", value=value)
        value["streams"][0]["duration"] = "NaN"
        with self.assertRaisesRegex(media.MediaFailure, "^unsupported_media_type$"):
            self._inspect(source_type="video", mime="video/mp4", value=value)

    def test_exact_codec_matrices_and_duration_presence(self):
        expected_audio = {
            "audio/mpeg": {"mp3"},
            "audio/wav": {"pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le", "pcm_alaw", "pcm_mulaw"},
            "audio/x-wav": {"pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le", "pcm_alaw", "pcm_mulaw"},
            "audio/mp4": {"aac"},
            "audio/aac": {"aac"},
            "audio/ogg": {"vorbis", "opus"},
            "audio/flac": {"flac"},
        }
        self.assertEqual({key: set(value) for key, value in media._AUDIO_CODECS.items()}, expected_audio)
        self.assertEqual(set(media._VIDEO_CODECS["video/mp4"]), {"h264", "hevc", "av1"})
        self.assertEqual(set(media._VIDEO_CODECS["video/quicktime"]), {"h264", "hevc", "av1"})
        self.assertEqual(set(media._VIDEO_CODECS["video/webm"]), {"vp8", "vp9", "av1"})
        self.assertEqual(set(media._VIDEO_AUDIO_CODECS), {"aac", "mp3", "opus", "vorbis", "pcm_s16le"})

        missing = {
            "format": {"format_name": "mp3"},
            "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
        }
        with self.assertRaisesRegex(media.MediaFailure, "^unsupported_media_type$"):
            self._inspect(source_type="audio", mime="audio/mpeg", value=missing)
        stream_only = {
            "format": {"format_name": "mp3"},
            "streams": [{"codec_type": "audio", "codec_name": "mp3", "duration": "1.25"}],
        }
        self.assertEqual(self._inspect(source_type="audio", mime="audio/mpeg", value=stream_only).duration_seconds, 1.25)


class FfprobePipeTests(unittest.IsolatedAsyncioTestCase):
    class Writer:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, value):
            self.data.extend(value)

        async def drain(self):
            return None

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    class Process:
        def __init__(self, output):
            self.stdin = FfprobePipeTests.Writer()
            self.stdout = asyncio.StreamReader(); self.stdout.feed_data(output); self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader(); self.stderr.feed_eof()
            self.returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def test_ffprobe_receives_bounded_pipe_only_input(self):
        output = json.dumps({"format": {}, "streams": []}).encode()
        process = self.Process(output)
        captured = {}

        async def create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return process

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.bin"
            path.write_bytes(b"validated-bytes")
            with patch("smartfetch.media.shutil.which", return_value="ffprobe"), \
                 patch("smartfetch.media.asyncio.create_subprocess_exec", side_effect=create):
                await media.run_ffprobe(path)
        self.assertIn("pipe", captured["args"])
        self.assertNotIn("file,pipe", captured["args"])
        self.assertIn("pipe:0", captured["args"])
        self.assertEqual(bytes(process.stdin.data), b"validated-bytes")


class ImmutableDecisionAndDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_checks_raw_cap_and_owns_nested_payload(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        decision = media.choose_delivery(
            source_type="image",
            mime_type="image/png",
            content=b"x",
            schema=schema,
            instructions=None,
            prompt="p",
        )
        schema["properties"]["x"]["type"] = "number"
        first = decision.payload
        self.assertEqual(first["schema"]["properties"]["x"]["type"], "string")
        first["schema"]["properties"]["x"]["type"] = "boolean"
        self.assertEqual(decision.payload["schema"]["properties"]["x"]["type"], "string")
        with self.assertRaisesRegex(media.MediaFailure, "^source_too_large$"):
            media.choose_delivery(
                source_type="image",
                mime_type="image/png",
                content=b"x" * (media.MEDIA_LIMITS["image"].max_bytes + 1),
                schema={},
                instructions=None,
                prompt="p",
            )

    async def test_delete_false_or_exception_always_confirms_absence(self):
        class Client:
            def __init__(self, result):
                self.result = result
                self.calls = []

            async def delete(self, file_id):
                self.calls.append("delete")
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

            async def get_status(self, file_id):
                self.calls.append("get")
                return "not_found"

        for result in (False, RuntimeError("CANARY-provider-path")):
            client = Client(result)
            await media._delete_provider_file(client, "private-id")
            self.assertEqual(client.calls, ["delete", "get"])

    def test_media_limits_are_not_mutable(self):
        with self.assertRaises(TypeError):
            media.MEDIA_LIMITS["image"] = media.MEDIA_LIMITS["pdf"]


if __name__ == "__main__":
    unittest.main()
