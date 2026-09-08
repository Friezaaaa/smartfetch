"""Private, bounded media-ingestion primitives for future V1.11 orchestration.

This module exposes no route or tool. Network, subprocess, retrieval, and provider-file
boundaries are injectable so callers can keep each operation request-local.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import base64
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image
from pypdf import PdfReader

from .core import smart_fetch
from .security import validate_public_url


MiB = 1024 * 1024
INLINE_REQUEST_MAX_BYTES = 18_000_000
MAX_REDIRECTS = 5
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
DOWNLOAD_TIMEOUT_SECONDS = 25.0
FFPROBE_TIMEOUT_SECONDS = 5.0
FFPROBE_OUTPUT_MAX_BYTES = 65_536
PROVIDER_FILE_TIMEOUT_SECONDS = 10.0

FAILURE_CODES = frozenset({
    "invalid_request",
    "invalid_source_url",
    "source_too_large",
    "unsupported_media_type",
    "retrieval_failed",
    "retrieval_timeout",
    "provider_cleanup_failed",
    "provider_unavailable",
})


class MediaFailure(Exception):
    """Finite public-safe internal failure; never includes raw provider/source details."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in FAILURE_CODES:
            raise ValueError("invalid media failure code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MediaLimit:
    max_bytes: int
    allowed_mime_types: tuple[str, ...]
    max_pixels: int | None = None
    max_frames: int | None = None
    max_pages: int | None = None
    max_duration_seconds: int | None = None


MEDIA_LIMITS = {
    "image": MediaLimit(
        10 * MiB,
        ("image/jpeg", "image/png", "image/webp"),
        max_pixels=20_000_000,
        max_frames=1,
    ),
    "pdf": MediaLimit(20 * MiB, ("application/pdf",), max_pages=20),
    "audio": MediaLimit(
        25 * MiB,
        ("audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/aac", "audio/ogg", "audio/flac"),
        max_duration_seconds=30 * 60,
    ),
    "video": MediaLimit(
        50 * MiB,
        ("video/mp4", "video/webm", "video/quicktime"),
        max_duration_seconds=10 * 60,
    ),
}


def _limit(source_type: str) -> MediaLimit:
    if type(source_type) is not str or source_type not in MEDIA_LIMITS:
        raise MediaFailure("invalid_request")
    return MEDIA_LIMITS[source_type]


def _content_type(value: Any) -> str:
    if type(value) is not str:
        raise MediaFailure("unsupported_media_type")
    return value.split(";", 1)[0].strip().lower()


def _require_file_size(path: Path, source_type: str) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        raise MediaFailure("retrieval_failed") from None
    if size <= 0:
        raise MediaFailure("unsupported_media_type")
    if size > _limit(source_type).max_bytes:
        raise MediaFailure("source_too_large")


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path = field(repr=False)
    source_type: str
    mime_type: str
    size_bytes: int


class StreamingResponse(Protocol):
    status_code: int
    headers: Any

    def iter_bytes(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...


class StreamingTransport(Protocol):
    async def open(
        self, url: str, *, connect_timeout: float, read_timeout: float
    ) -> StreamingResponse: ...


class HttpxStreamingTransport:
    """No-proxy, no-auto-redirect transport for explicitly validated public URLs."""

    __slots__ = ("_client",)

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    async def open(self, url: str, *, connect_timeout: float, read_timeout: float) -> httpx.Response:
        request = self._client.build_request(
            "GET", url,
            headers={"Accept-Encoding": "identity"},
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
        )
        return await self._client.send(
            request,
            stream=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _safe_unlink(path: Path | None) -> bool:
    if path is None:
        return True
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


async def _download(
    url: str,
    source_type: str,
    transport: StreamingTransport,
) -> DownloadedMedia:
    limit = _limit(source_type)
    current = url
    path: Path | None = None
    response: StreamingResponse | None = None
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                if urlsplit(current).scheme.lower() != "https":
                    raise MediaFailure("invalid_source_url")
            except MediaFailure:
                raise
            except Exception:
                raise MediaFailure("invalid_source_url") from None
            try:
                current = validate_public_url(current)
            except Exception:
                raise MediaFailure("invalid_source_url") from None
            try:
                response = await transport.open(
                    current,
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                    read_timeout=READ_TIMEOUT_SECONDS,
                )
            except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
                raise MediaFailure("retrieval_timeout") from None
            except Exception:
                raise MediaFailure("retrieval_failed") from None
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                await response.aclose()
                response = None
                if type(location) is not str or not location or redirect_count == MAX_REDIRECTS:
                    raise MediaFailure("retrieval_failed")
                current = urljoin(current, location)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise MediaFailure("retrieval_failed")

            mime_type = _content_type(response.headers.get("content-type"))
            if mime_type not in limit.allowed_mime_types:
                raise MediaFailure("unsupported_media_type")
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except (TypeError, ValueError, OverflowError):
                    raise MediaFailure("retrieval_failed") from None
                if declared_size < 0:
                    raise MediaFailure("retrieval_failed")
                if declared_size > limit.max_bytes:
                    raise MediaFailure("source_too_large")

            descriptor, name = tempfile.mkstemp(prefix="smartfetch-media-", suffix=".bin")
            path = Path(name)
            try:
                os.chmod(path, 0o600)
                size = 0
                with os.fdopen(descriptor, "wb") as output:
                    async for chunk in response.iter_bytes():
                        if type(chunk) is not bytes:
                            raise MediaFailure("retrieval_failed")
                        size += len(chunk)
                        if size > limit.max_bytes:
                            raise MediaFailure("source_too_large")
                        output.write(chunk)
                if size == 0:
                    raise MediaFailure("unsupported_media_type")
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            return DownloadedMedia(path, source_type, mime_type, size)
        raise MediaFailure("retrieval_failed")
    except MediaFailure:
        _safe_unlink(path)
        raise
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
        _safe_unlink(path)
        raise MediaFailure("retrieval_timeout") from None
    except asyncio.CancelledError:
        _safe_unlink(path)
        raise
    except Exception:
        _safe_unlink(path)
        raise MediaFailure("retrieval_failed") from None
    finally:
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                pass


@asynccontextmanager
async def media_download(
    url: str,
    source_type: str,
    *,
    transport: StreamingTransport | None = None,
) -> AsyncIterator[DownloadedMedia]:
    owned_transport = transport is None
    active_transport = transport or HttpxStreamingTransport()
    item: DownloadedMedia | None = None
    try:
        item = await asyncio.wait_for(
            _download(url, source_type, active_transport),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        yield item
    except asyncio.TimeoutError:
        raise MediaFailure("retrieval_timeout") from None
    finally:
        cleanup_ok = _safe_unlink(item.path if item else None)
        if owned_transport:
            try:
                await active_transport.aclose()  # type: ignore[attr-defined]
            except Exception:
                pass
        if not cleanup_ok:
            raise MediaFailure("retrieval_failed")


@dataclass(frozen=True, slots=True)
class ImageInfo:
    width: int
    height: int
    frame_count: int
    format: str


_IMAGE_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def inspect_image(path: Path, declared_mime: str) -> ImageInfo:
    _require_file_size(path, "image")
    try:
        with Image.open(path) as image:
            image_format = image.format
            if type(image_format) is not str or image_format not in _IMAGE_FORMAT_MIME:
                raise MediaFailure("unsupported_media_type")
            if _IMAGE_FORMAT_MIME[image_format] != _content_type(declared_mime):
                raise MediaFailure("unsupported_media_type")
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1))
            if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
                raise MediaFailure("unsupported_media_type")
            if frames != 1:
                raise MediaFailure("unsupported_media_type")
            if width * height > MEDIA_LIMITS["image"].max_pixels:
                raise MediaFailure("source_too_large")
            image.seek(0)
            image.load()
            return ImageInfo(width, height, frames, image_format)
    except MediaFailure:
        raise
    except Exception:
        raise MediaFailure("unsupported_media_type") from None


@dataclass(frozen=True, slots=True)
class PdfInfo:
    page_count: int


def inspect_pdf(path: Path, declared_mime: str) -> PdfInfo:
    _require_file_size(path, "pdf")
    if _content_type(declared_mime) != "application/pdf":
        raise MediaFailure("unsupported_media_type")
    try:
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise MediaFailure("unsupported_media_type")
            source.seek(0)
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise MediaFailure("unsupported_media_type")
            page_count = len(reader.pages)
            if page_count < 1:
                raise MediaFailure("unsupported_media_type")
            if page_count > MEDIA_LIMITS["pdf"].max_pages:
                raise MediaFailure("source_too_large")
            return PdfInfo(page_count)
    except MediaFailure:
        raise
    except Exception:
        raise MediaFailure("unsupported_media_type") from None


@dataclass(frozen=True, slots=True)
class TimedMediaInfo:
    duration_seconds: float
    format_name: str


MediaMetadata = ImageInfo | PdfInfo | TimedMediaInfo


@dataclass(frozen=True, slots=True)
class IngestedMedia:
    download: DownloadedMedia = field(repr=False)
    metadata: MediaMetadata


_AUDIO_FORMATS = {
    "audio/mpeg": {"mp3"},
    "audio/wav": {"wav"}, "audio/x-wav": {"wav"},
    "audio/mp4": {"mov,mp4,m4a,3gp,3g2,mj2"},
    "audio/aac": {"aac"}, "audio/ogg": {"ogg"}, "audio/flac": {"flac"},
}
_VIDEO_FORMATS = {
    "video/mp4": {"mov,mp4,m4a,3gp,3g2,mj2"},
    "video/quicktime": {"mov,mp4,m4a,3gp,3g2,mj2"},
    "video/webm": {"matroska,webm"},
}


async def _read_bounded(reader: asyncio.StreamReader, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await reader.read(min(8192, maximum + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > maximum:
            raise MediaFailure("unsupported_media_type")
        chunks.append(chunk)


async def _stop_process(process: asyncio.subprocess.Process | Any) -> None:
    try:
        if process.returncode is None:
            process.kill()
    except Exception:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except Exception:
        pass


async def run_ffprobe(path: Path) -> dict[str, Any]:
    process = None
    stdout_task = None
    stderr_task = None
    started = asyncio.get_running_loop().time()

    def remaining() -> float:
        return max(
            0.0,
            FFPROBE_TIMEOUT_SECONDS - (asyncio.get_running_loop().time() - started),
        )

    try:
        executable = shutil.which("ffprobe")
        if executable is None:
            raise MediaFailure("provider_unavailable")
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                executable, "-nostdin", "-v", "error",
                "-protocol_whitelist", "file,pipe",
                "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,duration",
                "-of", "json", str(path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"LANG": "C", "LC_ALL": "C"},
            ),
            timeout=remaining(),
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(_read_bounded(process.stdout, FFPROBE_OUTPUT_MAX_BYTES))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, FFPROBE_OUTPUT_MAX_BYTES))
        stdout, _ = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task), timeout=remaining()
        )
        return_code = await asyncio.wait_for(process.wait(), timeout=remaining())
        if return_code != 0:
            raise MediaFailure("unsupported_media_type")
        value = json.loads(stdout.decode("utf-8"))
        if type(value) is not dict:
            raise MediaFailure("unsupported_media_type")
        return value
    except asyncio.TimeoutError:
        if process is not None:
            await _stop_process(process)
        raise MediaFailure("retrieval_timeout") from None
    except MediaFailure:
        if process is not None:
            await _stop_process(process)
        raise
    except asyncio.CancelledError:
        if process is not None:
            await _stop_process(process)
        raise
    except Exception:
        if process is not None:
            await _stop_process(process)
        raise MediaFailure("unsupported_media_type") from None
    finally:
        for task in (stdout_task, stderr_task):
            if task is not None and not task.done():
                task.cancel()


async def inspect_timed_media(
    path: Path,
    source_type: Literal["audio", "video"],
    declared_mime: str,
    *,
    runner: Callable[..., Awaitable[dict[str, Any]]] = run_ffprobe,
) -> TimedMediaInfo:
    if type(source_type) is not str or source_type not in {"audio", "video"}:
        raise MediaFailure("invalid_request")
    _require_file_size(path, source_type)
    mime = _content_type(declared_mime)
    formats = (_AUDIO_FORMATS if source_type == "audio" else _VIDEO_FORMATS).get(mime)
    if formats is None:
        raise MediaFailure("unsupported_media_type")
    try:
        result = await runner(path)
        if type(result) is not dict or type(result.get("format")) is not dict:
            raise MediaFailure("unsupported_media_type")
        container = result["format"].get("format_name")
        if type(container) is not str or container not in formats:
            raise MediaFailure("unsupported_media_type")
        duration_value = result["format"].get("duration")
        if type(duration_value) not in {str, int, float} or type(duration_value) is bool:
            raise MediaFailure("unsupported_media_type")
        duration = Decimal(str(duration_value))
        if not duration.is_finite() or duration <= 0:
            raise MediaFailure("unsupported_media_type")
        maximum = _limit(source_type).max_duration_seconds
        assert maximum is not None
        if duration > maximum:
            raise MediaFailure("source_too_large")
        streams = result.get("streams")
        if type(streams) is not list or not streams:
            raise MediaFailure("unsupported_media_type")
        expected = source_type
        if not any(type(stream) is dict and stream.get("codec_type") == expected for stream in streams):
            raise MediaFailure("unsupported_media_type")
        return TimedMediaInfo(float(duration), container)
    except MediaFailure:
        raise
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise MediaFailure("unsupported_media_type") from None
    except asyncio.CancelledError:
        raise
    except Exception:
        raise MediaFailure("retrieval_failed") from None


@asynccontextmanager
async def ingest_remote_media(
    url: str,
    source_type: Literal["image", "pdf", "audio", "video"],
    *,
    transport: StreamingTransport | None = None,
    ffprobe_runner: Callable[..., Awaitable[dict[str, Any]]] = run_ffprobe,
) -> AsyncIterator[IngestedMedia]:
    async with media_download(url, source_type, transport=transport) as download:
        if source_type == "image":
            metadata: MediaMetadata = inspect_image(download.path, download.mime_type)
        elif source_type == "pdf":
            metadata = inspect_pdf(download.path, download.mime_type)
        else:
            metadata = await inspect_timed_media(
                download.path, source_type, download.mime_type, runner=ffprobe_runner
            )
        yield IngestedMedia(download, metadata)


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    kind: Literal["inline", "provider_file"]
    payload: dict[str, Any] = field(repr=False)


def choose_delivery(
    *, source_type: str, mime_type: str, content: bytes,
    schema: dict[str, Any], instructions: str | None, prompt: str,
) -> DeliveryDecision:
    limit = _limit(source_type)
    if type(content) is not bytes or type(schema) is not dict or type(prompt) is not str:
        raise MediaFailure("invalid_request")
    if instructions is not None and type(instructions) is not str:
        raise MediaFailure("invalid_request")
    mime_type = _content_type(mime_type)
    if mime_type not in limit.allowed_mime_types:
        raise MediaFailure("unsupported_media_type")
    payload = {
        "media": {"mime_type": mime_type, "data": base64.b64encode(content).decode("ascii")},
        "schema": schema,
        "instructions": instructions,
        "prompt": prompt,
    }
    try:
        size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise MediaFailure("invalid_request") from None
    if size <= INLINE_REQUEST_MAX_BYTES:
        return DeliveryDecision("inline", payload)
    if source_type == "image":
        raise MediaFailure("source_too_large")
    return DeliveryDecision("provider_file", {"mime_type": mime_type})


@dataclass(frozen=True, slots=True)
class ProviderFileRef:
    file_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.file_id) is not str or not self.file_id or len(self.file_id) > 512:
            raise MediaFailure("provider_cleanup_failed")


class ProviderFileClient(Protocol):
    async def upload(self, *, path: Path, mime_type: str) -> ProviderFileRef: ...
    async def delete(self, file_id: str) -> bool | None: ...
    async def get_status(self, file_id: str) -> str: ...


async def _delete_provider_file(client: ProviderFileClient, file_id: str) -> None:
    try:
        deleted = await asyncio.wait_for(
            client.delete(file_id), timeout=PROVIDER_FILE_TIMEOUT_SECONDS
        )
        if deleted is True:
            return
        if deleted is None and await asyncio.wait_for(
            client.get_status(file_id), timeout=PROVIDER_FILE_TIMEOUT_SECONDS
        ) == "not_found":
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    raise MediaFailure("provider_cleanup_failed")


@asynccontextmanager
async def transient_provider_file(
    client: ProviderFileClient, path: Path, mime_type: str
) -> AsyncIterator[ProviderFileRef]:
    try:
        ref = await asyncio.wait_for(
            client.upload(path=path, mime_type=mime_type),
            timeout=PROVIDER_FILE_TIMEOUT_SECONDS,
        )
        if type(ref) is not ProviderFileRef:
            raise MediaFailure("provider_cleanup_failed")
    except MediaFailure:
        raise
    except Exception:
        raise MediaFailure("provider_cleanup_failed") from None
    body_error: BaseException | None = None
    try:
        yield ref
    except BaseException as error:
        body_error = error
    cleanup = asyncio.create_task(_delete_provider_file(client, ref.file_id))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError as cancelled:
        try:
            await asyncio.shield(cleanup)
        except Exception:
            raise MediaFailure("provider_cleanup_failed") from None
        if body_error is None:
            raise cancelled
    if body_error is not None:
        raise body_error


async def retrieve_webpage(
    url: str,
    render_mode: str,
    *,
    fetcher: Callable[..., dict[str, Any]] = smart_fetch,
) -> dict[str, Any]:
    if type(render_mode) is not str or render_mode not in {"auto", "always"}:
        raise MediaFailure("invalid_request")
    return await asyncio.to_thread(
        fetcher,
        url,
        force_browser=render_mode == "always",
        max_chars=50_000,
    )


__all__ = [
    "CONNECT_TIMEOUT_SECONDS", "DOWNLOAD_TIMEOUT_SECONDS", "DownloadedMedia",
    "FFPROBE_OUTPUT_MAX_BYTES", "FFPROBE_TIMEOUT_SECONDS", "INLINE_REQUEST_MAX_BYTES",
    "MEDIA_LIMITS", "MAX_REDIRECTS", "MediaFailure", "PROVIDER_FILE_TIMEOUT_SECONDS", "ProviderFileRef",
    "choose_delivery", "ingest_remote_media", "inspect_image", "inspect_pdf", "inspect_timed_media",
    "media_download", "retrieve_webpage", "run_ffprobe", "transient_provider_file",
]
