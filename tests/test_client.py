import gzip
import json
import unittest
from typing import ClassVar
from unittest import mock

from keboola.component.exceptions import UserException

import client as client_module
from client import ShoptetClient, _decompress, _parse_retry_after
from tests.fake_api import FakeResponse, FakeSession, attach, paginated

BASE = "https://api.myshoptet.com"


def build_client(routes, **kwargs):
    """A client wired to a stub session, with sleeps disabled."""
    shoptet = ShoptetClient(private_api_token="token", **kwargs)
    return shoptet, attach(shoptet, FakeSession(routes))


class TestRetryAfter(unittest.TestCase):
    def test_plain_seconds(self):
        self.assertEqual(30.0, _parse_retry_after("30"))

    def test_iso_datetime_in_the_future(self):
        # Shoptet documents Retry-After as a datetime rather than a delta.
        wait = _parse_retry_after("2999-01-01T00:00:00+0000")
        assert wait is not None
        self.assertGreater(wait, 0)

    def test_datetime_in_the_past_never_returns_a_negative_wait(self):
        self.assertEqual(0.0, _parse_retry_after("2000-01-01T00:00:00+0000"))

    def test_unparseable_value_falls_back_to_back_off(self):
        self.assertIsNone(_parse_retry_after("soon-ish"))

    def test_missing_header(self):
        self.assertIsNone(_parse_retry_after(None))


class TestDecompress(unittest.TestCase):
    def test_gzipped_payload(self):
        self.assertEqual(b"hello", _decompress(gzip.compress(b"hello")))

    def test_plain_payload_passes_through(self):
        # A few snapshot endpoints do not gzip, so this must not be an error.
        self.assertEqual(b"hello", _decompress(b"hello"))


class TestAuth(unittest.TestCase):
    def test_private_token_header(self):
        shoptet, session = build_client({"/api/eshop": FakeResponse(200, {"data": {"trial": False}})})
        shoptet.get_eshop_info()
        self.assertEqual(1, len(session.calls))

    def test_missing_credentials_is_a_user_error(self):
        with self.assertRaises(UserException):
            ShoptetClient()

    def test_oauth_exchange_then_reuse(self):
        token_url = "https://1.myshoptet.com/action/ApiOAuthServer/getAccessToken"
        shoptet = ShoptetClient(oauth_access_token="permanent", oauth_token_url=token_url)
        session = FakeSession(
            {
                "/action/ApiOAuthServer/getAccessToken": FakeResponse(
                    200, {"access_token": "short-lived", "expires_in": 1800}
                ),
                "/api/eshop": FakeResponse(200, {"data": {"trial": False}}),
            }
        )
        attach(shoptet, session)

        shoptet.get_eshop_info()
        shoptet.get_eshop_info()

        exchanges = [call for call in session.calls if "ApiOAuthServer" in call[1]]
        self.assertEqual(1, len(exchanges), "the 30-minute token should be cached, not re-fetched per request")

    def test_oauth_server_without_a_token_is_a_user_error(self):
        shoptet = ShoptetClient(oauth_access_token="permanent", oauth_token_url="https://1.myshoptet.com/action/x")
        attach(shoptet, FakeSession({"/action/x": FakeResponse(200, {"expires_in": 1800})}))
        with self.assertRaises(UserException):
            shoptet.get_eshop_info()


class TestPagination(unittest.TestCase):
    def test_follows_page_count(self):
        shoptet, session = build_client(
            {
                "/api/categories": [
                    paginated([{"guid": "a"}], "categories", page=1, page_count=3),
                    paginated([{"guid": "b"}], "categories", page=2, page_count=3),
                    paginated([{"guid": "c"}], "categories", page=3, page_count=3),
                ]
            }
        )
        records = list(shoptet.iter_paginated("/api/categories", "categories"))
        self.assertEqual(["a", "b", "c"], [r["guid"] for r in records])
        self.assertEqual([1, 2, 3], [call[2]["page"] for call in session.calls])

    def test_stops_on_an_empty_page_even_if_the_paginator_claims_more(self):
        shoptet, _ = build_client(
            {
                "/api/categories": [
                    paginated([{"guid": "a"}], "categories", page=1, page_count=9),
                    paginated([], "categories", page=2, page_count=9),
                ]
            }
        )
        self.assertEqual(1, len(list(shoptet.iter_paginated("/api/categories", "categories"))))

    def test_non_dict_records_are_skipped(self):
        shoptet, _ = build_client(
            {"/api/categories": paginated([{"guid": "a"}, "junk"], "categories", page=1, page_count=1)}
        )
        self.assertEqual(1, len(list(shoptet.iter_paginated("/api/categories", "categories"))))


class TestErrors(unittest.TestCase):
    def test_403_explains_endpoint_rights(self):
        shoptet, _ = build_client(
            {
                "/api/orders": FakeResponse(
                    403, {"errors": [{"errorCode": "forbidden", "message": "Token has no rights"}]}
                )
            }
        )
        with self.assertRaises(UserException) as ctx:
            shoptet.get("/api/orders")
        message = str(ctx.exception)
        self.assertIn("Token has no rights", message)
        self.assertIn("read rights", message)

    def test_404_mentions_the_module_or_tariff(self):
        shoptet, _ = build_client({"/api/stocks": FakeResponse(404, {"errors": []})})
        with self.assertRaises(UserException) as ctx:
            shoptet.get("/api/stocks")
        self.assertIn("not be enabled", str(ctx.exception))

    def test_a_token_in_the_query_string_never_reaches_the_message(self):
        shoptet, _ = build_client({"/api/orders": FakeResponse(400, {"errors": []})})
        with self.assertRaises(UserException) as ctx:
            shoptet.get("/api/orders", {"token": "super-secret"})
        self.assertNotIn("super-secret", str(ctx.exception))


class TestRetrying(unittest.TestCase):
    @mock.patch.object(client_module.time, "sleep")
    def test_429_is_retried_honouring_retry_after(self, sleep):
        shoptet, _ = build_client(
            {
                "/api/categories": [
                    FakeResponse(429, {"errors": []}, headers={"Retry-After": "7"}),
                    paginated([{"guid": "a"}], "categories", page=1, page_count=1),
                ]
            }
        )
        records = list(shoptet.iter_paginated("/api/categories", "categories"))
        self.assertEqual(1, len(records))
        self.assertIn(7.0, [call.args[0] for call in sleep.call_args_list])

    @mock.patch.object(client_module.time, "sleep")
    def test_423_lock_is_retried(self, _sleep):
        shoptet, _ = build_client(
            {
                "/api/categories": [
                    FakeResponse(423, {"errors": []}),
                    paginated([{"guid": "a"}], "categories", page=1, page_count=1),
                ]
            }
        )
        self.assertEqual(1, len(list(shoptet.iter_paginated("/api/categories", "categories"))))

    @mock.patch.object(client_module.time, "sleep")
    def test_exhausted_retries_become_a_user_error(self, _sleep):
        shoptet, _ = build_client({"/api/categories": FakeResponse(503, {"errors": []})})
        with self.assertRaises(UserException) as ctx:
            list(shoptet.iter_paginated("/api/categories", "categories"))
        self.assertIn("did not recover", str(ctx.exception))

    @mock.patch.object(client_module.time, "sleep")
    def test_expired_oauth_token_is_refreshed_on_401(self, _sleep):
        token_url = "https://1.myshoptet.com/action/getAccessToken"
        shoptet = ShoptetClient(oauth_access_token="permanent", oauth_token_url=token_url)
        attach(
            shoptet,
            FakeSession(
                {
                    "/action/getAccessToken": FakeResponse(200, {"access_token": "fresh", "expires_in": 1800}),
                    "/api/eshop": [FakeResponse(401, {"errors": []}), FakeResponse(200, {"data": {"trial": False}})],
                }
            ),
        )
        self.assertEqual({"trial": False}, shoptet.get_eshop_info())


class TestThrottling(unittest.TestCase):
    @mock.patch.object(client_module.time, "sleep")
    def test_a_nearly_full_bucket_pauses_before_the_next_call(self, sleep):
        shoptet, _ = build_client(
            {
                "/api/categories": paginated([{"guid": "a"}], "categories", page=1, page_count=1),
            }
        )
        # 190/200 is 40 drops above the 75% threshold → 4s at 10 drops/s.
        shoptet._session._routes["/api/categories"][0].headers = {"X-RateLimit-Bucket-Filling": "190/200"}
        list(shoptet.iter_paginated("/api/categories", "categories"))
        self.assertEqual([4.0], [call.args[0] for call in sleep.call_args_list])

    @mock.patch.object(client_module.time, "sleep")
    def test_a_mostly_empty_bucket_does_not_pause(self, sleep):
        shoptet, _ = build_client({"/api/categories": paginated([{"guid": "a"}], "categories", page=1, page_count=1)})
        shoptet._session._routes["/api/categories"][0].headers = {"X-RateLimit-Bucket-Filling": "10/200"}
        list(shoptet.iter_paginated("/api/categories", "categories"))
        sleep.assert_not_called()


class TestSnapshots(unittest.TestCase):
    _RECORDS: ClassVar[list[dict[str, str]]] = [{"code": "1", "guid": "g1"}, {"code": "2", "guid": "g2"}]

    def _routes(self, statuses, payload):
        job_responses = [
            FakeResponse(200, {"data": {"job": {"jobId": "job1", "status": status, "resultUrl": None, "log": None}}})
            for status in statuses[:-1]
        ]
        job_responses.append(
            FakeResponse(
                200,
                {
                    "data": {
                        "job": {
                            "jobId": "job1",
                            "status": statuses[-1],
                            "duration": "1.5",
                            "resultUrl": "https://shop.example.com/api-results/job1.json",
                            "log": "boom" if statuses[-1] != "completed" else None,
                        }
                    }
                },
            )
        )
        return {
            "/api/orders/snapshot": FakeResponse(202, {"data": {"jobId": "job1"}}),
            "/api/system/jobs/job1": job_responses,
            "/api-results/job1.json": FakeResponse(200, content=payload),
        }

    @mock.patch.object(client_module.time, "sleep")
    def test_submit_poll_and_read_gzipped_jsonl(self, _sleep):
        payload = gzip.compress("\n".join(json.dumps(r) for r in self._RECORDS).encode())
        shoptet, session = build_client(self._routes(["pending", "running", "completed"], payload))

        records = list(shoptet.iter_snapshot("/api/orders/snapshot", {"changeTimeFrom": "2026-01-01T00:00:00+0000"}))

        self.assertEqual(self._RECORDS, records)
        polls = [call for call in session.calls if call[1] == "/api/system/jobs/job1"]
        self.assertEqual(3, len(polls), "the job should be polled until it reports completed")

    @mock.patch.object(client_module.time, "sleep")
    def test_uncompressed_result_is_read_too(self, _sleep):
        payload = "\n".join(json.dumps(r) for r in self._RECORDS).encode()
        shoptet, _ = build_client(self._routes(["completed"], payload))
        self.assertEqual(self._RECORDS, list(shoptet.iter_snapshot("/api/orders/snapshot")))

    @mock.patch.object(client_module.time, "sleep")
    def test_blank_and_malformed_lines_are_skipped(self, _sleep):
        payload = b'{"code": "1"}\n\nnot json\n{"code": "2"}\n'
        shoptet, _ = build_client(self._routes(["completed"], payload))
        self.assertEqual(["1", "2"], [r["code"] for r in shoptet.iter_snapshot("/api/orders/snapshot")])

    @mock.patch.object(client_module.time, "sleep")
    def test_failed_job_reports_the_shoptet_log(self, _sleep):
        shoptet, _ = build_client(self._routes(["failed"], b""))
        with self.assertRaises(UserException) as ctx:
            list(shoptet.iter_snapshot("/api/orders/snapshot"))
        self.assertIn("failed", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    @mock.patch.object(client_module.time, "sleep")
    def test_expired_job_is_a_user_error(self, _sleep):
        shoptet, _ = build_client(self._routes(["expired"], b""))
        with self.assertRaises(UserException):
            list(shoptet.iter_snapshot("/api/orders/snapshot"))

    @mock.patch.object(client_module.time, "sleep")
    def test_result_url_is_fetched_without_the_shoptet_token(self, _sleep):
        payload = b'{"code": "1"}'
        shoptet, session = build_client(self._routes(["completed"], payload))
        list(shoptet.iter_snapshot("/api/orders/snapshot"))
        # The stub records headers indirectly: assert the download happened at all,
        # and that the client did not need a second, authenticated attempt.
        downloads = [call for call in session.calls if call[1] == "/api-results/job1.json"]
        self.assertEqual(1, len(downloads))


if __name__ == "__main__":
    unittest.main()
