from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

DEFAULT_ALLOWED_HOSTS = frozenset({
    "figma-alpha-api.s3.us-west-2.amazonaws.com",
    "s3-alpha.figma.com",
    "images.figma.com",
})
REDIRECT_CODES = {301, 302, 303, 307, 308}


class DownloadError(RuntimeError):
    pass


@dataclass
class StreamResponse:
    status: int
    headers: dict[str, str]
    stream: BinaryIO


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _header(headers: dict[str, str], name: str, default: str = "") -> str:
    wanted = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == wanted), default)


def _open(url: str) -> StreamResponse:
    request = urllib.request.Request(url, headers={"Accept": "image/png,image/jpeg,image/gif,image/webp"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=30)
        return StreamResponse(response.status, dict(response.headers), response)
    except urllib.error.HTTPError as error:
        return StreamResponse(error.code, dict(error.headers), error)


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        valid_port = parsed.port in (None, 443)
    except ValueError as error:
        raise DownloadError(f"asset URL is malformed: {error}") from error
    if parsed.scheme != "https" or parsed.username or parsed.password or not valid_port:
        raise DownloadError("asset URL must use credential-free HTTPS on port 443")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in allowed_hosts:
        raise DownloadError(f"asset host is not allowlisted: {host or '<missing>'}")


def _kind_and_dimensions(data: bytes) -> tuple[str, int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "image/gif", width, height
    if data.startswith(b"RIFF") and len(data) >= 20 and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X" and len(data) >= 30:
            return "image/webp", 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return "image/webp", width, height
    if data.startswith(b"\xff\xd8"):
        index = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in sof_markers:
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return "image/jpeg", width, height
            if index + 4 > len(data):
                break
            length = int.from_bytes(data[index + 2:index + 4], "big")
            index += max(length + 2, 2)
    raise DownloadError("asset signature or dimensions are unsupported")


def _validate_complete_image(data: bytes, content_type: str) -> None:
    if content_type == "image/png":
        offset = 8
        saw_iend = False
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            end = offset + 12 + length
            if end > len(data):
                raise DownloadError("PNG chunk is truncated")
            chunk_type = data[offset + 4:offset + 8]
            chunk_data = data[offset + 8:offset + 8 + length]
            expected_crc = int.from_bytes(data[offset + 8 + length:end], "big")
            if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
                raise DownloadError("PNG chunk checksum is invalid")
            offset = end
            if chunk_type == b"IEND":
                saw_iend = True
                break
        if not saw_iend or offset != len(data):
            raise DownloadError("PNG is incomplete or has trailing bytes")
    elif content_type == "image/jpeg" and not data.endswith(b"\xff\xd9"):
        raise DownloadError("JPEG is incomplete")
    elif content_type == "image/gif" and not data.endswith(b";"):
        raise DownloadError("GIF is incomplete")
    elif content_type == "image/webp" and (len(data) < 12 or int.from_bytes(data[4:8], "little") + 8 != len(data)):
        raise DownloadError("WebP RIFF length is invalid")


def download_asset(
    url: str,
    destination: Path,
    *,
    expected_type: str | None = None,
    allowed_root: Path | None = None,
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
    max_bytes: int = 25 * 1024 * 1024,
    max_pixels: int = 32 * 1024 * 1024,
    max_redirects: int = 5,
    opener: Callable[[str], StreamResponse] = _open,
) -> dict[str, object]:
    if allowed_root is not None:
        root = allowed_root.resolve()
        resolved_destination = destination.resolve()
        if resolved_destination == root or root not in resolved_destination.parents:
            raise DownloadError("asset destination escapes the allowed visual directory")
    current = url
    response: StreamResponse | None = None
    for _ in range(max_redirects + 1):
        _validate_url(current, allowed_hosts)
        response = opener(current)
        if response.status in REDIRECT_CODES:
            location = _header(response.headers, "Location")
            if not location:
                response.stream.close()
                raise DownloadError("redirect omitted Location")
            current = urllib.parse.urljoin(current, location)
            response.stream.close()
            continue
        break
    else:
        raise DownloadError("too many asset redirects")
    assert response is not None
    try:
        if response.status in (403, 429):
            raise DownloadError(f"asset download returned HTTP {response.status}; URL may be expired or throttled")
        if response.status != 200:
            raise DownloadError(f"asset download returned HTTP {response.status}")

        declared = _header(response.headers, "Content-Type").split(";", 1)[0].strip().lower()
        if declared not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise DownloadError(f"missing or unsupported image content type: {declared or '<missing>'}")
        content_length = _header(response.headers, "Content-Length")
        if content_length:
            try:
                declared_bytes = int(content_length)
            except ValueError as error:
                raise DownloadError("asset Content-Length is invalid") from error
            if declared_bytes < 0 or declared_bytes > max_bytes:
                raise DownloadError("asset exceeds byte limit")
        else:
            declared_bytes = None

        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        total = 0
        digest = hashlib.sha256()
        content = bytearray()
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = response.stream.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise DownloadError("asset exceeds byte limit")
                    content.extend(chunk)
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if declared_bytes is not None and declared_bytes != total:
                raise DownloadError("asset length does not match Content-Length")
            actual_type, width, height = _kind_and_dimensions(bytes(content))
            _validate_complete_image(bytes(content), actual_type)
            if declared != actual_type:
                raise DownloadError(f"content type mismatch: declared {declared}, detected {actual_type}")
            if expected_type and actual_type != expected_type:
                raise DownloadError(f"unexpected image type: {actual_type}")
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise DownloadError(f"asset dimensions exceed limits: {width}x{height} > {max_pixels} pixels")
            os.replace(temporary, destination)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return {
            "sha256": digest.hexdigest(),
            "bytes": total,
            "content_type": actual_type,
            "width": width,
            "height": height,
        }
    finally:
        response.stream.close()

