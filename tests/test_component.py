"""End-to-end tests: a config plus stubbed HTTP traffic in, CSV and manifests out.

These exercise the parts most likely to break silently — child-table splitting,
primary keys, typed manifests, incremental windows and state — without needing a
Shoptet e-shop.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from freezegun import freeze_time
from keboola.component.exceptions import UserException

import client as client_module
from component import _REGISTRY, Component, FetchMode
from configuration import Configuration, ObjectType
from tests.fake_api import FakeResponse, FakeSession, paginated

_ORDER = {
    "code": "2026000001",
    "guid": "order-guid",
    "creationTime": "2026-01-05T09:02:27+0100",
    "changeTime": "2026-01-06T09:02:27+0100",
    "email": "customer@example.com",
    "cashDeskOrder": False,
    "price": {"withVat": "242.00", "currencyCode": "CZK"},
    "items": [
        {"code": "SKU-1", "name": "Mug", "amount": 2.0},
        {"code": "SKU-1", "name": "Mug (gift wrap)", "amount": 1.0},
    ],
}


def _snapshot_routes(records: list[dict[str, Any]], path: str = "/api/orders/snapshot") -> dict[str, Any]:
    payload = gzip.compress("\n".join(json.dumps(record) for record in records).encode())
    return {
        path: FakeResponse(202, {"data": {"jobId": "job1"}}),
        "/api/system/jobs/job1": FakeResponse(
            200,
            {
                "data": {
                    "job": {
                        "jobId": "job1",
                        "status": "completed",
                        "duration": "1.0",
                        "resultUrl": "https://shop.example.com/api-results/job1.json",
                        "log": None,
                    }
                }
            },
        ),
        "/api-results/job1.json": FakeResponse(200, content=payload),
    }


class ComponentTestCase(unittest.TestCase):
    """Runs the component against a stub session inside a throwaway data dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        (self.data_dir / "in").mkdir()
        (self.data_dir / "out" / "tables").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def write_config(self, parameters: dict[str, Any]) -> None:
        base = {"auth_type": "private_token", "#private_api_token": "token"}
        (self.data_dir / "config.json").write_text(json.dumps({"parameters": {**base, **parameters}}))

    def write_state(self, state: dict[str, Any]) -> None:
        (self.data_dir / "in" / "state.json").write_text(json.dumps(state))

    def run_component(self, routes: dict[str, Any]) -> FakeSession:
        session = FakeSession(routes)
        with (
            mock.patch.dict(os.environ, {"KBC_DATADIR": str(self.data_dir)}),
            mock.patch.object(client_module.requests, "Session", return_value=session),
            mock.patch.object(client_module.time, "sleep"),
        ):
            Component().run()
        return session

    def expect_failure(self, routes: dict[str, Any]) -> UserException:
        with self.assertRaises(UserException) as ctx:
            self.run_component(routes)
        return ctx.exception

    # ------------------------------------------------------------- assertions

    def read_table(self, name: str) -> tuple[list[str], list[dict[str, str]]]:
        """Return the manifest column order and the rows of an output table.

        Output CSVs are headerless on purpose — the manifest schema is
        authoritative — so the columns have to be read from the manifest.
        """
        manifest = json.loads((self.data_dir / "out" / "tables" / f"{name}.csv.manifest").read_text())
        columns = [column["name"] for column in manifest["schema"]]
        with (self.data_dir / "out" / "tables" / f"{name}.csv").open(newline="") as fh:
            rows = [dict(zip(columns, row, strict=True)) for row in csv.reader(fh)]
        return columns, rows

    def manifest(self, name: str) -> dict[str, Any]:
        return json.loads((self.data_dir / "out" / "tables" / f"{name}.csv.manifest").read_text())

    def primary_key(self, name: str) -> list[str]:
        """Primary-key column names, in schema order.

        There is no top-level ``primary_key`` key in the manifest; each column
        in ``schema`` carries its own ``primary_key`` flag instead.
        """
        return [column["name"] for column in self.manifest(name)["schema"] if column.get("primary_key")]

    def table_names(self) -> set[str]:
        return {path.stem for path in (self.data_dir / "out" / "tables").glob("*.csv")}

    def state(self) -> dict[str, Any]:
        return json.loads((self.data_dir / "out" / "state.json").read_text())


class TestSnapshotExtraction(ComponentTestCase):
    def test_orders_produce_a_parent_table_and_an_items_child_table(self):
        self.write_config({"object": "orders", "load_type": "full_load"})
        self.run_component(_snapshot_routes([_ORDER]))

        self.assertEqual({"orders", "orders_items"}, self.table_names())

        columns, rows = self.read_table("orders")
        self.assertEqual("code", columns[0], "the primary key comes first")
        self.assertEqual(1, len(rows))
        self.assertEqual("2026000001", rows[0]["code"])
        # `items` moved to its own table and must not linger on the parent row.
        self.assertNotIn("items", columns)
        # A nested object that is not an array stays as a JSON column.
        self.assertEqual({"withVat": "242.00", "currencyCode": "CZK"}, json.loads(rows[0]["price"]))

    def test_child_rows_carry_the_parent_key_and_their_position(self):
        self.write_config({"object": "orders", "load_type": "full_load"})
        self.run_component(_snapshot_routes([_ORDER]))

        columns, rows = self.read_table("orders_items")
        self.assertEqual(["order_code", "_row_number"], columns[:2])
        self.assertEqual(["2026000001", "2026000001"], [row["order_code"] for row in rows])
        # Both items share a product code, so the position is what tells them apart.
        self.assertEqual(["1", "2"], [row["_row_number"] for row in rows])
        self.assertEqual(["SKU-1", "SKU-1"], [row["code"] for row in rows])

    def test_child_primary_key_is_the_parent_key_plus_the_position(self):
        self.write_config({"object": "orders", "load_type": "full_load"})
        self.run_component(_snapshot_routes([_ORDER]))
        self.assertEqual(
            ["order_code", "_row_number"],
            [column["name"] for column in self.manifest("orders_items")["schema"] if column.get("primary_key")],
        )

    def test_child_tables_can_be_kept_inline(self):
        self.write_config({"object": "orders", "load_type": "full_load", "extract_child_tables": False})
        self.run_component(_snapshot_routes([_ORDER]))

        self.assertEqual({"orders"}, self.table_names())
        _, rows = self.read_table("orders")
        self.assertEqual(2, len(json.loads(rows[0]["items"])))

    def test_types_are_inferred_for_the_manifest(self):
        self.write_config({"object": "orders", "load_type": "full_load"})
        self.run_component(_snapshot_routes([_ORDER]))

        types = {c["name"]: c["data_type"]["base"]["type"] for c in self.manifest("orders")["schema"]}
        self.assertEqual("BOOLEAN", types["cashDeskOrder"])
        self.assertEqual("TIMESTAMP", types["creationTime"], "ISO time columns should not land as plain strings")
        self.assertEqual("STRING", types["email"])

        item_types = {c["name"]: c["data_type"]["base"]["type"] for c in self.manifest("orders_items")["schema"]}
        self.assertEqual("NUMERIC", item_types["amount"])
        self.assertEqual("INTEGER", item_types["_row_number"])

    def test_an_empty_snapshot_writes_no_table(self):
        self.write_config({"object": "orders", "load_type": "full_load"})
        self.run_component(_snapshot_routes([]))
        self.assertEqual(set(), self.table_names())

    def test_a_blank_primary_key_gets_a_placeholder(self):
        # Keboola typed-table PK columns are NOT NULL, so an empty cell would fail
        # the load; the row is still worth keeping.
        self.write_config({"object": "orders", "load_type": "full_load"})
        self.run_component(_snapshot_routes([{**_ORDER, "code": ""}]))
        _, rows = self.read_table("orders")
        self.assertEqual("__empty__", rows[0]["code"])


class TestLoadTypesAndWindows(ComponentTestCase):
    def _params(self, session: FakeSession, path: str = "/api/orders/snapshot") -> dict[str, Any]:
        return next(call[2] for call in session.calls if call[1] == path)

    def test_full_load_sends_no_change_filter_and_overwrites(self):
        self.write_config({"object": "orders", "load_type": "full_load"})
        session = self.run_component(_snapshot_routes([_ORDER]))
        self.assertNotIn("changeTimeFrom", self._params(session))
        self.assertFalse(self.manifest("orders")["incremental"])

    def test_first_incremental_run_has_no_watermark_so_pulls_everything(self):
        self.write_config({"object": "orders"})
        session = self.run_component(_snapshot_routes([_ORDER]))
        self.assertNotIn("changeTimeFrom", self._params(session))
        self.assertTrue(self.manifest("orders")["incremental"])

    def test_incremental_run_reuses_the_watermark_minus_the_lookback(self):
        self.write_config({"object": "orders", "lookback_hours": 2})
        self.write_state({"last_run": "2026-03-10T12:00:00+0000"})
        session = self.run_component(_snapshot_routes([_ORDER]))
        self.assertEqual("2026-03-10T10:00:00+0000", self._params(session)["changeTimeFrom"])

    def test_a_date_range_maps_to_the_endpoint_creation_filters(self):
        self.write_config(
            {
                "object": "orders",
                "load_type": "full_load",
                "date_range": {"date_from": "2026-01-01", "date_to": "2026-02-01"},
            }
        )
        session = self.run_component(_snapshot_routes([_ORDER]))
        params = self._params(session)
        self.assertEqual("2026-01-01T00:00:00+0000", params["creationTimeFrom"])
        self.assertEqual("2026-02-01T00:00:00+0000", params["creationTimeTo"])

    def test_abandoned_carts_use_their_own_visit_time_filter(self):
        # Not every collection filters on creation time; sending the wrong
        # parameter name would be rejected by the API.
        self.write_config(
            {"object": "abandoned_carts", "load_type": "full_load", "date_range": {"date_from": "2026-01-01"}}
        )
        session = self.run_component(_snapshot_routes([{"guid": "cart-1"}], "/api/abandoned-carts/snapshot"))
        self.assertEqual(
            "2026-01-01T00:00:00+0000", self._params(session, "/api/abandoned-carts/snapshot")["visitTimeFrom"]
        )

    @freeze_time("2026-05-10T08:00:00+00:00")
    def test_the_watermark_is_the_run_start_time(self):
        self.write_config({"object": "orders"})
        self.run_component(_snapshot_routes([_ORDER]))
        self.assertEqual({"last_run": "2026-05-10T08:00:00+0000"}, self.state())

    def test_an_unparseable_date_is_a_user_error(self):
        self.write_config({"object": "orders", "date_range": {"date_from": "the day before the thing"}})
        self.assertIn("Could not parse date", str(self.expect_failure(_snapshot_routes([_ORDER]))))


class TestChangeFeeds(ComponentTestCase):
    _ROUTES: ClassVar[dict[str, Any]] = {
        "/api/orders/changes": paginated(
            [{"code": "1", "changeTime": "2026-03-01T10:00:00+0100", "changeType": "delete"}],
            "changes",
            page=1,
            page_count=1,
        )
    }

    @freeze_time("2026-03-10T00:00:00+00:00")
    def test_first_run_falls_back_to_a_default_window(self):
        # `from` is mandatory on the change feeds, so a first run must still send one.
        self.write_config({"object": "orders_changes"})
        session = self.run_component(self._ROUTES)
        params = next(call[2] for call in session.calls if call[1] == "/api/orders/changes")
        self.assertEqual("2026-03-03T00:00:00+0000", params["from"])

    def test_the_watermark_wins_once_there_is_one(self):
        self.write_config({"object": "orders_changes", "lookback_hours": 0})
        self.write_state({"last_run": "2026-03-09T06:00:00+0000"})
        session = self.run_component(self._ROUTES)
        params = next(call[2] for call in session.calls if call[1] == "/api/orders/changes")
        self.assertEqual("2026-03-09T06:00:00+0000", params["from"])

    @freeze_time("2026-03-16T00:00:00+00:00")
    def test_a_full_load_ignores_the_stored_watermark(self):
        """The value matters, not just the presence of `from`.

        Asserting only `assertIn("from", params)` — as this test originally did —
        cannot fail: a change feed always sends `from`. It therefore passed while the
        code narrowed an explicit full load to the *stale watermark* and then
        overwrote the whole table with that narrow slice, discarding every previously
        accumulated event. Same shape as the `abandoned_carts` trap, third instance.
        """
        self.write_config({"object": "orders_changes", "load_type": "full_load"})
        self.write_state({"last_run": "2026-03-09T06:00:00+0000"})
        session = self.run_component(self._ROUTES)
        params = next(call[2] for call in session.calls if call[1] == "/api/orders/changes")
        # The default window from "now" (2026-03-09 minus lookback would be the bug).
        self.assertEqual("2026-03-09T00:00:00+0000", params["from"])
        self.assertFalse(self.manifest("orders_changes")["incremental"])

    @freeze_time("2026-03-16T00:00:00+00:00")
    def test_a_full_load_honours_an_explicit_date_range(self):
        self.write_config(
            {
                "object": "orders_changes",
                "load_type": "full_load",
                "date_range": {"date_from": "2026-01-01"},
            }
        )
        self.write_state({"last_run": "2026-03-09T06:00:00+0000"})
        session = self.run_component(self._ROUTES)
        params = next(call[2] for call in session.calls if call[1] == "/api/orders/changes")
        self.assertEqual("2026-01-01T00:00:00+0000", params["from"])

    def test_an_incremental_run_still_uses_the_watermark(self):
        # The fix above must not break the normal incremental path.
        self.write_config({"object": "orders_changes", "lookback_hours": 24})
        self.write_state({"last_run": "2026-03-09T06:00:00+0000"})
        session = self.run_component(self._ROUTES)
        params = next(call[2] for call in session.calls if call[1] == "/api/orders/changes")
        self.assertEqual("2026-03-08T06:00:00+0000", params["from"])

    def test_a_change_feed_key_includes_the_change_type(self):
        # `changeTime` alone is schema-nullable and not guaranteed unique across
        # entities, so the primary key needs all three columns to identify one
        # change event (gap analysis finding 9).
        self.write_config({"object": "orders_changes", "load_type": "full_load"})
        self.run_component(self._ROUTES)
        self.assertEqual(
            ["code", "changeTime", "changeType"],
            [column["name"] for column in self.manifest("orders_changes")["schema"] if column.get("primary_key")],
        )

    def test_products_and_customers_changes_are_keyed_by_guid_not_code(self):
        # Blocker 1: the products/customers change-log responses have no `code`
        # field at all, only `guid`. Keying by the never-present `code` collapses
        # the effective primary key down to the (nullable) `changeTime` alone,
        # silently merging unrelated change events on upsert.
        routes = {
            "/api/products/changes": paginated(
                [{"guid": "g1", "changeTime": "2026-03-01T10:00:00+0100", "changeType": "edit"}],
                "changes",
                page=1,
                page_count=1,
            )
        }
        self.write_config({"object": "products_changes", "load_type": "full_load"})
        self.run_component(routes)
        self.assertEqual(
            ["guid", "changeTime", "changeType"],
            [column["name"] for column in self.manifest("products_changes")["schema"] if column.get("primary_key")],
        )
        _, rows = self.read_table("products_changes")
        self.assertEqual("g1", rows[0]["guid"])


class TestPerStockObjects(ComponentTestCase):
    _STOCKS: ClassVar[FakeResponse] = FakeResponse(
        200, {"data": {"stocks": [{"id": 1}, {"id": 7}], "defaultStockId": 1}}
    )

    def test_supplies_are_read_for_every_stock_and_stamped_with_its_id(self):
        routes = {
            "/api/stocks": self._STOCKS,
            "/api/stocks/1/supplies": paginated([{"productGuid": "g1", "code": "A"}], "supplies", 1, 1),
            "/api/stocks/7/supplies": paginated([{"productGuid": "g2", "code": "B"}], "supplies", 1, 1),
        }
        self.write_config({"object": "stock_supplies", "load_type": "full_load"})
        self.run_component(routes)

        columns, rows = self.read_table("stock_supplies")
        self.assertEqual(["stock_id", "productGuid", "code"], columns[:3])
        self.assertEqual([("1", "A"), ("7", "B")], [(row["stock_id"], row["code"]) for row in rows])

    def test_pinning_one_stock_skips_the_lookup(self):
        routes = {
            "/api/stocks": self._STOCKS,
            "/api/stocks/7/supplies": paginated([{"productGuid": "g2", "code": "B"}], "supplies", 1, 1),
        }
        self.write_config({"object": "stock_supplies", "load_type": "full_load", "stock_id": "7"})
        session = self.run_component(routes)

        self.assertEqual([], [call for call in session.calls if call[1] == "/api/stocks"])
        _, rows = self.read_table("stock_supplies")
        self.assertEqual(["7"], [row["stock_id"] for row in rows])


class TestSingleRecordObjects(ComponentTestCase):
    def test_eshop_is_one_row_with_no_primary_key(self):
        routes = {"/api/eshop": FakeResponse(200, {"data": {"trial": False, "currencies": [{"code": "CZK"}]}})}
        self.write_config({"object": "eshop"})
        self.run_component(routes)

        manifest = self.manifest("eshop")
        self.assertEqual([], manifest.get("primary_key", []))
        self.assertFalse(manifest["incremental"], "a table without a primary key cannot be upserted")
        _, rows = self.read_table("eshop")
        self.assertEqual(1, len(rows))
        self.assertEqual([{"code": "CZK"}], json.loads(rows[0]["currencies"]))


class TestIncludeSections(ComponentTestCase):
    def test_requested_sections_are_forwarded(self):
        self.write_config({"object": "orders", "load_type": "full_load", "include": ["notes", "shippingDetails"]})
        session = self.run_component(_snapshot_routes([_ORDER]))
        params = next(call[2] for call in session.calls if call[1] == "/api/orders/snapshot")
        self.assertEqual("notes,shippingDetails", params["include"])

    def test_an_unknown_section_fails_before_any_request(self):
        self.write_config({"object": "orders", "include": ["nope"]})
        message = str(self.expect_failure(_snapshot_routes([_ORDER])))
        self.assertIn("nope", message)
        self.assertIn("shippingDetails", message, "the error should list what is actually supported")

    def test_sections_are_ignored_where_the_endpoint_has_none(self):
        routes = {"/api/brands": paginated([{"guid": "brand-guid"}], "brands", 1, 1)}
        self.write_config({"object": "brands", "load_type": "full_load", "include": ["notes"]})
        session = self.run_component(routes)
        self.assertNotIn("include", next(call[2] for call in session.calls if call[1] == "/api/brands"))


class TestPrimaryKeyFixes(ComponentTestCase):
    """Objects whose declared primary key never appeared in the real response.

    Each of these silently forced a full overwrite with no deduplication
    (gap-analysis findings 2, 3, 7, 8): the declared PK column was absent, so
    `_finalize_table`'s defensive `effective_pk` filter dropped it, leaving an
    empty primary key.
    """

    def test_brands_are_keyed_by_guid(self):
        # The response has no `code` field at all; only the (confusingly also
        # named `code`) path parameter of the single-brand endpoint is a guid.
        routes = {"/api/brands": paginated([{"guid": "brand-guid", "name": "Acme"}], "brands", 1, 1)}
        self.write_config({"object": "brands", "load_type": "full_load"})
        self.run_component(routes)
        _, rows = self.read_table("brands")
        self.assertEqual(["guid"], self.primary_key("brands"))
        self.assertEqual("brand-guid", rows[0]["guid"])

    def test_customer_groups_are_keyed_by_id(self):
        # The response has no `guid` field at all; `id` is the real identifier.
        routes = {"/api/customers/groups": FakeResponse(200, {"data": {"customerGroups": [{"id": 7, "name": "VIP"}]}})}
        self.write_config({"object": "customer_groups", "load_type": "full_load"})
        self.run_component(routes)
        self.assertEqual(["id"], self.primary_key("customer_groups"))

    def test_order_history_is_keyed_by_order_and_id_together(self):
        # `id` is only documented as an "order history identifier" with example value
        # 1, and the snapshot record carries `orderCode` expressly to relate the remark
        # to its order — so `id` is very likely a per-order sequence. Two orders whose
        # first remark is both `id: 1` must stay two rows; keying on `id` alone would
        # merge them. `creationTime` stays out of the key because it is nullable.
        routes = _snapshot_routes(
            [
                {"id": 1, "orderCode": "2026000001", "creationTime": None, "text": "note A"},
                {"id": 1, "orderCode": "2026000002", "creationTime": None, "text": "note B"},
            ],
            "/api/orders/history/snapshot",
        )
        self.write_config({"object": "order_history", "load_type": "full_load"})
        self.run_component(routes)
        self.assertEqual(["orderCode", "id"], self.primary_key("order_history"))
        _, rows = self.read_table("order_history")
        self.assertEqual(2, len(rows), "a per-order id must not collapse two orders' remarks")

    def test_product_pricelist_prices_is_keyed_by_product_and_pricelist_code_with_no_child_table(self):
        # Each JSONL record is already one (product x price list) row: `guid`
        # never appears (the product identifier is `productGuid`), and the
        # top-level `prices` field is an unrelated preview-price object, not a
        # per-price-list array worth its own child table.
        routes = _snapshot_routes(
            [{"productGuid": "prod-1", "code": "EUR-LIST", "price": "10.00", "prices": {"purchase": "8.00"}}],
            "/api/products/snapshot/pricelists",
        )
        self.write_config({"object": "product_pricelist_prices", "load_type": "full_load"})
        self.run_component(routes)
        self.assertEqual({"product_pricelist_prices"}, self.table_names())
        self.assertEqual(["productGuid", "code"], self.primary_key("product_pricelist_prices"))


class TestCustomerDeliveryAddresses(ComponentTestCase):
    def test_the_singular_delivery_address_field_produces_the_plural_child_table(self):
        # Blocker 4: the API field is `deliveryAddress` (singular, but an array);
        # the previous registry entry looked for `deliveryAddresses` (plural) and
        # so this child table was never created.
        routes = _snapshot_routes(
            [
                {
                    "guid": "cust-1",
                    "deliveryAddress": [{"street": "Main St", "city": "Prague"}],
                    "accounts": [],
                    "remarks": [],
                }
            ],
            "/api/customers/snapshot",
        )
        self.write_config({"object": "customers", "load_type": "full_load"})
        self.run_component(routes)
        self.assertIn("customers_delivery_addresses", self.table_names())
        _, rows = self.read_table("customers_delivery_addresses")
        self.assertEqual("Prague", rows[0]["city"])


class TestProofPayments(ComponentTestCase):
    def test_proof_payments_never_declares_an_items_child_table(self):
        # Finding 11: `proofPaymentSnapshot` has no `items` field at all; reusing
        # the invoice-family children for it was an unchecked generalization.
        routes = _snapshot_routes([{"code": "PP1", "isValid": True}], "/api/proof-payments/snapshot")
        self.write_config({"object": "proof_payments", "load_type": "full_load"})
        self.run_component(routes)
        self.assertEqual({"proof_payments"}, self.table_names())


class TestAbandonedCarts(ComponentTestCase):
    """Blocker 5: no identifier field exists in the API response at all."""

    def _routes(self, records):
        return _snapshot_routes(records, "/api/abandoned-carts/snapshot")

    def test_there_is_no_primary_key(self):
        self.write_config({"object": "abandoned_carts", "load_type": "full_load"})
        self.run_component(self._routes([{"date": "2026-01-01T00:00:00+0100", "cartValue": "100.00"}]))
        self.assertEqual([], self.primary_key("abandoned_carts"))

    def test_an_incremental_row_is_forced_to_a_full_load_with_a_warning(self):
        # The row default is `incremental_load`; honouring it here would fetch
        # only the newest slice and overwrite the whole table with it.
        self.write_config({"object": "abandoned_carts"})  # incremental_load is the default
        with self.assertLogs("component", level="WARNING") as logs:
            self.run_component(self._routes([{"date": "2026-01-01T00:00:00+0100"}]))
        self.assertFalse(self.manifest("abandoned_carts")["incremental"])
        self.assertTrue(
            any("no stable identifier" in message for message in logs.output),
            "expected a warning explaining the forced full load",
        )

    def test_no_change_window_is_ever_sent_even_with_a_previous_watermark(self):
        # A watermark from a previous run must not narrow this fetch: there is no
        # primary key to upsert on, so a narrowed fetch plus a full overwrite
        # would silently drop every previously accumulated cart.
        self.write_config({"object": "abandoned_carts"})
        self.write_state({"last_run": "2026-03-09T06:00:00+0000"})
        session = self.run_component(self._routes([{"date": "2026-01-01T00:00:00+0100"}]))
        params = next(call[2] for call in session.calls if call[1] == "/api/abandoned-carts/snapshot")
        self.assertNotIn("visitTimeFrom", params)

    @staticmethod
    def _config(**overrides: Any) -> Configuration:
        params: dict[str, Any] = {
            "auth_type": "private_token",
            "#private_api_token": "token",
            "object": "orders_changes",
            **overrides,
        }
        return Configuration(**params)

    def test_the_guard_holds_for_a_change_feed_too(self):
        """The one shape where `full_load_only` used to be bypassed.

        Checking this through `abandoned_carts` alone proves nothing: that entry has
        no `changed_from` at all, so `_incremental_since` is never even consulted and
        the assertion above cannot fail however the guard behaves. Change feeds are
        different — their `from` is mandatory, so they deliberately skip the
        incremental check, and a keyless one would have been date-windowed while
        writing a table it cannot upsert into. No such object exists today; this
        pins the guard so adding one stays safe.
        """
        keyless_change_feed = replace(_REGISTRY[ObjectType.orders_changes], primary_key=[], full_load_only=True)
        window = Component._incremental_since(
            Component.__new__(Component),
            self._config(lookback_hours=24),
            keyless_change_feed,
            {"last_run": "2026-03-10T12:00:00+0000"},
            None,
            False,
        )
        self.assertIsNone(window, "a full_load_only change feed must never receive a watermark window")

    def test_a_full_load_only_change_feed_still_honours_an_explicit_date(self):
        # The guard refuses the automatic watermark, not a user-chosen date.
        chosen = datetime(2026, 2, 1, tzinfo=UTC)
        window = Component._incremental_since(
            Component.__new__(Component),
            self._config(lookback_hours=24),
            replace(_REGISTRY[ObjectType.orders_changes], primary_key=[], full_load_only=True),
            {"last_run": "2026-03-10T12:00:00+0000"},
            chosen,
            False,
        )
        self.assertEqual(chosen, window)

    def test_an_explicit_date_range_still_narrows_the_fetch(self):
        # Unlike the automatic incremental watermark, a user-chosen "Date range"
        # is an explicit, informed narrowing and is still honoured.
        self.write_config(
            {"object": "abandoned_carts", "load_type": "full_load", "date_range": {"date_from": "2026-01-01"}}
        )
        session = self.run_component(self._routes([{"date": "2026-01-01T00:00:00+0100"}]))
        params = next(call[2] for call in session.calls if call[1] == "/api/abandoned-carts/snapshot")
        self.assertEqual("2026-01-01T00:00:00+0000", params["visitTimeFrom"])


class TestDateAndChangeFilters(ComponentTestCase):
    def test_discount_coupons_honours_the_creation_date_filter(self):
        # Finding 12: the endpoint supports creationTimeFrom/To; the registry
        # wasn't wiring it, so the row's "Date range" fields silently did nothing.
        routes = {"/api/discount-coupons": paginated([{"code": "SALE10"}], "coupons", 1, 1)}
        self.write_config(
            {
                "object": "discount_coupons",
                "load_type": "full_load",
                "date_range": {"date_from": "2026-01-01", "date_to": "2026-02-01"},
            }
        )
        session = self.run_component(routes)
        params = next(call[2] for call in session.calls if call[1] == "/api/discount-coupons")
        self.assertEqual("2026-01-01T00:00:00+0000", params["creationTimeFrom"])
        self.assertEqual("2026-02-01T00:00:00+0000", params["creationTimeTo"])

    def test_stock_movements_honours_the_incremental_watermark(self):
        # Finding 13: the endpoint supports changeTimeFrom; without wiring it,
        # every run re-read the entire movement history for every stock.
        routes = {
            "/api/stocks": FakeResponse(200, {"data": {"stocks": [{"id": 1}]}}),
            "/api/stocks/1/movements": paginated([{"id": 1}], "movements", 1, 1),
        }
        self.write_config({"object": "stock_movements", "lookback_hours": 0})
        self.write_state({"last_run": "2026-03-09T06:00:00+0000"})
        session = self.run_component(routes)
        params = next(call[2] for call in session.calls if call[1] == "/api/stocks/1/movements")
        self.assertEqual("2026-03-09T06:00:00+0000", params["changeTimeFrom"])


class TestEmptyPrimaryKeyCollisions(ComponentTestCase):
    def test_more_than_one_empty_primary_key_row_is_warned_about(self):
        # Finding 14: `categories.guid` is `required` but schema-nullable — at
        # most one synthetic/root category is plausible with a null guid,
        # anything more silently collapses onto the shared placeholder.
        routes = {
            "/api/categories": paginated(
                [{"guid": None, "name": "Root"}, {"guid": None, "name": "Also root?"}], "categories", 1, 1
            )
        }
        self.write_config({"object": "categories", "load_type": "full_load"})
        with self.assertLogs("component", level="WARNING") as logs:
            self.run_component(routes)
        self.assertTrue(any("empty primary key" in message for message in logs.output))

    def test_a_single_empty_primary_key_row_is_not_warned_about(self):
        routes = {"/api/categories": paginated([{"guid": None, "name": "Root"}], "categories", 1, 1)}
        self.write_config({"object": "categories", "load_type": "full_load"})
        with self.assertNoLogs("component", level="WARNING"):
            self.run_component(routes)


class TestRegistry(unittest.TestCase):
    """Invariants that would otherwise only fail against a live e-shop."""

    def test_every_object_has_an_endpoint(self):
        self.assertEqual([], [obj.value for obj in ObjectType if obj not in _REGISTRY])

    def test_paginated_endpoints_declare_a_page_size(self):
        # Every Shoptet collection caps itemsPerPage differently and rejects a
        # larger value, so a paginated endpoint without an explicit cap is a bug.
        missing = [
            obj.value
            for obj, endpoint in _REGISTRY.items()
            if endpoint.mode in (FetchMode.PAGINATED, FetchMode.PER_STOCK) and not endpoint.items_per_page
        ]
        self.assertEqual([], missing)

    def test_only_paginated_endpoints_declare_a_page_size(self):
        stray = [
            obj.value
            for obj, endpoint in _REGISTRY.items()
            if endpoint.items_per_page and endpoint.mode not in (FetchMode.PAGINATED, FetchMode.PER_STOCK)
        ]
        self.assertEqual([], stray)

    def test_children_require_a_parent_prefix(self):
        # Child rows are keyed by prefixed parent columns; without a prefix they
        # would collide with the child's own fields of the same name.
        missing = [obj.value for obj, e in _REGISTRY.items() if e.children and not e.parent_prefix]
        self.assertEqual([], missing)

    def test_per_stock_paths_are_templated(self):
        for obj, endpoint in _REGISTRY.items():
            if endpoint.mode == FetchMode.PER_STOCK:
                self.assertIn("{stock_id}", endpoint.path, obj.value)

    def test_list_and_paginated_endpoints_name_their_data_key(self):
        missing = [
            obj.value
            for obj, e in _REGISTRY.items()
            if e.mode in (FetchMode.PAGINATED, FetchMode.LIST, FetchMode.PER_STOCK) and not e.data_key
        ]
        self.assertEqual([], missing)


class TestConfigurationErrors(ComponentTestCase):
    def test_a_missing_object_is_a_user_error(self):
        self.write_config({})
        self.assertIn("object", str(self.expect_failure({})))

    def test_missing_credentials_are_a_user_error(self):
        (self.data_dir / "config.json").write_text(json.dumps({"parameters": {"object": "orders"}}))
        self.assertIn("private API token", str(self.expect_failure({})))


if __name__ == "__main__":
    unittest.main()
