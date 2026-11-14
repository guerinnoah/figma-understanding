from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

FILE_TYPES = {"design", "file", "board", "proto"}
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,256}$")


class FigmaSourceError(ValueError):
    pass


@dataclass(frozen=True)
class FigmaSource:
    url: str
    file_key: str
    file_type: str
    selected_node_id: str | None


def parse_figma_url(value: str) -> FigmaSource:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise FigmaSourceError(f"malformed Figma URL: {error}") from error
    if parsed.scheme != "https" or (parsed.hostname or "").lower().rstrip(".") not in {"figma.com", "www.figma.com"}:
        raise FigmaSourceError("expected an HTTPS figma.com file URL")
    if parsed.username or parsed.password or port not in (None, 443):
        raise FigmaSourceError("Figma URL must not contain credentials or a nonstandard port")
    segments = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(segments) < 2 or segments[0] not in FILE_TYPES:
        raise FigmaSourceError("URL is not a supported Figma design, file, board, or prototype route")
    if "branch" in {segment.casefold() for segment in segments[2:]}:
        raise FigmaSourceError("nested branch routes are ambiguous; use the branch's canonical file URL")
    file_key = segments[1]
    if not KEY_RE.fullmatch(file_key):
        raise FigmaSourceError("Figma file key is malformed")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if any(key.casefold().replace("_", "-") in {"version-id", "version"} for key in query):
        raise FigmaSourceError("historical-version URLs are not accepted for a current full-file inventory")
    node_values = query.get("node-id", []) + query.get("node_id", [])
    selected_node_id = node_values[-1].strip() if node_values and node_values[-1].strip() else None
    return FigmaSource(value.strip(), file_key, segments[0], selected_node_id)

