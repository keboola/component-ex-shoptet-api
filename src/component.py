"""Shoptet API extractor — main component class.

Replaces the CSV-permalink Shoptet extractor with the official Shoptet REST API.
``run()`` is a thin orchestrator; every HTTP concern (auth, rate limiting,
pagination, asynchronous snapshot jobs) lives in :mod:`client`.

One **object** is extracted per config row. How each object is read — a bulk
JSON Lines snapshot, a paginated list, a flat list, a single record, or one call
per stock — plus its primary key, date filters and nested child tables, is
declared once in :data:`_REGISTRY`. Adding an object means adding a registry
entry and an enum member, not new control flow.

Nested arrays on a record (order items, product variants, …) become their own
tables keyed back to the parent, because a Shoptet order is a document with a
variable number of line items and squashing it into one row would either lose
data or explode the column count.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any

import dateparser
from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType, SelectElement, ValidationResult
from keboola.vcr import DefaultSanitizer

from client import BASE_URL, ShoptetClient
from configuration import AuthType, Configuration, Credentials, ObjectType

# VCR sanitizers, picked up by the datadirtest VCR scaffolder during recording.
# Only real credentials are redacted: the two auth headers plus the token field
# in the OAuth exchange body. Response *content* is deliberately left intact —
# the scaffolder builds each test's `expected/` output from the sanitized
# recording, so redacting a field that is also an output column would make
# replay diverge from `expected/` and fail every run.
_SENSITIVE_FIELDS = [
    "access_token",
    "Shoptet-Access-Token",
    "Shoptet-Private-API-Token",
]
VCR_SANITIZERS = [DefaultSanitizer(sensitive_fields=_SENSITIVE_FIELDS)]

logger = logging.getLogger(__name__)

_STATE_LAST_RUN = "last_run"

# Shoptet expects ISO 8601 with a numeric offset and no colon ("2017-12-12T22:08:01+0100").
_API_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# Keboola typed-table PK columns are physically NOT NULL and an empty CSV field
# imports as NULL, so a blank primary-key cell would fail the storage load.
_EMPTY_PK_PLACEHOLDER = "__empty__"

# Position of a child row inside its parent, part of every child table's primary
# key: Shoptet line items carry no stable id of their own (an order item has only
# a product code, which can legitimately repeat within one order), so the index
# is the only thing that identifies a row uniquely and reproducibly.
_ROW_NUMBER_COLUMN = "_row_number"

# The `changes` endpoints require a `from` timestamp. On a first run there is no
# watermark and possibly no configured date, so fall back to a window that is
# useful rather than failing.
_CHANGES_DEFAULT_WINDOW_DAYS = 7

# Column-name hints that mark an ISO string as a timestamp rather than text.
_TIMESTAMP_COLUMN_HINTS = ("time", "date")


class FetchMode(Enum):
    SNAPSHOT = auto()  # asynchronous job → gzipped JSON Lines file
    PAGINATED = auto()  # page/itemsPerPage, driven by the `paginator` object
    LIST = auto()  # single response carrying the whole array
    SINGLE = auto()  # one record (e.g. /api/eshop)
    PER_STOCK = auto()  # paginated, once per stock in the e-shop


@dataclass(frozen=True)
class _Child:
    """A nested array to split into its own table."""

    field: str  # field name on the parent record
    suffix: str  # output table is f"{object}_{suffix}"


@dataclass(frozen=True)
class _Endpoint:
    """Everything needed to read one object and lay it out in Storage."""

    path: str
    mode: FetchMode
    primary_key: list[str]
    # Key under `data` holding the array (paginated/list) or the record (single).
    data_key: str | None = None
    # Query parameters this endpoint uses for a creation-time window.
    created_from: str | None = None
    created_to: str | None = None
    # Query parameter for the incremental change window. Change feeds call it
    # `from` and require it; snapshots call it `changeTimeFrom`.
    changed_from: str | None = None
    # Nested arrays promoted to child tables.
    children: tuple[_Child, ...] = ()
    # Prefix for the parent-key columns injected into child rows.
    parent_prefix: str = ""
    # Values accepted by the snapshot `include` parameter, for validation and UI.
    include_options: tuple[str, ...] = ()
    # Page size to request. Each Shoptet collection documents its own maximum
    # (10 for articles, 20 for reviews, 1000 for stock supplies), and asking for
    # more than the cap is rejected — so this is the documented max per endpoint,
    # not one global guess. None leaves the API default in place.
    items_per_page: int | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)


_ORDER_CHILDREN = (
    _Child("items", "items"),
    _Child("shippings", "shippings"),
    _Child("paymentMethods", "payment_methods"),
    _Child("completion", "completion"),
    _Child("paymentTransactions", "payment_transactions"),
)

_PRODUCT_CHILDREN = (
    _Child("variants", "variants"),
    _Child("images", "images"),
    _Child("categories", "categories"),
    _Child("flags", "flags"),
    _Child("descriptiveParameters", "descriptive_parameters"),
    _Child("filteringParameters", "filtering_parameters"),
    _Child("surchargeParameters", "surcharge_parameters"),
    _Child("setItems", "set_items"),
    _Child("gifts", "gifts"),
    _Child("relatedProducts", "related_products"),
    _Child("alternativeProducts", "alternative_products"),
    _Child("relatedFiles", "related_files"),
    _Child("relatedVideos", "related_videos"),
    _Child("perStockAmounts", "stock_amounts"),
    _Child("perPricelistPrices", "pricelist_prices"),
)

_DOCUMENT_CHILDREN = (_Child("items", "items"),)


# Change feeds share one shape: paginated, `from` is mandatory, and each row is
# an (entity, changeType, changeTime) triple rather than the entity itself.
def _changes_endpoint(path: str, items_per_page: int) -> _Endpoint:
    """A change feed: paginated, `from` is mandatory, one row per change event."""
    return _Endpoint(
        path,
        FetchMode.PAGINATED,
        ["code", "changeTime"],
        data_key="changes",
        changed_from="from",
        items_per_page=items_per_page,
    )


# object → how to read it. Paths, data keys, filters and `include` options all
# come from the published OpenAPI description (https://api.docs.shoptet.com/).
_REGISTRY: dict[ObjectType, _Endpoint] = {
    # ---------------------------------------------------------- bulk snapshots
    ObjectType.orders: _Endpoint(
        "/api/orders/snapshot",
        FetchMode.SNAPSHOT,
        ["code"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=_ORDER_CHILDREN,
        parent_prefix="order",
        include_options=("notes", "images", "shippingDetails", "stockLocation", "surchargeParameters", "productFlags"),
    ),
    ObjectType.order_history: _Endpoint(
        "/api/orders/history/snapshot",
        FetchMode.SNAPSHOT,
        ["orderCode", "creationTime"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
    ),
    ObjectType.products: _Endpoint(
        "/api/products/snapshot",
        FetchMode.SNAPSHOT,
        ["guid"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=_PRODUCT_CHILDREN,
        parent_prefix="product",
        include_options=(
            "images",
            "variantParameters",
            "allCategories",
            "flags",
            "descriptiveParameters",
            "measureUnit",
            "surchargeParameters",
            "setItems",
            "filteringParameters",
            "recyclingFee",
            "consumptionTax",
            "warranty",
            "sortVariants",
            "gifts",
            "alternativeProducts",
            "relatedProducts",
            "relatedVideos",
            "relatedFiles",
            "perStockAmounts",
            "perPricelistPrices",
        ),
    ),
    ObjectType.product_pricelist_prices: _Endpoint(
        "/api/products/snapshot/pricelists",
        FetchMode.SNAPSHOT,
        ["guid"],
        children=(_Child("prices", "prices"),),
        parent_prefix="product",
    ),
    ObjectType.customers: _Endpoint(
        "/api/customers/snapshot",
        FetchMode.SNAPSHOT,
        ["guid"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=(
            _Child("accounts", "accounts"),
            _Child("deliveryAddresses", "delivery_addresses"),
            _Child("remarks", "remarks"),
        ),
        parent_prefix="customer",
    ),
    ObjectType.invoices: _Endpoint(
        "/api/invoices/snapshot",
        FetchMode.SNAPSHOT,
        ["code"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=_DOCUMENT_CHILDREN,
        parent_prefix="invoice",
        include_options=("surchargeParameters",),
    ),
    ObjectType.proforma_invoices: _Endpoint(
        "/api/proforma-invoices/snapshot",
        FetchMode.SNAPSHOT,
        ["code"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=_DOCUMENT_CHILDREN,
        parent_prefix="proforma_invoice",
        include_options=("surchargeParameters",),
    ),
    ObjectType.credit_notes: _Endpoint(
        "/api/credit-notes/snapshot",
        FetchMode.SNAPSHOT,
        ["code"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=_DOCUMENT_CHILDREN,
        parent_prefix="credit_note",
        include_options=("surchargeParameters",),
    ),
    ObjectType.delivery_notes: _Endpoint(
        "/api/delivery-notes/snapshot",
        FetchMode.SNAPSHOT,
        ["code"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=_DOCUMENT_CHILDREN,
        parent_prefix="delivery_note",
    ),
    ObjectType.proof_payments: _Endpoint(
        "/api/proof-payments/snapshot",
        FetchMode.SNAPSHOT,
        ["code"],
        created_from="creationTimeFrom",
        created_to="creationTimeTo",
        changed_from="changeTimeFrom",
        children=_DOCUMENT_CHILDREN,
        parent_prefix="proof_payment",
    ),
    ObjectType.abandoned_carts: _Endpoint(
        "/api/abandoned-carts/snapshot",
        FetchMode.SNAPSHOT,
        ["guid"],
        # Abandoned carts are filtered by last visit, not by creation.
        created_from="visitTimeFrom",
        created_to="visitTimeTo",
        changed_from="visitTimeFrom",
        children=(_Child("items", "items"),),
        parent_prefix="cart",
    ),
    # ------------------------------------------------------------ change feeds
    ObjectType.orders_changes: _changes_endpoint("/api/orders/changes", 1000),
    ObjectType.products_changes: _changes_endpoint("/api/products/changes", 1000),
    ObjectType.customers_changes: _changes_endpoint("/api/customers/changes", 20),
    ObjectType.invoices_changes: _changes_endpoint("/api/invoices/changes", 20),
    ObjectType.proforma_invoices_changes: _changes_endpoint("/api/proforma-invoices/changes", 20),
    ObjectType.credit_notes_changes: _changes_endpoint("/api/credit-notes/changes", 20),
    ObjectType.proof_payments_changes: _changes_endpoint("/api/proof-payments/changes", 20),
    # -------------------------------------------------------------------- stock
    ObjectType.stocks: _Endpoint("/api/stocks", FetchMode.LIST, ["id"], data_key="stocks"),
    ObjectType.stock_supplies: _Endpoint(
        "/api/stocks/{stock_id}/supplies",
        FetchMode.PER_STOCK,
        ["stock_id", "productGuid", "code"],
        data_key="supplies",
        changed_from="changedFrom",
        items_per_page=1000,
    ),
    ObjectType.stock_movements: _Endpoint(
        "/api/stocks/{stock_id}/movements",
        FetchMode.PER_STOCK,
        ["stock_id", "id"],
        data_key="movements",
        items_per_page=1000,
    ),
    # -------------------------------------------------- catalogue & reference
    ObjectType.categories: _Endpoint(
        "/api/categories", FetchMode.PAGINATED, ["guid"], data_key="categories", items_per_page=1000
    ),
    ObjectType.parametric_categories: _Endpoint(
        "/api/parametric-categories", FetchMode.PAGINATED, ["guid"], data_key="parametricCategories", items_per_page=100
    ),
    ObjectType.brands: _Endpoint("/api/brands", FetchMode.PAGINATED, ["code"], data_key="brands", items_per_page=1000),
    ObjectType.suppliers: _Endpoint(
        "/api/suppliers", FetchMode.PAGINATED, ["guid"], data_key="suppliers", items_per_page=500
    ),
    ObjectType.price_lists: _Endpoint("/api/pricelists", FetchMode.LIST, ["id"], data_key="pricelists"),
    ObjectType.product_availabilities: _Endpoint(
        "/api/products/availabilities", FetchMode.LIST, ["id"], data_key="availabilities"
    ),
    ObjectType.product_flags: _Endpoint("/api/products/flags", FetchMode.LIST, ["code"], data_key="flags"),
    ObjectType.product_units: _Endpoint("/api/products/units", FetchMode.LIST, ["id"], data_key="units"),
    ObjectType.product_measure_units: _Endpoint(
        "/api/products/measure-units", FetchMode.LIST, ["id"], data_key="measureUnits"
    ),
    ObjectType.product_warranties: _Endpoint("/api/products/warranties", FetchMode.LIST, ["id"], data_key="warranties"),
    ObjectType.filtering_parameters: _Endpoint(
        "/api/products/filtering-parameters",
        FetchMode.PAGINATED,
        ["code"],
        data_key="filteringParameters",
        children=(_Child("values", "values"),),
        parent_prefix="parameter",
        items_per_page=100,
    ),
    ObjectType.variant_parameters: _Endpoint(
        "/api/products/variant-parameters",
        FetchMode.PAGINATED,
        ["id"],
        data_key="parameters",
        children=(_Child("values", "values"),),
        parent_prefix="parameter",
        items_per_page=100,
    ),
    ObjectType.surcharge_parameters: _Endpoint(
        "/api/products/surcharge-parameters",
        FetchMode.PAGINATED,
        ["code"],
        data_key="surchargeParameters",
        children=(_Child("values", "values"),),
        parent_prefix="parameter",
        items_per_page=500,
    ),
    ObjectType.recycling_fee_categories: _Endpoint(
        "/api/products/recycling-fee-categories", FetchMode.LIST, ["id"], data_key="recyclingFeeCategories"
    ),
    ObjectType.consumption_taxes: _Endpoint(
        "/api/products/consumption-taxes", FetchMode.LIST, ["id"], data_key="consumptionTaxes"
    ),
    # -------------------------------------------------- order & customer refs
    ObjectType.order_statuses: _Endpoint("/api/orders/statuses", FetchMode.LIST, ["id"], data_key="statuses"),
    ObjectType.order_sources: _Endpoint("/api/orders/sources", FetchMode.LIST, ["id"], data_key="sources"),
    ObjectType.shipping_methods: _Endpoint(
        "/api/shipping-methods", FetchMode.LIST, ["guid"], data_key="shippingMethods"
    ),
    ObjectType.payment_methods: _Endpoint("/api/payment-methods", FetchMode.LIST, ["guid"], data_key="paymentMethods"),
    ObjectType.customer_groups: _Endpoint("/api/customers/groups", FetchMode.LIST, ["guid"], data_key="customerGroups"),
    ObjectType.customer_regions: _Endpoint("/api/customers/regions", FetchMode.LIST, ["id"], data_key="regions"),
    # ---------------------------------------------------- marketing & content
    ObjectType.reviews_products: _Endpoint(
        "/api/reviews/products", FetchMode.PAGINATED, ["id"], data_key="reviews", items_per_page=20
    ),
    ObjectType.reviews_project: _Endpoint(
        "/api/reviews/project", FetchMode.PAGINATED, ["id"], data_key="reviews", items_per_page=200
    ),
    ObjectType.discount_coupons: _Endpoint(
        "/api/discount-coupons", FetchMode.PAGINATED, ["code"], data_key="coupons", items_per_page=1000
    ),
    ObjectType.quantity_discounts: _Endpoint(
        "/api/quantity-discounts", FetchMode.PAGINATED, ["id"], data_key="discounts", items_per_page=20
    ),
    ObjectType.volume_discounts: _Endpoint(
        "/api/volume-discounts", FetchMode.PAGINATED, ["id"], data_key="discounts", items_per_page=20
    ),
    ObjectType.xy_discounts: _Endpoint(
        "/api/xy-discounts", FetchMode.PAGINATED, ["id"], data_key="discounts", items_per_page=20
    ),
    ObjectType.mailing_lists: _Endpoint("/api/mailing-lists", FetchMode.LIST, ["code"], data_key="mailingLists"),
    ObjectType.unsubscribed_emails: _Endpoint(
        "/api/unsubscribed-emails", FetchMode.PAGINATED, ["email"], data_key="unsubscribedEmails", items_per_page=1000
    ),
    ObjectType.sales_channels: _Endpoint("/api/sales-channels", FetchMode.LIST, ["guid"], data_key="salesChannels"),
    ObjectType.articles: _Endpoint(
        "/api/articles", FetchMode.PAGINATED, ["id"], data_key="articles", items_per_page=10
    ),
    ObjectType.article_sections: _Endpoint("/api/articles/sections", FetchMode.LIST, ["id"], data_key="sections"),
    ObjectType.pages: _Endpoint("/api/pages", FetchMode.PAGINATED, ["id"], data_key="pages", items_per_page=20),
    ObjectType.discussion_posts: _Endpoint(
        "/api/discussions-posts", FetchMode.PAGINATED, ["id"], data_key="discussion", items_per_page=100
    ),
    # ------------------------------------------------------------------ single
    # /api/eshop returns one settings document with no id of its own, so it has no
    # primary key: a single-row table is always fully overwritten.
    ObjectType.eshop: _Endpoint("/api/eshop", FetchMode.SINGLE, []),
}


def _merge_kind(previous: str | None, current: str) -> str:
    """Narrowest native kind that fits both observed kinds for a column."""
    if previous is None or previous == current:
        return current
    if {previous, current} == {"integer", "numeric"}:
        return "numeric"
    return "string"


class _TableWriter:
    """Spools rows to a temp NDJSON file while tracking the column superset.

    Keeps memory bounded on large pulls (a full product snapshot is easily
    hundreds of thousands of records): rows are appended to /tmp as they arrive,
    then streamed back into the output CSV once the whole column set is known.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: dict[str, None] = {}  # insertion-ordered set of every key seen
        self._kinds: dict[str, str] = {}
        self.count = 0
        # Deliberately not a context manager: the handle stays open for the whole
        # extraction and is released by close() in Component.run()'s finally block.
        self._fh = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w",
            prefix=f"{name}_",
            suffix=".ndjson",
            dir=tempfile.gettempdir(),
            delete=False,
            encoding="utf-8",
            newline="",
        )
        self.path = Path(self._fh.name)

    def write(self, row: dict[str, Any]) -> None:
        for key, value in row.items():
            self.columns.setdefault(key, None)
            self._observe_kind(key, value)
        self._fh.write(json.dumps(row, ensure_ascii=False))
        self._fh.write("\n")
        self.count += 1

    def _observe_kind(self, key: str, value: Any) -> None:
        if value is None or value == "":
            return  # empty cells carry no type information
        if isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, int):
            kind = "integer"
        elif isinstance(value, float):
            kind = "numeric"
        else:
            kind = "string"
        self._kinds[key] = _merge_kind(self._kinds.get(key), kind)

    def kind(self, column: str) -> str:
        return self._kinds.get(column, "string")

    def rows(self) -> Iterator[dict[str, Any]]:
        self._fh.flush()
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def close(self) -> None:
        self._fh.close()
        self.path.unlink(missing_ok=True)


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()
        # Parse only the connection settings here so the sync actions share one
        # client and never trip over an unrelated extraction-settings problem.
        credentials = Credentials(**self.configuration.parameters)
        use_private = credentials.auth_type == AuthType.private_token
        self._client = ShoptetClient(
            private_api_token=credentials.private_api_token if use_private else None,
            oauth_access_token=None if use_private else credentials.oauth_access_token,
            oauth_token_url=None if use_private else credentials.oauth_token_url,
            base_url=credentials.api_base_url or BASE_URL,
        )

    # ------------------------------------------------------------------- run

    def run(self) -> None:
        cfg = Configuration(**self.configuration.parameters)
        assert cfg.object is not None  # guaranteed by Configuration validation
        endpoint = _REGISTRY[cfg.object]
        self._validate_include(cfg, endpoint)

        # Take the watermark before fetching, so a record edited mid-run is
        # re-fetched next time instead of being skipped.
        run_started_at = datetime.now(UTC)
        previous_state = self.get_state_file() or {}

        writers: dict[str, _TableWriter] = {}
        try:
            parent_writer = self._writer(writers, cfg.object.value)
            for record in self._iter_records(cfg, endpoint, previous_state):
                self._write_record(record, cfg, endpoint, parent_writer, writers)
            for writer in writers.values():
                primary_key = (
                    endpoint.primary_key if writer.name == cfg.object.value else self._child_primary_key(endpoint)
                )
                self._finalize_table(writer, primary_key, cfg.incremental)
        finally:
            for writer in writers.values():
                writer.close()

        self.write_state_file({_STATE_LAST_RUN: run_started_at.strftime(_API_DATETIME_FORMAT)})
        logger.info("Extraction of '%s' finished.", cfg.object.value)

    # -------------------------------------------------------------- fetching

    def _iter_records(
        self, cfg: Configuration, endpoint: _Endpoint, previous_state: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        params = self._build_params(cfg, endpoint, previous_state)

        if endpoint.mode == FetchMode.SNAPSHOT:
            yield from self._client.iter_snapshot(endpoint.path, params)
        elif endpoint.mode == FetchMode.PAGINATED:
            assert endpoint.data_key is not None
            yield from self._client.iter_paginated(endpoint.path, endpoint.data_key, params)
        elif endpoint.mode == FetchMode.LIST:
            assert endpoint.data_key is not None
            yield from self._client.iter_list(endpoint.path, endpoint.data_key, params)
        elif endpoint.mode == FetchMode.SINGLE:
            record = self._client.get_single(endpoint.path, endpoint.data_key, params)
            if record:
                yield record
        elif endpoint.mode == FetchMode.PER_STOCK:
            yield from self._iter_per_stock(cfg, endpoint, params)
        else:  # pragma: no cover - unreachable, the enum is exhaustive above
            raise UserException(f"Unsupported fetch mode for object '{cfg.object}'.")

    def _iter_per_stock(
        self, cfg: Configuration, endpoint: _Endpoint, params: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """Read a per-stock collection for one stock, or for every stock.

        Stock ids are not knowable from the configuration alone, so unless the
        user pinned one we resolve them from ``/api/stocks`` first. The stock id
        is stamped onto each row: it is part of the primary key and the API does
        not repeat it in the payload.
        """
        stock_ids = [cfg.stock_id] if cfg.stock_id else self._all_stock_ids()
        if not stock_ids:
            logger.warning("The e-shop reports no stocks; nothing to extract.")
            return
        assert endpoint.data_key is not None
        for stock_id in stock_ids:
            path = endpoint.path.format(stock_id=stock_id)
            logger.info("Reading %s for stock %s.", endpoint.data_key, stock_id)
            for record in self._client.iter_paginated(path, endpoint.data_key, params):
                yield {"stock_id": stock_id, **record}

    def _all_stock_ids(self) -> list[str]:
        return [str(stock["id"]) for stock in self._client.iter_list("/api/stocks", "stocks") if stock.get("id")]

    def _build_params(self, cfg: Configuration, endpoint: _Endpoint, previous_state: dict[str, Any]) -> dict[str, Any]:
        """Assemble the query string for one object.

        Only parameters the endpoint actually declares are sent — Shoptet rejects
        unknown query parameters, and the filter names differ per collection
        (``creationTimeFrom`` for documents, ``visitTimeFrom`` for abandoned
        carts, ``from`` for change feeds).
        """
        params: dict[str, Any] = dict(endpoint.extra_params)

        date_from = self._parse_datetime(cfg.date_range.date_from)
        date_to = self._parse_datetime(cfg.date_range.date_to)
        if date_from and endpoint.created_from:
            params[endpoint.created_from] = date_from.strftime(_API_DATETIME_FORMAT)
        if date_to and endpoint.created_to:
            params[endpoint.created_to] = date_to.strftime(_API_DATETIME_FORMAT)

        if endpoint.changed_from:
            since = self._incremental_since(cfg, endpoint, previous_state, date_from)
            if since:
                params[endpoint.changed_from] = since.strftime(_API_DATETIME_FORMAT)

        if endpoint.include_options and cfg.include:
            params["include"] = ",".join(cfg.include)
        if endpoint.items_per_page:
            params["itemsPerPage"] = endpoint.items_per_page
        return params

    def _incremental_since(
        self,
        cfg: Configuration,
        endpoint: _Endpoint,
        previous_state: dict[str, Any],
        date_from: datetime | None,
    ) -> datetime | None:
        """Lower bound of the change window, or ``None`` for an unfiltered pull.

        The watermark is pulled back by ``lookback_hours`` so a record edited in
        the seconds around the previous run's cut-off is picked up again rather
        than falling between two runs.
        """
        is_changes_feed = endpoint.changed_from == "from"
        if not cfg.incremental and not is_changes_feed:
            return None

        watermark = self._parse_datetime(previous_state.get(_STATE_LAST_RUN))
        if watermark:
            return watermark - timedelta(hours=cfg.lookback_hours)
        if date_from:
            return date_from
        if is_changes_feed:
            # `from` is mandatory on the change feeds, so a first run has to pick
            # some window rather than fail.
            logger.info(
                "No previous state for a change feed; reading the last %d days of changes.",
                _CHANGES_DEFAULT_WINDOW_DAYS,
            )
            return datetime.now(UTC) - timedelta(days=_CHANGES_DEFAULT_WINDOW_DAYS)
        logger.info("No previous state found; performing a full load.")
        return None

    @staticmethod
    def _validate_include(cfg: Configuration, endpoint: _Endpoint) -> None:
        if not cfg.include:
            return
        if not endpoint.include_options:
            logger.warning(
                "Object '%s' does not support optional sections; ignoring the 'include' setting.", cfg.object
            )
            return
        unknown = [section for section in cfg.include if section not in endpoint.include_options]
        if unknown:
            raise UserException(
                f"Unknown section(s) {', '.join(unknown)} for object '{cfg.object}'. "
                f"Supported sections: {', '.join(endpoint.include_options)}."
            )

    # --------------------------------------------------------------- writing

    def _write_record(
        self,
        record: dict[str, Any],
        cfg: Configuration,
        endpoint: _Endpoint,
        parent_writer: _TableWriter,
        writers: dict[str, _TableWriter],
    ) -> None:
        """Write one API record as a parent row plus any child rows."""
        assert cfg.object is not None
        remaining = dict(record)
        if cfg.extract_child_tables:
            parent_keys = {self._parent_column(endpoint, key): record.get(key) for key in endpoint.primary_key}
            for child in endpoint.children:
                rows = remaining.pop(child.field, None)
                if not isinstance(rows, list) or not rows:
                    continue
                child_writer = self._writer(writers, f"{cfg.object.value}_{child.suffix}")
                for index, child_row in enumerate(rows, start=1):
                    if not isinstance(child_row, dict):
                        # A scalar array (e.g. a list of codes) still deserves a row.
                        child_row = {"value": child_row}
                    child_writer.write({**parent_keys, _ROW_NUMBER_COLUMN: index, **self._flatten(child_row)})
        parent_writer.write(self._flatten(remaining))

    @staticmethod
    def _writer(writers: dict[str, _TableWriter], name: str) -> _TableWriter:
        writer = writers.get(name)
        if writer is None:
            writer = _TableWriter(name)
            writers[name] = writer
        return writer

    @staticmethod
    def _parent_column(endpoint: _Endpoint, key: str) -> str:
        prefix = endpoint.parent_prefix
        return f"{prefix}_{key}" if prefix else key

    def _child_primary_key(self, endpoint: _Endpoint) -> list[str]:
        return [self._parent_column(endpoint, key) for key in endpoint.primary_key] + [_ROW_NUMBER_COLUMN]

    @staticmethod
    def _flatten(record: dict[str, Any]) -> dict[str, Any]:
        """Flatten one record; nested objects and lists become JSON-string columns.

        Deliberate: nested shapes vary a lot across the Shoptet surface (an order
        carries ``price``, ``billingAddress``, ``notes``, … each with its own
        optional fields), so exploding them into dotted columns would produce a
        wide, sparse, unstable schema. Arrays worth their own rows are already
        split into child tables before this runs; whatever is left keeps a stable
        column set and stays parseable downstream.
        """
        return {
            key: (json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)
            for key, value in record.items()
        }

    def _finalize_table(self, writer: _TableWriter, primary_key: list[str], incremental: bool) -> None:
        if writer.count == 0:
            logger.info("No rows for %s; skipping table.", writer.name)
            return
        # Only keep primary-key columns the data actually produced: an object
        # whose id field is absent (a disabled module, a filtered response) would
        # otherwise fail the storage load on a missing column.
        effective_pk = [column for column in primary_key if column in writer.columns]
        if len(effective_pk) != len(primary_key):
            missing = sorted(set(primary_key) - set(effective_pk))
            logger.warning(
                "Primary-key column(s) %s are absent from %s; loading without them.", ", ".join(missing), writer.name
            )
        columns = self._order_columns(writer.columns, effective_pk)
        schema = {col: ColumnDefinition(data_types=self._base_type_for(col, writer.kind(col))) for col in columns}
        table = self.create_out_table_definition(
            f"{writer.name}.csv",
            primary_key=effective_pk,
            incremental=incremental and bool(effective_pk),
            schema=schema,
        )
        pk_columns = set(effective_pk)
        # Headerless CSV: `schema` is authoritative for the column names, so a
        # header row would be imported as data.
        with open(table.full_path, "w", encoding="utf-8", newline="") as fh:
            csv_writer = csv.writer(fh)
            for row in writer.rows():
                csv_writer.writerow([self._serialize_cell(row.get(col, ""), col in pk_columns) for col in columns])
        self.write_manifest(table)
        logger.info("Wrote %d rows to %s.", writer.count, writer.name)

    @staticmethod
    def _base_type_for(name: str, kind: str) -> BaseType:
        lowered = name.lower()
        if kind == "string" and any(hint in lowered for hint in _TIMESTAMP_COLUMN_HINTS):
            # creationTime / changeTime / taxDate / visitTime … are ISO strings.
            return BaseType.timestamp()
        if kind == "integer":
            return BaseType.integer()
        if kind == "numeric":
            return BaseType.numeric()
        if kind == "boolean":
            return BaseType.boolean()
        return BaseType.string()

    @staticmethod
    def _order_columns(columns: Iterable[str], primary_key: list[str]) -> list[str]:
        # Deterministic order: primary key first, then the rest alphabetically.
        seen = set(primary_key)
        return list(primary_key) + sorted(column for column in columns if column not in seen)

    @staticmethod
    def _serialize_cell(value: object, is_primary_key: bool) -> object:
        if value is None:
            value = ""
        elif isinstance(value, bool):
            value = "true" if value else "false"
        if is_primary_key and value == "":
            return _EMPTY_PK_PLACEHOLDER
        return value

    # ----------------------------------------------------------------- utils

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        """Parse an absolute or relative date into an aware UTC datetime."""
        if not value:
            return None
        parsed = dateparser.parse(str(value), settings={"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "UTC"})
        if parsed is None:
            raise UserException(
                f"Could not parse date '{value}'. Use YYYY-MM-DD, a full ISO timestamp, "
                f"or a relative expression such as 'yesterday' or '30 days ago'."
            )
        return parsed.astimezone(UTC)

    # --------------------------------------------------------- sync actions

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        try:
            eshop = self._client.get_eshop_info()
        except UserException as err:
            return ValidationResult(f"Connection failed: {err}", MessageType.DANGER)
        contact = eshop.get("contactInformation") or {}
        name = contact.get("eshopName") or contact.get("companyName") or eshop.get("projectId") or "the e-shop"
        return ValidationResult(f"Connection to {name} succeeded.", MessageType.SUCCESS)

    @sync_action("listStocks")
    def list_stocks(self) -> list[SelectElement]:
        return [
            SelectElement(value=str(stock["id"]), label=str(stock.get("title") or stock.get("name") or stock["id"]))
            for stock in self._client.iter_list("/api/stocks", "stocks")
            if stock.get("id") is not None
        ]

    @sync_action("listPriceLists")
    def list_price_lists(self) -> list[SelectElement]:
        return [
            SelectElement(value=str(pricelist["id"]), label=str(pricelist.get("name") or pricelist["id"]))
            for pricelist in self._client.iter_list("/api/pricelists", "pricelists")
            if pricelist.get("id") is not None
        ]

    @sync_action("listIncludeSections")
    def list_include_sections(self) -> list[SelectElement]:
        """Optional snapshot sections available for the object chosen in this row.

        Driven off the registry rather than hardcoded in the schema: the sections
        differ per collection (orders has six, invoices one, most objects none),
        so a static list in the UI would offer sections the API would reject.
        """
        raw_object = self.configuration.parameters.get("object")
        if not raw_object:
            return []
        try:
            endpoint = _REGISTRY[ObjectType(raw_object)]
        except KeyError, ValueError:
            return []
        return [SelectElement(value=section, label=section) for section in endpoint.include_options]

    @sync_action("listApprovedEndpoints")
    def list_approved_endpoints(self) -> list[SelectElement]:
        """What this token may actually read.

        A private API token only carries the endpoint groups the merchant granted
        it, and an addon only the ones Shoptet approved, so this is the quickest
        way to see why an object returns 403 on one e-shop and works on another.
        """
        return [SelectElement(value=name, label=name) for name in self._client.list_approved_endpoints()]


if __name__ == "__main__":
    try:
        component = Component()
        component.execute_action()
    except UserException as exc:
        # Log the message itself, not just a traceback: the platform surfaces the
        # last log line to the user, and a bare logger.exception() hides it.
        logger.error(str(exc))
        sys.exit(1)
    except Exception:
        logger.exception("Component failed with an unexpected error")
        sys.exit(2)
