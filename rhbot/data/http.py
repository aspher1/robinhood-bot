"""The only HTTP helper in this package. It issues GET requests."""

from __future__ import annotations

import httpx


def json_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    client: httpx.Client | None = None,
) -> tuple[int, object, dict[str, str]]:
    if client is None:
        with json_client() as owned_client:
            response = owned_client.get(url, headers=headers)
    else:
        response = client.get(url, headers=headers)
    try:
        body = response.json()
    except Exception:
        body = None
    return response.status_code, body, {k: v for k, v in response.headers.items()}
