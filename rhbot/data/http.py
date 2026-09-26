"""The only HTTP helper in this package. It issues GET requests."""

from __future__ import annotations

import httpx


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    timeout = httpx.Timeout(10.0, connect=5.0)
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url, headers=headers)
    try:
        body = response.json()
    except Exception:
        body = None
    return response.status_code, body, {k: v for k, v in response.headers.items()}
