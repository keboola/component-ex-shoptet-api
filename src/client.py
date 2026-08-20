"""HTTP client for the Shoptet REST API.

Covers everything network-facing so ``component.py`` stays a thin orchestrator:

* **Authentication** — both Shoptet auth models:
  ``private_token`` sends a Shoptet Premium private API token straight through
  (``Shoptet-Private-Api-Token``, matching the casing the OpenAPI
  ``securitySchemes`` declare — HTTP header names are case-insensitive per RFC
  7230, so this is cosmetic, but matching the spec avoids the reader wondering);
  ``addon_oauth`` exchanges an addon's permanent OAuth token for the 30-minute
  API access token (``Shoptet-Access-Token``) and refreshes it transparently on
  expiry or a 401.
* **Rate limiting** — Shoptet uses a leaky bucket (200 drops, draining 10/s) and
  publishes the fill level on every response, so we throttle *before* being told
  to, and honour ``Retry-After`` on the 429 we couldn't avoid.
* **Pagination** — ``page``/``itemsPerPage`` with the ``paginator`` object.
* **Snapshots** — the bulk read endpoints are asynchronous: they return a
  ``jobId``, the job is polled, and the result is a (usually gzipped) JSON Lines
  file. :meth:`ShoptetClient.iter_snapshot` hides all three steps behind an
  iterator of records.

Sources: https://developers.shoptet.com/api/documentation/ and the published
OpenAPI description at https://api.docs.shoptet.com/.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import zlib
from collections.abc import Iterator
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests
import tenacity
from keboola.component.exceptions import UserException

logger = logging.getLogger(__name__)

BASE_URL = "https://api.myshoptet.com"

# Shoptet's leaky bucket: 200 drops capacity, 10 drops/s drain, reported per
# response as `X-RateLimit-Bucket-Filling: <current>/<max>`.
BUCKET_HEADER = "X-RateLimit-Bucket-Filling"
_BUCKET_DRAIN_PER_SECOND = 10.0
# Above this fill ratio we pause to let the bucket drain rather than earn a 429.
_BUCKET_SLOWDOWN_RATIO = 0.75

# Retry / back-off
_MAX_RETRIES = 8
_MAX_BACKOFF_S = 120
# 423 = write lock, held at most 5s for a given URL.
_LOCK_WAIT_S = 5
_FALLBACK_WAIT = tenacity.wait_exponential_jitter(initial=2, max=_MAX_BACKOFF_S)

# Async job polling
_JOB_POLL_INITIAL_S = 2
_JOB_POLL_MAX_S = 30
# Snapshots of a large catalogue legitimately take minutes; give up well after
# that but long before a Keboola job would time out on its own.
_JOB_TIMEOUT_S = 3600
_JOB_TERMINAL_OK = "completed"
_JOB_TERMINAL_FAILED = frozenset({"failed", "expired", "killed"})

_SECRET_KEY_HINTS = ("token", "secret", "password", "authorization")


class ShoptetClientError(Exception):
    """Unexpected API failure — surfaces as exit code 2."""


class _TransientError(Exception):
    """Retryable failure: network error, 429, 423 lock or any 5xx."""

    def __init__(self, *, wait: float | None = None, status: int | None = None, cause: Exception | None = None) -> None:
        self.wait = wait
        self.status = status
        self.cause = cause
        super().__init__(f"transient Shoptet API failure (status={status})")


def _redact(value: Any) -> Any:
    """Mask secret-looking keys so error bodies are safe to log at DEBUG."""
    if isinstance(value, dict):
        return {k: ("***" if any(h in k.lower() for h in _SECRET_KEY_HINTS) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _parse_retry_after(raw: str | None) -> float | None:
    """Turn a ``Retry-After`` value into seconds to wait.

    Shoptet documents this header as a *datetime* rather than the usual delta in
    seconds, so accept both spellings (plus an HTTP-date, which the RFC allows)
    and never return a negative wait for a timestamp that has already passed.
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    for parse in (_parse_iso_datetime, parsedate_to_datetime):
        try:
            moment = parse(raw)
        except ValueError, TypeError:
            continue
        if moment is None:
            continue
        now = datetime.now(tz=moment.tzinfo) if moment.tzinfo else datetime.now()  # noqa: DTZ005
        return max(0.0, (moment - now).total_seconds())
    logger.warning("Could not interpret Retry-After header value %r; falling back to back-off.", raw)
    return None


def _parse_iso_datetime(raw: str) -> datetime | None:
    # Shoptet writes offsets without a colon ("+0100"); fromisoformat has
    # accepted that spelling, and a trailing "Z", since 3.11.
    return datetime.fromisoformat(raw)


def _decompress(payload: bytes) -> bytes:
    """Gunzip a snapshot payload if it is gzipped, otherwise pass it through.

    Most snapshot endpoints gzip their result file, a few do not, and the result
    URL is plain object storage that may or may not set Content-Encoding — so
    sniff the magic bytes instead of trusting the header.
    """
    if payload[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(payload)
        except (OSError, zlib.error) as err:
            raise ShoptetClientError(f"Snapshot result looked gzipped but could not be decompressed: {err}") from err
    return payload


class ShoptetClient:
    """Read-only client for one Shoptet e-shop."""

    def __init__(
        self,
        *,
        private_api_token: str | None = None,
        oauth_access_token: str | None = None,
        oauth_token_url: str | None = None,
        base_url: str = BASE_URL,
        request_timeout: int = 120,
    ) -> None:
        if not private_api_token and not (oauth_access_token and oauth_token_url):
            raise UserException(
                "No Shoptet credentials provided. Supply either a private API token, "
                "or an addon OAuth access token together with the e-shop OAuth server URL."
            )
        self._private_api_token = private_api_token
        self._oauth_access_token = oauth_access_token
        self._oauth_token_url = oauth_token_url
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"
        self._session.headers["User-Agent"] = "keboola-ex-shoptet-api/1.0"
        # Short-lived API access token for the addon OAuth flow: (token, expires_at_monotonic)
        self._api_token: str | None = None
        self._api_token_expires_at: float = 0.0

    # ---------------------------------------------------------------- auth

    def _auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        if self._private_api_token:
            return {"Shoptet-Private-Api-Token": self._private_api_token}
        return {"Shoptet-Access-Token": self._access_token(force_refresh=force_refresh)}

    def _access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid short-lived API access token, fetching one if needed.

        Tokens live ~30 minutes and a single OAuth token may hold at most five
        valid API tokens at once, so we reuse ours until it is close to expiring
        rather than minting one per request. A 60-second safety margin covers the
        clock skew between issuing the token and the last request that uses it.
        """
        if not force_refresh and self._api_token and time.monotonic() < self._api_token_expires_at:
            return self._api_token
        assert self._oauth_token_url is not None  # guaranteed by __init__
        response = self._request_with_retry(
            "GET",
            self._oauth_token_url,
            headers={"Authorization": f"Bearer {self._oauth_access_token}"},
            expect_json=True,
        )
        body = response if isinstance(response, dict) else {}
        token = body.get("access_token")
        if not token:
            raise UserException(
                "Shoptet OAuth server did not return an access token. Check that the OAuth access token "
                "is still valid and that the addon is installed in the e-shop."
            )
        expires_in = int(body.get("expires_in") or 1800)
        self._api_token = token
        self._api_token_expires_at = time.monotonic() + max(60, expires_in - 60)
        logger.debug("Obtained a Shoptet API access token valid for %ss.", expires_in)
        return token

    # ------------------------------------------------------------- requests

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET an API path (e.g. ``/api/orders``) and return the parsed body."""
        body = self._request_with_retry("GET", self._url(path), params=params, expect_json=True)
        return body if isinstance(body, dict) else {}

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
        raw: bool = False,
    ) -> Any:
        retryer = tenacity.Retrying(
            retry=tenacity.retry_if_exception_type(_TransientError),
            stop=tenacity.stop_after_attempt(_MAX_RETRIES),
            wait=self._retry_wait,
            sleep=time.sleep,
            before_sleep=self._log_retry,
            reraise=False,
        )
        try:
            return retryer(self._attempt, method, url, params, headers, expect_json, raw)
        except tenacity.RetryError as err:
            last = err.last_attempt.exception()
            status = getattr(last, "status", None)
            detail = f"status {status}" if status else f"{type(last).__name__}: {last}"
            raise UserException(
                f"Shoptet API did not recover after {_MAX_RETRIES} attempts ({detail}). Endpoint: {self._safe_url(url)}"
            ) from err

    def _attempt(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        expect_json: bool,
        raw: bool,
    ) -> Any:
        request_headers = dict(headers) if headers else self._auth_headers()
        try:
            response = self._session.request(method, url, params=params, headers=request_headers, timeout=self._timeout)
        except requests.RequestException as err:
            raise _TransientError(cause=err) from err

        self._throttle(response)

        if response.status_code == 401 and headers is None and not self._private_api_token:
            # The 30-minute API token expired mid-run — mint a fresh one and retry.
            logger.debug("Shoptet API returned 401; refreshing the API access token.")
            self._auth_headers(force_refresh=True)
            raise _TransientError(status=401, wait=0)
        if response.status_code == 429:
            raise _TransientError(status=429, wait=_parse_retry_after(response.headers.get("Retry-After")))
        if response.status_code == 423:
            raise _TransientError(status=423, wait=_LOCK_WAIT_S)
        if response.status_code >= 500:
            raise _TransientError(status=response.status_code)
        if not response.ok:
            raise self._user_error(response, url)

        if raw:
            return response.content
        if not expect_json:
            return response.text
        return self._parse_json(response, url)

    def _parse_json(self, response: requests.Response, url: str) -> Any:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as err:
            raise ShoptetClientError(
                f"Shoptet API returned a non-JSON response for {self._safe_url(url)} (status {response.status_code})."
            ) from err

    def _user_error(self, response: requests.Response, url: str) -> UserException:
        """Turn a 4xx into an actionable user error.

        Shoptet reports failures as ``{"errors": [{"errorCode", "message", "instance"}]}``;
        the 403 in particular almost always means the token lacks rights for that
        endpoint group, which is worth saying out loud because it is fixed in the
        e-shop administration, not in the configuration.
        """
        messages: list[str] = []
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            for error in body.get("errors") or []:
                if isinstance(error, dict) and error.get("message"):
                    instance = f" ({error['instance']})" if error.get("instance") else ""
                    messages.append(f"{error['message']}{instance}")
        detail = "; ".join(messages) or response.text[:300] or "no detail returned"
        endpoint = self._safe_url(url)

        if response.status_code in (401, 403):
            return UserException(
                f"Shoptet API refused the request to {endpoint} (HTTP {response.status_code}): {detail}. "
                "Check that the token is valid and that it has read rights for this endpoint group "
                "(e-shop administration → Connections → Private API), or that the addon has the "
                "endpoint approved in the API Partner section."
            )
        if response.status_code == 404:
            return UserException(
                f"Shoptet API returned 404 for {endpoint}: {detail}. "
                "The endpoint may not be enabled for this e-shop (module or tariff)."
            )
        return UserException(f"Shoptet API call to {endpoint} failed (HTTP {response.status_code}): {detail}")

    @staticmethod
    def _safe_url(url: str) -> str:
        """Path-only rendering of a URL, so tokens in a query string never reach a log."""
        parsed = urlparse(url)
        return parsed.path or url

    # ------------------------------------------------------------ throttling

    def _throttle(self, response: requests.Response) -> None:
        """Pause when the leaky bucket is close to full.

        Shoptet reports the bucket as ``current/max`` on every response and drains
        it at a documented 10 drops/s. Sleeping just long enough to bring the
        level back under the threshold keeps a long extraction moving steadily
        instead of sprinting into a 429 and then waiting out a penalty.
        """
        raw = response.headers.get(BUCKET_HEADER)
        if not raw or "/" not in raw:
            return
        current_raw, _, max_raw = raw.partition("/")
        try:
            current, maximum = float(current_raw), float(max_raw)
        except ValueError:
            return
        if maximum <= 0:
            return
        threshold = maximum * _BUCKET_SLOWDOWN_RATIO
        if current <= threshold:
            return
        wait = min((current - threshold) / _BUCKET_DRAIN_PER_SECOND, _MAX_BACKOFF_S)
        logger.debug("Rate-limit bucket at %s; pausing %.1fs to let it drain.", raw, wait)
        time.sleep(wait)

    @staticmethod
    def _retry_wait(state: tenacity.RetryCallState) -> float:
        exception = state.outcome.exception() if state.outcome else None
        if isinstance(exception, _TransientError) and exception.wait is not None:
            return min(exception.wait, _MAX_BACKOFF_S)
        return _FALLBACK_WAIT(state)

    @staticmethod
    def _log_retry(state: tenacity.RetryCallState) -> None:
        exception = state.outcome.exception() if state.outcome else None
        reason = getattr(exception, "status", None) or type(exception).__name__
        logger.warning(
            "Shoptet API request failed (%s); retrying in %.1fs (attempt %d/%d).",
            reason,
            state.next_action.sleep if state.next_action else 0,
            state.attempt_number,
            _MAX_RETRIES,
        )

    # ------------------------------------------------------------ pagination

    def iter_paginated(
        self, path: str, data_key: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every record of a paginated list endpoint.

        Follows ``paginator.pageCount`` rather than counting records, and stops on
        an empty page so a paginator that under-reports can never spin forever.
        """
        # `itemsPerPage` is deliberately not defaulted here: every Shoptet
        # collection has its own cap (10 for articles, 20 for reviews, 1000 for
        # stock supplies) and the caller passes the right one from the registry.
        # With none set, the API's own default applies.
        query = dict(params or {})
        page = 1
        while True:
            query["page"] = page
            data = self.get(path, query).get("data") or {}
            records = data.get(data_key) or []
            if not isinstance(records, list):
                raise ShoptetClientError(
                    f"Expected a list under data.{data_key} for {path}, got {type(records).__name__}."
                )
            yield from (record for record in records if isinstance(record, dict))
            paginator = data.get("paginator") or {}
            page_count = paginator.get("pageCount")
            if not records or not page_count or page >= int(page_count):
                return
            page += 1

    def get_single(self, path: str, data_key: str | None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch a non-paginated endpoint returning one object (e.g. ``/api/eshop``)."""
        data = self.get(path, params).get("data") or {}
        if data_key is None:
            return data if isinstance(data, dict) else {}
        record = data.get(data_key)
        return record if isinstance(record, dict) else {}

    def iter_list(self, path: str, data_key: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Yield records from a list endpoint that has no paginator at all."""
        data = self.get(path, params).get("data") or {}
        for record in data.get(data_key) or []:
            if isinstance(record, dict):
                yield record

    # ------------------------------------------------------------- snapshots

    def iter_snapshot(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Run an asynchronous snapshot export and yield its records.

        Three steps behind one iterator: submit the snapshot request (202 +
        ``jobId``), poll the job until it completes, then stream the JSON Lines
        result file. Snapshots are the only way to read a whole collection in one
        pass — the paginated list endpoints cap out at a few hundred records per
        second and return a shallower record — so this is the default for the
        large collections (orders, products, customers, documents).
        """
        job_id = self._submit_snapshot(path, params)
        result_url = self._await_job(job_id)
        if not result_url:
            logger.info("Snapshot job %s produced no result file; nothing to extract.", job_id)
            return
        yield from self._stream_jsonl(result_url)

    def _submit_snapshot(self, path: str, params: dict[str, Any] | None) -> str:
        data = self.get(path, params).get("data") or {}
        job_id = data.get("jobId")
        if not job_id:
            raise ShoptetClientError(f"Snapshot request to {path} returned no jobId: {_redact(data)}")
        logger.info("Submitted snapshot job %s for %s.", job_id, path)
        return str(job_id)

    def _await_job(self, job_id: str) -> str | None:
        """Poll a job to completion and return its ``resultUrl``.

        Back-off grows from 2s to 30s: a small export finishes almost at once, a
        full product catalogue can take minutes, and polling a slow job every two
        seconds would burn rate-limit budget the export itself needs.
        """
        deadline = time.monotonic() + _JOB_TIMEOUT_S
        interval: float = _JOB_POLL_INITIAL_S
        while True:
            job = self.get_single(f"/api/system/jobs/{job_id}", "job")
            status = str(job.get("status") or "").lower()
            if status == _JOB_TERMINAL_OK:
                logger.info("Snapshot job %s completed in %ss.", job_id, job.get("duration"))
                return job.get("resultUrl")
            if status in _JOB_TERMINAL_FAILED:
                log = (job.get("log") or "").strip()
                raise UserException(
                    f"Shoptet snapshot job {job_id} ended as '{status}'."
                    + (f" Shoptet reported: {log[:500]}" if log else "")
                )
            if time.monotonic() > deadline:
                raise UserException(
                    f"Shoptet snapshot job {job_id} was still '{status}' after "
                    f"{_JOB_TIMEOUT_S // 60} minutes; giving up. Narrow the date range and try again."
                )
            logger.debug("Snapshot job %s is '%s'; polling again in %.0fs.", job_id, status, interval)
            time.sleep(interval)
            interval = min(interval * 1.5, _JOB_POLL_MAX_S)

    def _stream_jsonl(self, result_url: str) -> Iterator[dict[str, Any]]:
        """Download a snapshot result file and yield one record per line.

        The result URL is an unguessable one-off link on the e-shop's own domain,
        so it is fetched *without* our token first — there is no reason to send a
        Shoptet credential to a host the API named — and only retried with
        authentication if the download is actually refused.
        """
        try:
            payload = self._request_with_retry("GET", result_url, headers={}, raw=True)
        except UserException:
            logger.debug("Snapshot result URL rejected an anonymous download; retrying authenticated.")
            payload = self._request_with_retry("GET", result_url, raw=True)

        text = _decompress(payload).decode("utf-8", errors="replace")
        count = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                logger.warning("Skipping malformed JSON on line %d of the snapshot result.", line_number)
                continue
            if isinstance(record, dict):
                count += 1
                yield record
        logger.info("Read %d records from the snapshot result file.", count)

    # ---------------------------------------------------------- diagnostics

    def get_eshop_info(self) -> dict[str, Any]:
        """Fetch ``/api/eshop`` — the cheapest call that proves the token works."""
        return self.get_single("/api/eshop", None)

    def list_approved_endpoints(self) -> list[str]:
        """Endpoints this token is actually allowed to read.

        Used to explain a 403 before it happens: a private API token only carries
        the endpoint groups it was granted, and an addon only the ones Shoptet
        approved, so the same configuration can work on one e-shop and not another.
        """
        endpoints: list[str] = []
        for record in self.iter_list("/api/system/endpoints", "endpoints", {"status": "approved"}):
            name = record.get("endpoint") or record.get("name")
            if name:
                endpoints.append(str(name))
        return endpoints
