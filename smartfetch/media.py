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
import importlib
import ipaddress
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import threading
from types import MappingProxyType
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Protocol
from urllib.parse import urljoin, urlsplit
import weakref

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
WEBPAGE_TIMEOUT_SECONDS = 60.0
WORKER_EXIT_TIMEOUT_SECONDS = 1.0
WORKER_RESULT_MAX_BYTES = 1_000_000
DOWNLOAD_CONCURRENCY = 8
MEDIA_PROCESSING_CONCURRENCY = 2
CAPACITY_WAIT_SECONDS = 5.0

FAILURE_CODES = frozenset({
    "invalid_request",
    "invalid_source_url",
    "source_too_large",
    "unsupported_media_type",
    "retrieval_failed",
    "retrieval_timeout",
    "provider_cleanup_failed",
    "provider_unavailable",
    "capacity_unavailable",
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


MEDIA_LIMITS = MappingProxyType({
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
})


def _limit(source_type: str) -> MediaLimit:
    if type(source_type) is not str or source_type not in MEDIA_LIMITS:
        raise MediaFailure("invalid_request")
    return MEDIA_LIMITS[source_type]


_capacity_lock = threading.Lock()
_download_limiters: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_media_limiters: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _capacity_limiter(kind: Literal["download", "media"]) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    mapping = _download_limiters if kind == "download" else _media_limiters
    configured = DOWNLOAD_CONCURRENCY if kind == "download" else MEDIA_PROCESSING_CONCURRENCY
    with _capacity_lock:
        limiter = mapping.get(loop)
        if limiter is None or getattr(limiter, "_smartfetch_capacity", None) != configured:
            limiter = asyncio.Semaphore(configured)
            setattr(limiter, "_smartfetch_capacity", configured)
            mapping[loop] = limiter
    return limiter


def _reset_capacity_limiters_for_tests() -> None:
    """Clear loop-local capacity state; intended only for deterministic tests."""
    with _capacity_lock:
        _download_limiters.clear()
        _media_limiters.clear()


@asynccontextmanager
async def _capacity(kind: Literal["download", "media"]) -> AsyncIterator[None]:
    limiter = _capacity_limiter(kind)
    try:
        await asyncio.wait_for(limiter.acquire(), timeout=CAPACITY_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise MediaFailure("capacity_unavailable") from None
    try:
        yield
    finally:
        limiter.release()


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


def _open_local_file(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise MediaFailure("unsupported_media_type")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path = field(repr=False)
    source_type: str
    mime_type: str
    size_bytes: int
    workspace: Path | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    url: str = field(repr=False)
    hostname: str
    port: int
    addresses: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.url) is not str
            or type(self.hostname) is not str
            or type(self.port) is not int
            or self.port != 443
            or type(self.addresses) is not tuple
            or not self.addresses
            or any(type(address) is not str for address in self.addresses)
        ):
            raise MediaFailure("invalid_source_url")
        try:
            for address in self.addresses:
                ipaddress.ip_address(address)
        except ValueError:
            raise MediaFailure("invalid_source_url") from None


def _ip_url(address: str, port: int) -> str:
    parsed = ipaddress.ip_address(address)
    host = f"[{parsed.compressed}]" if parsed.version == 6 else parsed.compressed
    suffix = "" if port == 443 else f":{port}"
    return f"https://{host}{suffix}/"


def _resolve_public_target(url: str) -> PinnedTarget:
    try:
        if type(url) is not str or urlsplit(url).scheme.lower() != "https":
            raise MediaFailure("invalid_source_url")
        validated = validate_public_url(url)
        parsed = urlsplit(validated)
        if parsed.scheme.lower() != "https" or parsed.hostname is None:
            raise MediaFailure("invalid_source_url")
        hostname = parsed.hostname.lower().rstrip(".")
        port = parsed.port or 443
        infos = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        addresses: list[str] = []
        for info in infos:
            address = ipaddress.ip_address(info[4][0]).compressed
            validate_public_url(_ip_url(address, port))
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise MediaFailure("invalid_source_url")
        return PinnedTarget(validated, hostname, port, tuple(addresses))
    except MediaFailure:
        raise
    except Exception:
        raise MediaFailure("invalid_source_url") from None


class StreamingResponse(Protocol):
    status_code: int
    headers: Any

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...


class StreamingTransport(Protocol):
    async def open(
        self, target: PinnedTarget, *, connect_timeout: float, read_timeout: float
    ) -> StreamingResponse: ...


class HttpxStreamingTransport:
    """No-proxy, no-auto-redirect transport for explicitly validated public URLs."""

    __slots__ = ("_transport",)

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport or httpx.AsyncHTTPTransport(retries=0)

    async def open(
        self, target: PinnedTarget, *, connect_timeout: float, read_timeout: float
    ) -> httpx.Response:
        started = asyncio.get_running_loop().time()
        last_error: BaseException | None = None
        for address in target.addresses:
            remaining = connect_timeout - (asyncio.get_running_loop().time() - started)
            if remaining <= 0:
                raise httpx.ConnectTimeout("connection timeout")
            original = httpx.URL(target.url)
            pinned = original.copy_with(host=address, port=target.port)
            host_header = target.hostname if target.port == 443 else f"{target.hostname}:{target.port}"
            request = httpx.Request(
                "GET",
                pinned,
                headers={"Accept-Encoding": "identity", "Host": host_header},
                extensions={
                    "timeout": httpx.Timeout(read_timeout, connect=remaining).as_dict(),
                    "sni_hostname": target.hostname,
                },
            )
            try:
                return await self._transport.handle_async_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                last_error = error
        if isinstance(last_error, httpx.TimeoutException):
            raise last_error
        raise httpx.ConnectError("all approved addresses failed")

    async def aclose(self) -> None:
        await self._transport.aclose()


def _cleanup_workspace(workspace: Path | None) -> bool:
    if workspace is None:
        return True
    for _ in range(2):
        try:
            if workspace.exists():
                shutil.rmtree(workspace)
            if not workspace.exists():
                return True
        except OSError:
            continue
    return False


def _new_workspace() -> tuple[Path, int, Path]:
    try:
        workspace = Path(tempfile.mkdtemp(prefix="smartfetch-media-"))
        os.chmod(workspace, 0o700)
        path = workspace / "media.bin"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(path, flags, 0o600)
        return workspace, descriptor, path
    except Exception:
        if "workspace" in locals():
            _cleanup_workspace(workspace)
        raise MediaFailure("retrieval_failed") from None


async def _download(
    url: str,
    source_type: str,
    transport: StreamingTransport,
) -> DownloadedMedia:
    limit = _limit(source_type)
    current = url
    workspace: Path | None = None
    path: Path | None = None
    response: StreamingResponse | None = None
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            target = _resolve_public_target(current)
            try:
                response = await transport.open(
                    target,
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
                if type(declared) is not str:
                    raise MediaFailure("retrieval_failed")
                try:
                    declared_size = int(declared)
                except (TypeError, ValueError, OverflowError):
                    raise MediaFailure("retrieval_failed") from None
                if declared_size < 0:
                    raise MediaFailure("retrieval_failed")
                if declared_size > limit.max_bytes:
                    raise MediaFailure("source_too_large")

            workspace, descriptor, path = _new_workspace()
            try:
                size = 0
                with os.fdopen(descriptor, "wb") as output:
                    async for chunk in response.aiter_bytes():
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
            return DownloadedMedia(path, source_type, mime_type, size, workspace)
        raise MediaFailure("retrieval_failed")
    except BaseException as error:
        cleanup_ok = _cleanup_workspace(workspace)
        if not cleanup_ok:
            raise MediaFailure("retrieval_failed") from None
        if isinstance(error, MediaFailure):
            raise error
        if isinstance(error, asyncio.CancelledError):
            raise
        if isinstance(error, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
            raise MediaFailure("retrieval_timeout") from None
        raise MediaFailure("retrieval_failed") from None
    finally:
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                raise MediaFailure("retrieval_failed") from None


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
        async with _capacity("download"):
            item = await asyncio.wait_for(
                _download(url, source_type, active_transport),
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
            yield item
    except asyncio.TimeoutError:
        raise MediaFailure("retrieval_timeout") from None
    finally:
        cleanup_ok = _cleanup_workspace(item.workspace if item else None)
        if owned_transport:
            try:
                await active_transport.aclose()  # type: ignore[attr-defined]
            except Exception:
                raise MediaFailure("retrieval_failed") from None
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
        with _open_local_file(path) as source:
            with Image.open(source) as image:
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
        with _open_local_file(path) as source:
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


def _resolve_callable(module_name: str, qualified_name: str) -> Callable[..., Any]:
    if (
        type(module_name) is not str
        or type(qualified_name) is not str
        or "<locals>" in qualified_name
        or len(module_name) > 256
        or len(qualified_name) > 512
    ):
        raise MediaFailure("retrieval_failed")
    value: Any = importlib.import_module(module_name)
    for part in qualified_name.split("."):
        if not part:
            raise MediaFailure("retrieval_failed")
        value = getattr(value, part)
    if not callable(value):
        raise MediaFailure("retrieval_failed")
    return value


def _worker_operation(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    if operation == "image":
        info = inspect_image(Path(request["path"]), request["mime_type"])
        return {
            "width": info.width,
            "height": info.height,
            "frame_count": info.frame_count,
            "format": info.format,
        }
    if operation == "pdf":
        info = inspect_pdf(Path(request["path"]), request["mime_type"])
        return {"page_count": info.page_count}
    if operation == "webpage":
        fetcher = _resolve_callable(request["module"], request["qualified_name"])
        result = fetcher(
            request["url"],
            force_browser=request["force_browser"],
            max_chars=50_000,
        )
        if type(result) is not dict:
            raise MediaFailure("retrieval_failed")
        return result
    raise MediaFailure("retrieval_failed")


def _worker_entry(sender: Any, request_bytes: bytes) -> None:
    try:
        request = json.loads(request_bytes.decode("utf-8"))
        if type(request) is not dict:
            raise MediaFailure("retrieval_failed")
        response = {"ok": True, "result": _worker_operation(request)}
    except MediaFailure as error:
        response = {"ok": False, "code": error.code}
    except BaseException:
        response = {"ok": False, "code": "retrieval_failed"}
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > WORKER_RESULT_MAX_BYTES:
            encoded = b'{"ok":false,"code":"retrieval_failed"}'
        sender.send_bytes(encoded)
    except BaseException:
        pass
    finally:
        try:
            sender.close()
        except BaseException:
            pass


async def _wait_process_exit(
    process: multiprocessing.Process, timeout: float | None
) -> bool:
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    while process.is_alive():
        process.join(0)
        if not process.is_alive():
            break
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.01, remaining))
        else:
            await asyncio.sleep(0.01)
    process.join(0)
    return not process.is_alive()


async def _terminate_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
        await _wait_process_exit(process, WORKER_EXIT_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        await _wait_process_exit(process, None)
    if process.is_alive():
        raise MediaFailure("retrieval_failed")


async def _run_worker(request: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        request_bytes = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception:
        raise MediaFailure("retrieval_failed") from None
    if len(request_bytes) > 65_536:
        raise MediaFailure("retrieval_failed")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker_entry,
        args=(sender, request_bytes),
        name="smartfetch-media-worker",
        daemon=True,
    )
    try:
        process.start()
        sender.close()
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            response_bytes: bytes | None = None
            while response_bytes is None:
                if receiver.poll():
                    response_bytes = receiver.recv_bytes(WORKER_RESULT_MAX_BYTES + 1)
                    break
                if not process.is_alive():
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.01, remaining))
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(_terminate_process(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup)
            raise
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        exited = await _wait_process_exit(process, remaining)
        if response_bytes is None and exited:
            raise MediaFailure("retrieval_failed")
        if response_bytes is None or not exited:
            await _terminate_process(process)
            raise MediaFailure("retrieval_timeout")
        if process.exitcode != 0:
            raise MediaFailure("retrieval_failed")
        if len(response_bytes) > WORKER_RESULT_MAX_BYTES:
            raise MediaFailure("retrieval_failed")
        response = json.loads(response_bytes.decode("utf-8"))
        if type(response) is not dict or type(response.get("ok")) is not bool:
            raise MediaFailure("retrieval_failed")
        if response["ok"] is False:
            code = response.get("code")
            if type(code) is str and code in FAILURE_CODES:
                raise MediaFailure(code)
            raise MediaFailure("retrieval_failed")
        result = response.get("result")
        if type(result) is not dict:
            raise MediaFailure("retrieval_failed")
        return result
    except MediaFailure:
        if process.is_alive():
            await _terminate_process(process)
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        if process.is_alive():
            await _terminate_process(process)
        raise MediaFailure("retrieval_failed") from None
    finally:
        try:
            receiver.close()
        except Exception:
            pass
        try:
            sender.close()
        except Exception:
            pass
        try:
            process.close()
        except Exception:
            pass


async def _inspect_in_worker(
    path: Path, source_type: Literal["image", "pdf"], mime_type: str
) -> ImageInfo | PdfInfo:
    result = await _run_worker(
        {"operation": source_type, "path": str(path), "mime_type": mime_type},
        timeout=WEBPAGE_TIMEOUT_SECONDS,
    )
    try:
        if source_type == "image":
            if (
                type(result.get("width")) is not int
                or type(result.get("height")) is not int
                or type(result.get("frame_count")) is not int
                or result["width"] <= 0
                or result["height"] <= 0
                or result["frame_count"] != 1
                or type(result.get("format")) is not str
                or result["format"] not in _IMAGE_FORMAT_MIME
            ):
                raise MediaFailure("retrieval_failed")
            return ImageInfo(
                width=result["width"],
                height=result["height"],
                frame_count=result["frame_count"],
                format=result["format"],
            )
        if type(result.get("page_count")) is not int or not 1 <= result["page_count"] <= 20:
            raise MediaFailure("retrieval_failed")
        return PdfInfo(page_count=result["page_count"])
    except Exception:
        raise MediaFailure("retrieval_failed") from None


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
_AUDIO_CODECS = {
    "audio/mpeg": frozenset({"mp3"}),
    "audio/wav": frozenset({
        "pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
        "pcm_f64le", "pcm_alaw", "pcm_mulaw",
    }),
    "audio/x-wav": frozenset({
        "pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
        "pcm_f64le", "pcm_alaw", "pcm_mulaw",
    }),
    "audio/mp4": frozenset({"aac"}),
    "audio/aac": frozenset({"aac"}),
    "audio/ogg": frozenset({"vorbis", "opus"}),
    "audio/flac": frozenset({"flac"}),
}
_VIDEO_CODECS = {
    "video/mp4": frozenset({"h264", "hevc", "av1"}),
    "video/quicktime": frozenset({"h264", "hevc", "av1"}),
    "video/webm": frozenset({"vp8", "vp9", "av1"}),
}
_VIDEO_AUDIO_CODECS = frozenset({"aac", "mp3", "opus", "vorbis", "pcm_s16le"})


def _reported_duration(value: Any) -> Decimal:
    if type(value) not in {str, int, float}:
        raise MediaFailure("unsupported_media_type")
    try:
        duration = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise MediaFailure("unsupported_media_type") from None
    if not duration.is_finite() or duration < 0:
        raise MediaFailure("unsupported_media_type")
    return duration


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
    except asyncio.TimeoutError:
        try:
            process.kill()
        except Exception:
            pass
        await process.wait()
    except Exception:
        raise MediaFailure("retrieval_failed") from None


async def _feed_bounded_stdin(writer: Any, path: Path) -> None:
    total = 0
    try:
        with _open_local_file(path) as source:
            while True:
                chunk = source.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MEDIA_LIMITS["video"].max_bytes:
                    raise MediaFailure("source_too_large")
                writer.write(chunk)
                await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def run_ffprobe(path: Path) -> dict[str, Any]:
    process = None
    stdout_task = None
    stderr_task = None
    stdin_task = None
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
                "-protocol_whitelist", "pipe",
                "-i", "pipe:0",
                "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,duration",
                "-of", "json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"LANG": "C", "LC_ALL": "C"},
            ),
            timeout=remaining(),
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stdin_task = asyncio.create_task(_feed_bounded_stdin(process.stdin, path))
        stdout_task = asyncio.create_task(_read_bounded(process.stdout, FFPROBE_OUTPUT_MAX_BYTES))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, FFPROBE_OUTPUT_MAX_BYTES))
        _, stdout, _ = await asyncio.wait_for(
            asyncio.gather(stdin_task, stdout_task, stderr_task), timeout=remaining()
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
        tasks = tuple(task for task in (stdin_task, stdout_task, stderr_task) if task is not None)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


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
        streams = result.get("streams")
        if type(streams) is not list or not streams:
            raise MediaFailure("unsupported_media_type")
        if any(type(stream) is not dict for stream in streams):
            raise MediaFailure("unsupported_media_type")
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        if len(audio_streams) + len(video_streams) != len(streams):
            raise MediaFailure("unsupported_media_type")
        if source_type == "audio":
            if len(audio_streams) != 1 or video_streams:
                raise MediaFailure("unsupported_media_type")
            if audio_streams[0].get("codec_name") not in _AUDIO_CODECS[mime]:
                raise MediaFailure("unsupported_media_type")
        else:
            if len(video_streams) != 1 or len(audio_streams) > 1:
                raise MediaFailure("unsupported_media_type")
            if video_streams[0].get("codec_name") not in _VIDEO_CODECS[mime]:
                raise MediaFailure("unsupported_media_type")
            if audio_streams and audio_streams[0].get("codec_name") not in _VIDEO_AUDIO_CODECS:
                raise MediaFailure("unsupported_media_type")

        durations: list[Decimal] = []
        if "duration" in result["format"]:
            durations.append(_reported_duration(result["format"]["duration"]))
        for stream in streams:
            if "duration" in stream:
                durations.append(_reported_duration(stream["duration"]))
        if not durations:
            raise MediaFailure("unsupported_media_type")
        duration = max(durations)
        maximum = _limit(source_type).max_duration_seconds
        assert maximum is not None
        if duration > maximum:
            raise MediaFailure("source_too_large")
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
    async with _capacity("media"):
        async with media_download(url, source_type, transport=transport) as download:
            if source_type in {"image", "pdf"}:
                metadata: MediaMetadata = await _inspect_in_worker(
                    download.path, source_type, download.mime_type
                )
            else:
                metadata = await inspect_timed_media(
                    download.path, source_type, download.mime_type, runner=ffprobe_runner
                )
            yield IngestedMedia(download, metadata)


@dataclass(frozen=True, slots=True, init=False)
class DeliveryDecision:
    kind: Literal["inline", "provider_file"]
    _payload_json: bytes = field(repr=False)

    def __init__(self, kind: Literal["inline", "provider_file"], payload: dict[str, Any]) -> None:
        if type(kind) is not str or kind not in {"inline", "provider_file"} or type(payload) is not dict:
            raise MediaFailure("invalid_request")
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            raise MediaFailure("invalid_request") from None
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_payload_json", encoded)

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self._payload_json.decode("utf-8"))
        if type(value) is not dict:
            raise MediaFailure("invalid_request")
        return value


def choose_delivery(
    *, source_type: str, mime_type: str, content: bytes,
    schema: dict[str, Any], instructions: str | None, prompt: str,
) -> DeliveryDecision:
    limit = _limit(source_type)
    if type(content) is not bytes or type(schema) is not dict or type(prompt) is not str:
        raise MediaFailure("invalid_request")
    if instructions is not None and type(instructions) is not str:
        raise MediaFailure("invalid_request")
    if len(content) > limit.max_bytes:
        raise MediaFailure("source_too_large")
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
    deleted = False
    try:
        deleted = await asyncio.wait_for(
            client.delete(file_id), timeout=PROVIDER_FILE_TIMEOUT_SECONDS
        )
        if deleted is True:
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    try:
        if await asyncio.wait_for(
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
    try:
        module_name = getattr(fetcher, "__module__", None)
        qualified_name = getattr(fetcher, "__qualname__", None)
    except Exception:
        raise MediaFailure("invalid_request") from None
    if type(module_name) is not str or type(qualified_name) is not str:
        raise MediaFailure("invalid_request")
    async with _capacity("media"):
        return await _run_worker(
            {
                "operation": "webpage",
                "module": module_name,
                "qualified_name": qualified_name,
                "url": url,
                "force_browser": render_mode == "always",
            },
            timeout=WEBPAGE_TIMEOUT_SECONDS,
        )


__all__ = [
    "CONNECT_TIMEOUT_SECONDS", "DOWNLOAD_TIMEOUT_SECONDS", "DownloadedMedia",
    "FFPROBE_OUTPUT_MAX_BYTES", "FFPROBE_TIMEOUT_SECONDS", "INLINE_REQUEST_MAX_BYTES",
    "MEDIA_LIMITS", "MAX_REDIRECTS", "MediaFailure", "PROVIDER_FILE_TIMEOUT_SECONDS", "ProviderFileRef",
    "choose_delivery", "ingest_remote_media", "inspect_image", "inspect_pdf", "inspect_timed_media",
    "media_download", "retrieve_webpage", "run_ffprobe", "transient_provider_file",
]
