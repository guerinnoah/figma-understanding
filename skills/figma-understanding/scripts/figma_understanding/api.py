from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class FigmaAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class FigmaBatchError(FigmaAPIError):
    def __init__(self, message: str, *, partial: dict[str, str | None], status: int | None = None):
        super().__init__(message, status=status)
        self.partial = partial


@dataclass
class JsonResponse:
    status: int
    headers: dict[str, str]
    body: dict[str, Any]


Transport = Callable[[urllib.request.Request], JsonResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _header(headers: dict[str, str], name: str, default: str = "") -> str:
    wanted = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == wanted), default)


def _transport(request: urllib.request.Request) -> JsonResponse:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=120) as response:
            body = json.load(response)
            return JsonResponse(response.status, dict(response.headers), body)
    except urllib.error.HTTPError as error:
        try:
            try:
                body = json.load(error)
            except (ValueError, TypeError):
                body = {}
            return JsonResponse(error.code, dict(error.headers), body)
        finally:
            error.close()


class FigmaClient:
    def __init__(self, token: str, *, transport: Transport = _transport, sleep: Callable[[float], None] = time.sleep):
        if not token:
            raise ValueError("FIGMA_TOKEN is required")
        self._token = token
        self._transport = transport
        self._sleep = sleep

    def _get(self, path: str, query: dict[str, str], *, retries: int = 2) -> dict[str, Any]:
        url = "https://api.figma.com" + path + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"X-Figma-Token": self._token, "Accept": "application/json"})
        for attempt in range(retries + 1):
            try:
                response = self._transport(request)
            except (TimeoutError, urllib.error.URLError) as error:
                if attempt < retries:
                    self._sleep(min(2 ** attempt, 8))
                    continue
                raise FigmaAPIError(f"Figma API request failed after bounded retries: {error}") from error
            if response.status == 429 and attempt < retries:
                retry_after = _header(response.headers, "Retry-After", "1")
                try:
                    delay = min(max(float(retry_after), 0), 60)
                    if not math.isfinite(delay):
                        delay = 1
                except ValueError:
                    delay = 1
                self._sleep(delay)
                continue
            if response.status == 403:
                raise FigmaAPIError("Figma denied access (403); check token scope and file permission", status=403)
            if response.status == 429:
                raise FigmaAPIError("Figma rate limit persisted after bounded retries", status=429)
            if response.status < 200 or response.status >= 300:
                raise FigmaAPIError(f"Figma API returned HTTP {response.status}", status=response.status)
            if not isinstance(response.body, dict):
                raise FigmaAPIError("Figma API returned a non-object JSON response")
            return response.body
        raise AssertionError("unreachable")

    def render_urls(
        self,
        file_key: str,
        node_ids: list[str],
        *,
        version: str | None,
        batch_size: int = 50,
    ) -> dict[str, str | None]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        result: dict[str, str | None] = {}
        for offset in range(0, len(node_ids), batch_size):
            batch = node_ids[offset:offset + batch_size]
            query = {"ids": ",".join(batch), "format": "png", "scale": "1"}
            if version:
                query["version"] = version
            try:
                body = self._get(f"/v1/images/{urllib.parse.quote(file_key, safe='')}", query)
                images = body.get("images")
                if not isinstance(images, dict):
                    raise FigmaAPIError("Figma render response omitted the images map")
            except Exception as error:
                raise FigmaBatchError(str(error), partial=result, status=getattr(error, "status", None)) from error
            for node_id in batch:
                value = images.get(node_id)
                result[node_id] = value if isinstance(value, str) else None
        return result

    def original_urls(self, file_key: str) -> dict[str, str]:
        body = self._get(f"/v1/files/{urllib.parse.quote(file_key, safe='')}/images", {})
        images = body.get("images")
        if not isinstance(images, dict):
            raise FigmaAPIError("Figma original-image response omitted the images map")
        return {str(key): value for key, value in images.items() if isinstance(value, str)}

    def file(self, file_key: str) -> dict[str, Any]:
        return self._get(f"/v1/files/{urllib.parse.quote(file_key, safe='')}", {})

    def comments(self, file_key: str) -> dict[str, Any]:
        return self._get(f"/v1/files/{urllib.parse.quote(file_key, safe='')}/comments", {"as_md": "true"})

