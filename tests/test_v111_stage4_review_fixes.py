import asyncio
import json
import unittest
from unittest.mock import patch

from starlette.requests import Request

from smartfetch import activity


class BoundedASGIBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_declared_oversize_is_rejected_without_reading(self):
        from smartfetch.server import _read_bounded_request_body

        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            return {"type": "http.request", "body": b"x", "more_body": False}

        request = Request(
            {
                "type": "http", "method": "POST", "path": "/",
                "headers": [(b"content-length", b"65")],
            },
            receive,
        )
        with self.assertRaisesRegex(ValueError, "invalid_request"):
            await _read_bounded_request_body(request, maximum=64)
        self.assertEqual(calls, 0)

    async def test_false_content_length_does_not_control_accepted_body(self):
        from smartfetch.server import _read_bounded_request_body

        messages = iter((
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ))

        async def receive():
            return next(messages)

        request = Request(
            {
                "type": "http", "method": "POST", "path": "/",
                "headers": [(b"content-length", b"1")],
            },
            receive,
        )
        self.assertEqual(
            await _read_bounded_request_body(request, maximum=6),
            b"abcdef",
        )

    async def test_understated_length_stops_at_cap_plus_one(self):
        from smartfetch.server import _read_bounded_request_body

        consumed = 0
        chunks = [b"a" * 4 for _ in range(20)]

        async def receive():
            nonlocal consumed
            chunk = chunks[consumed]
            consumed += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": consumed < len(chunks),
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/search-and-extract/results",
                "headers": [(b"content-length", b"1")],
            },
            receive,
        )
        with self.assertRaisesRegex(ValueError, "invalid_request"):
            await _read_bounded_request_body(request, maximum=16)
        self.assertEqual(consumed, 5)

    async def test_missing_length_chunked_body_is_bounded_and_replayed(self):
        from smartfetch.server import _read_bounded_request_body

        messages = iter((
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b'{"query":', "more_body": True},
            {"type": "http.request", "body": b'"valid"}', "more_body": False},
        ))

        async def receive():
            return next(messages)

        request = Request(
            {"type": "http", "method": "POST", "path": "/", "headers": []},
            receive,
        )
        body = await _read_bounded_request_body(request, maximum=64)
        self.assertEqual(body, b'{"query":"valid"}')
        self.assertEqual(await request.body(), body)

    async def test_disconnect_and_malformed_messages_fail_finitely(self):
        from smartfetch.server import _read_bounded_request_body

        for message in (
            {"type": "http.disconnect"},
            {"type": "websocket.receive", "body": b"x"},
            {"type": "http.request", "body": "not-bytes", "more_body": False},
            {"type": "http.request", "body": b"x", "more_body": "yes"},
        ):
            async def receive(current=message):
                return current

            request = Request(
                {"type": "http", "method": "POST", "path": "/", "headers": []},
                receive,
            )
            with self.subTest(message=message["type"]):
                with self.assertRaisesRegex(ValueError, "invalid_request"):
                    await _read_bounded_request_body(request, maximum=64)


class VariantDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_variant_deadlines(self):
        from smartfetch.v111_service import V111_DEADLINES_SECONDS

        self.assertEqual(dict(V111_DEADLINES_SECONDS), {
            "results": 15.0,
            "answer": 40.0,
            "structured": 60.0,
            "webpage": 60.0,
            "image": 60.0,
            "pdf": 60.0,
            "audio": 90.0,
            "video": 120.0,
        })

    async def test_timeout_cancels_and_waits_for_cleanup(self):
        from smartfetch.v111_service import run_v111_deadline, V111ExecutionError

        cleanup_finished = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleanup_finished.set()

        with patch("smartfetch.v111_service._deadline_for_variant", return_value=0.01):
            with self.assertRaises(V111ExecutionError) as caught:
                await run_v111_deadline("results", operation())
        self.assertEqual(caught.exception.code, "provider_timeout")
        self.assertTrue(cleanup_finished.is_set())

    async def test_total_workflow_deadline_is_not_reset_per_step(self):
        from smartfetch.v111_service import run_v111_deadline, V111ExecutionError

        async def operation():
            await asyncio.sleep(0.015)
            await asyncio.sleep(0.015)

        with patch("smartfetch.v111_service._deadline_for_variant", return_value=0.02):
            with self.assertRaises(V111ExecutionError):
                await run_v111_deadline("answer", operation())

    async def test_every_variant_class_cancels_a_hanging_operation(self):
        from smartfetch.v111_service import (
            V111_DEADLINES_SECONDS,
            V111ExecutionError,
            run_v111_deadline,
        )

        for variant in V111_DEADLINES_SECONDS:
            cancelled = asyncio.Event()

            async def operation():
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            with self.subTest(variant=variant):
                with patch(
                    "smartfetch.v111_service._deadline_for_variant",
                    return_value=0.001,
                ):
                    with self.assertRaises(V111ExecutionError):
                        await run_v111_deadline(variant, operation())
                self.assertTrue(cancelled.is_set())


class V111ActivityTests(unittest.TestCase):
    def test_fields_are_allowlisted_bounded_and_legacy_events_unchanged(self):
        from smartfetch.activity import emit_v111_activity

        records = []
        with patch.object(activity._LOGGER, "info", side_effect=records.append):
            emit_v111_activity(
                "provider_completed",
                capability="extract_structured_data",
                variant="video",
                provider="gemini",
                model_route="flash_lite",
                source_count=1,
                input_tokens=50,
                output_tokens=20,
                provider_cost_micro_usd=123,
                query="CANARY-QUERY",
                schema="CANARY-SCHEMA",
            )
            activity.emit_activity(
                "tool_started",
                capability="search_and_extract",
                variant="results",
            )
        first, second = (json.loads(item) for item in records)
        self.assertEqual(first["capability"], "extract_structured_data")
        self.assertEqual(first["variant"], "video")
        self.assertEqual(first["provider"], "gemini")
        self.assertEqual(first["model_route"], "flash_lite")
        self.assertEqual(first["provider_cost_micro_usd"], 123)
        self.assertNotIn("query", first)
        self.assertNotIn("schema", first)
        self.assertNotIn("capability", second)
        self.assertNotIn("variant", second)
        serialized = "\n".join(records)
        self.assertNotIn("CANARY", serialized)

    def test_missing_usage_is_omitted_and_logging_is_total(self):
        from smartfetch.activity import emit_v111_activity

        records = []
        with patch.object(activity._LOGGER, "info", side_effect=records.append):
            emit_v111_activity(
                "provider_completed",
                capability="search_and_extract",
                variant="results",
                provider="exa",
            )
            emit_v111_activity(
                "provider_completed",
                capability=["hostile"],
                variant="results",
                provider="exa",
                input_tokens=True,
            )
        first = json.loads(records[0])
        self.assertNotIn("input_tokens", first)
        self.assertNotIn("provider_cost_micro_usd", first)

    def test_same_capability_media_variants_remain_distinguishable(self):
        from smartfetch.activity import emit_v111_activity

        records = []
        with patch.object(activity._LOGGER, "info", side_effect=records.append):
            for variant in ("webpage", "image", "pdf", "audio", "video"):
                emit_v111_activity(
                    "tool_started",
                    capability="extract_structured_data",
                    variant=variant,
                )
        self.assertEqual(
            [json.loads(item)["variant"] for item in records],
            ["webpage", "image", "pdf", "audio", "video"],
        )


if __name__ == "__main__":
    unittest.main()
