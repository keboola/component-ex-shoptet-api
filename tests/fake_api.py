"""A stub ``requests.Session`` for driving the client without a Shoptet e-shop.

Tests route on ``(method, path)`` and can queue several responses for the same
path, which is what makes pagination, job polling and retry paths testable
offline. Everything the client actually touches on a response object is
implemented — status, headers, content, ``.json()`` — and nothing else.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._json_body = json_body
        if content is not None:
            self.content = content
        elif json_body is not None:
            self.content = json.dumps(json_body).encode()
        else:
            self.content = (text or "").encode()
        self.text = text if text is not None else self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


class FakeSession:
    """Serves queued responses per route and records every request made."""

    def __init__(self, routes: dict[str, list[FakeResponse] | FakeResponse]) -> None:
        self.headers: dict[str, str] = {}
        self._routes = {key: list(value) if isinstance(value, list) else [value] for key, value in routes.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> FakeResponse:
        path = urlparse(url).path
        self.calls.append((method, path, dict(params or {})))
        queued = self._routes.get(path)
        if not queued:
            return FakeResponse(404, {"errors": [{"message": f"no stub for {path}"}]})
        # The last queued response repeats, so a test only queues what varies.
        return queued.pop(0) if len(queued) > 1 else queued[0]


def paginated(records: list[Any], data_key: str, page: int, page_count: int) -> FakeResponse:
    """One page of a list endpoint, shaped like a real Shoptet response."""
    return FakeResponse(
        200,
        {
            "data": {
                data_key: records,
                "paginator": {
                    "totalCount": len(records) * page_count,
                    "page": page,
                    "pageCount": page_count,
                    "itemsOnPage": len(records),
                    "itemsPerPage": len(records),
                },
            },
            "errors": [],
            "metadata": {"requestId": "test"},
        },
    )


def attach(shoptet: Any, session: FakeSession) -> FakeSession:
    """Swap a client's real ``requests.Session`` for a stub.

    Deliberately reaching into the private attribute: the client owns its session
    so production code never has to accept an injected one, and a test double is
    the only reason to replace it.
    """
    shoptet._session = session
    return session
