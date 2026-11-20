"""Tiny JSON-over-HTTP helper on top of urllib, so the bot needs no third-party HTTP stack."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def request(method: str, url: str, *, headers: dict | None = None,
            json_body: dict | None = None, timeout: float = 20) -> bytes:
    """Raises ApiError on a non-2xx reply and OSError on network trouble."""
    data = None
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8", "replace")) from None
