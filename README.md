Shoptet (API)
=============

Extracts e-shop data from [Shoptet](https://www.shoptet.cz/) through the official
[Shoptet REST API](https://api.docs.shoptet.com/).

This is the API-based successor to the **Shoptet Permalink** data source
(`kds-team.ex-shoptet-permalink`), which downloads CSV exports from permalink URLs.
The API gives full order/product/customer detail instead of the flat export columns,
supports server-side incremental filtering, and does not depend on permalinks that a
merchant can regenerate.

**Table of Contents:**

[TOC]

Functionality Notes
===================

Bulk collections (orders, products, customers, accounting documents) are read through
Shoptet's **asynchronous snapshot** endpoints: the component submits a snapshot request,
polls the job, and streams the resulting gzipped [JSON Lines](https://jsonlines.org/)
file. That is the only way to read a whole collection in one pass and it returns the same
detail as the per-record endpoints. Smaller collections and reference data are read
through the paginated list endpoints.

Shoptet rate-limits with a leaky bucket (200 drops, draining 10/s) and reports the fill
level on every response. The component slows itself down as the bucket fills instead of
running into `429`, and honours `Retry-After` when it happens anyway. Write locks (`423`)
and server errors are retried with exponential back-off.

Prerequisites
=============

The Shoptet API is not open to everyone. There are two ways in, and this component
supports both.

**1. Private API token — requires Shoptet Premium**

The merchant generates the token in the e-shop administration under
**Connections → Private API** (up to 10 tokens, no expiry) and grants it read rights for
the endpoint groups you want to extract. This is the simplest option: paste the token into
the configuration and you are done. It is only available on the
[Shoptet Premium](https://www.shoptetpremium.cz/api/) tariff.

**2. Addon OAuth token — requires being a Shoptet API partner**

On the other tariffs the API is only reachable by marketplace addons. The addon holds a
permanent OAuth access token per installed e-shop; the component exchanges it for the
30-minute API access token and refreshes it as needed. Becoming an API partner means
submitting an addon proposal to Shoptet and signing a contract — see
[Testing without a Shoptet account](#testing-without-a-shoptet-account) below.

Configuration
=============

Connection (configuration level)
--------------------------------

| Parameter               | Required                 | Description                                                                                     |
|-------------------------|--------------------------|-------------------------------------------------------------------------------------------------|
| `auth_type`             | yes                      | `private_token` (default) or `addon_oauth`.                                                     |
| `#private_api_token`    | with `private_token`     | Private API token from **Connections → Private API**.                                            |
| `#oauth_access_token`   | with `addon_oauth`       | The addon's permanent OAuth access token for this e-shop.                                        |
| `oauth_token_url`       | with `addon_oauth`       | E.g. `https://123456.myshoptet.com/action/ApiOAuthServer/getAccessToken`.                         |
| `api_base_url`          | no                       | Overrides the API host. Only for testing — see [Development](#development).                       |

Use the **Test connection** button to verify the credentials before adding rows.

Data to extract (row level)
---------------------------

| Parameter              | Default             | Description                                                                                                                     |
|------------------------|---------------------|---------------------------------------------------------------------------------------------------------------------------------|
| `object`               | —                   | What to extract; one object per row. See [Supported objects](#supported-objects).                                                |
| `load_type`            | `incremental_load`  | `incremental_load` upserts by primary key and asks Shoptet only for changed records where the endpoint supports it. `full_load` overwrites the table. |
| `lookback_hours`       | `24`                | How far before the previous run the change window starts, so records edited around the cut-off are not missed.                    |
| `date_range.date_from` | empty               | `YYYY-MM-DD`, a full ISO timestamp, or a relative expression (`30 days ago`). Applied where the endpoint has a date filter.       |
| `date_range.date_to`   | empty               | Upper bound of the same window.                                                                                                  |
| `include`              | `[]`                | Optional response sections Shoptet only sends on request (order `notes`, product `images`, …). Available values depend on `object`. |
| `stock_id`             | empty               | Restricts `stock_supplies` / `stock_movements` to one stock. Empty reads every stock.                                             |
| `extract_child_tables` | `true`              | Split nested arrays (order items, product variants, …) into their own tables. When `false` they stay as JSON strings.             |

### Deletions

Incremental loads upsert; they never remove rows. To detect deletions, either run a
`full_load`, or add a row for the matching **change feed** object (`orders_changes`,
`products_changes`, …), which returns `edit` and `delete` events since a timestamp.

Sample configuration
--------------------

```json
{
  "parameters": {
    "auth_type": "private_token",
    "#private_api_token": "your-private-api-token",
    "object": "orders",
    "load_type": "incremental_load",
    "lookback_hours": 24,
    "date_range": {
      "date_from": "30 days ago",
      "date_to": ""
    },
    "include": ["notes", "shippingDetails"],
    "extract_child_tables": true
  }
}
```

Supported objects
=================

| Group                | Objects                                                                                                                                                                                                                    |
|----------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Sales                | `orders`, `order_history`, `abandoned_carts`                                                                                                                                                                                |
| Catalogue            | `products`, `product_pricelist_prices`, `categories`, `parametric_categories`, `brands`, `suppliers`, `price_lists`                                                                                                          |
| Customers            | `customers`, `customer_groups`, `customer_regions`                                                                                                                                                                          |
| Accounting documents | `invoices`, `proforma_invoices`, `credit_notes`, `delivery_notes`, `proof_payments`                                                                                                                                          |
| Stock                | `stocks`, `stock_supplies`, `stock_movements`                                                                                                                                                                                |
| Marketing            | `discount_coupons`, `quantity_discounts`, `volume_discounts`, `xy_discounts`, `mailing_lists`, `unsubscribed_emails`, `reviews_products`, `reviews_project`                                                                   |
| Content              | `articles`, `article_sections`, `pages`, `discussion_posts`                                                                                                                                                                  |
| Reference data       | `order_statuses`, `order_sources`, `shipping_methods`, `payment_methods`, `product_availabilities`, `product_flags`, `product_units`, `product_measure_units`, `product_warranties`, `filtering_parameters`, `variant_parameters`, `surcharge_parameters`, `recycling_fee_categories`, `consumption_taxes`, `sales_channels`, `eshop` |
| Change feeds         | `orders_changes`, `products_changes`, `customers_changes`, `invoices_changes`, `proforma_invoices_changes`, `credit_notes_changes`, `proof_payments_changes`                                                                  |

If you need an endpoint that is not listed, submit a request to
[ideas.keboola.com](https://ideas.keboola.com/).

Output
======

Each row writes one table named after the object, plus a companion table per nested array.
Column names are the API field names; nested objects are stored as JSON strings. Manifests
carry an inferred schema (integer / numeric / boolean / timestamp / string).

| Table                       | Primary key                    |
|-----------------------------|--------------------------------|
| `orders`                    | `code`                         |
| `orders_items`              | `order_code`, `_row_number`    |
| `products`                  | `guid`                         |
| `products_variants`         | `product_guid`, `_row_number`  |
| `customers`                 | `guid`                         |
| `invoices`                  | `code`                         |
| `stock_supplies`            | `stock_id`, `productGuid`, `code` |
| `eshop`                     | none (single row, always overwritten) |

`_row_number` is the position of a child row inside its parent. Shoptet line items carry no
id of their own — an order item has only a product code, which can repeat within one order —
so the position is what identifies a row uniquely and reproducibly.

Because child rows are keyed by position, removing a line item from an existing order does
not delete its old row on an incremental load. Use a `full_load` for tables where that
matters.

Nested arrays are split into companion tables one level deep only. Two `products` `include`
options — `perStockAmounts` and `perPricelistPrices` — are documented as "amounts/prices per
individual stock/price list", but the fields they add actually live one level *deeper*, inside
each element of the `products_variants` table, not on the product record itself. Requesting
them does not create `products_stock_amounts` / `products_pricelist_prices` tables; it only
widens the JSON blob already stored in `products_variants`' nested-object columns.

`abandoned_carts` has no identifier field of any kind in the Shoptet API — not `guid`, not `id`,
nothing. It is always extracted as a full load, regardless of the row's `load_type` setting: an
incremental run would only be able to fetch the newest slice, and with no primary key to upsert
on, that slice would silently overwrite the whole table and discard every previously
accumulated cart. The job log explains this when it happens.

Testing without a Shoptet account
=================================

There is no self-service sandbox. Ranked by effort:

1. **Documentation mock server — free, no account, works today.** Shoptet's API reference is
   served with a mock at `https://api.docs.shoptet.com/_mock/shoptet-api/openapi`, which
   returns the documented example payloads for any dummy token. Set `api_base_url` to it and
   the component runs end to end against realistic responses for the paginated / list / single
   objects. Limitation: snapshot jobs return a `resultUrl` on a host that does not resolve, so
   no `SNAPSHOT`-mode object (orders, products, customers, all accounting documents, abandoned
   carts) can be exercised against it — only real Shoptet credentials can.
2. **Free trial e-shop — minutes, but no API.** A trial e-shop cannot install addons and has
   no Private API screen, so it cannot issue a token.
3. **Shoptet Premium e-shop — the private-token path.** Any Premium e-shop can generate a
   token immediately. Premium is priced from roughly 12 000 CZK/month, so this realistically
   means borrowing a token from a customer who already has it.
4. **API partner test e-shop — weeks.** Submit an addon proposal via the Shoptet marketplace
   form; Shoptet promises initial feedback within four weeks. On approval you sign a contract
   and receive a free test e-shop, an API Partner admin section, and 7-day test API tokens.
   This is the only route to a Shoptet-provided test environment, and the only route to a
   published marketplace addon.

### Current test coverage

`tests/test_client.py` (HTTP concerns — auth, throttling, retries, pagination, snapshot
polling — against a stub `requests.Session`), `tests/test_component.py` (config-in,
CSV-and-manifest-out, against the same stub session), `tests/test_configuration.py` and
`tests/test_registry_invariants.py` (every declared primary-key column and child field
checked against a vendored extract of the real Shoptet response schemas) run offline, with
no Shoptet account or mock-server access needed.

`tests/test_functional.py` runs a recorded, passing VCR suite (`tests/functional/`, cassettes
under `keboola.datadirtest.vcr.VCRDataDirTester`) against the documentation mock server
(`https://api.docs.shoptet.com/_mock/shoptet-api/openapi`) with a dummy `#private_api_token` —
real HTTP recordings, not hand-written cassettes. It covers:

- **All five sync actions** — `testConnection`, `listStocks`, `listPriceLists`,
  `listIncludeSections` (for `orders`), `listApprovedEndpoints`.
- **Every recordable fetch mode** — `PAGINATED` (`categories`, `brands`), `LIST` (`stocks`,
  `customer_groups`), `SINGLE` (`eshop`, whose empty primary key is by design), `PER_STOCK`
  (`stock_movements`, both fanned out over every stock and pinned via `stock_id`).
- **The child-table split** — `variant_parameters`' nested `values` array becomes
  `variant_parameters_values.csv` keyed by `parameter_id` + `_row_number`; a paired test with
  `extract_child_tables: false` proves the same array instead stays inline as a JSON-string
  column with no child table produced.
- **A change feed** (`orders_changes`) — one test proves the mandatory `from` window is
  synthesised on a first run with no previous state; a second, chained test
  (`tests/functional/15_16_orders_changes_chain/`) proves a previous run's real watermark minus
  `lookback_hours` reaches the `from` query string on the next run. It has to be a *chained*
  datadirtest (`out/state.json` of one sub-test threaded live into `in/state.json` of the next,
  at both record and replay time) rather than two independent tests with a committed
  `in/state.json` seed, because a standalone test's `in/state.json` is unconditionally reset to
  `{}` in `setUp` — a merely-committed seed would never actually reach the component on replay.
- **Four failure paths**, each asserted to fail before any HTTP call: missing credentials,
  a row with no `object` selected, an `include` section unknown to the chosen object, and
  `addon_oauth` without `oauth_token_url`.

**What it does not cover, and why:**

- **Every `SNAPSHOT`-mode object** — `orders`, `order_history`, `products`,
  `product_pricelist_prices`, `customers`, `invoices`, `proforma_invoices`, `credit_notes`,
  `delivery_notes`, `proof_payments`, `abandoned_carts` — cannot be recorded against the mock
  server (see the limitation above: the completed job's `resultUrl` points at a host that does
  not resolve, so the download fails after retries). Recording one anyway would mean either a
  functional test that reliably fails, or a hand-crafted cassette faking the JSONL download —
  neither is acceptable. These stay covered only by `tests/test_component.py`'s stubbed-session
  unit tests until a real Shoptet token is available.
- **`abandoned_carts`' `full_load_only` guard** (an incremental-load row is forced to a full
  load, with a warning) is not a functional test either, for the same reason: `abandoned_carts`
  is itself `SNAPSHOT`-mode, so recording it hits the unresolvable-`resultUrl` limit above before
  the guard's effect could ever be observed end-to-end. Covered by
  `tests/test_component.py`'s stubbed-session tests instead.
- **`stock_supplies`** is left out of the recorded suite for an unrelated reason: its primary
  key includes a `code` field, and `keboola.datadirtest`'s recording pipeline always layers its
  own baseline sanitizer — which redacts *any* JSON key literally named `code` (an
  OAuth-authorization-code heuristic) — ahead of whatever this component declares in
  `VCR_SANITIZERS`, corrupting that field in the cassette while the `expected/` output (captured
  from the live, unsanitized response at record time) keeps the real value, so replay
  deterministically diverges from `expected/`. `stock_movements` (no `code` field) is used for
  both `PER_STOCK` variants instead. `eshop` and `orders_changes` hit the same collision on
  fields that could not be swapped out (`currencies[].code`/`languages[].code`, and the change
  feed's own identifier) — see `tests/setup/record_code_safe.py` for the sanitizer-patch
  workaround used to record those two anyway (this component's OAuth flow never carries a
  real OAuth "code" grant parameter, so excluding "code" from the redacted-field set for
  recording never risks leaking an actual secret).
- Only a representative object was recorded per fetch mode / change feed, not all ~56
  `ObjectType` values — the remaining `PAGINATED`/`LIST` reference-data objects share the exact
  same code paths as the ones recorded here and are already schema-checked by
  `tests/test_registry_invariants.py`.

**Once a real Shoptet token (private API or addon OAuth) is available:** put it in a
gitignored `secrets.json` (see `references/vcr-quickstart.md` in the `component-test` skill for
the format), point `api_base_url` at the real e-shop or drop it for the production default, and
re-record the missing objects with
`uv run python -m keboola.datadirtest scaffold --secrets secrets.json --regenerate`. The
`SNAPSHOT` objects and `abandoned_carts`' guard can then be recorded normally — a real e-shop's
`resultUrl` resolves. `stock_supplies`, `eshop` and `orders_changes` will still need the
`record_code_safe.py` sanitizer patch (or a fix upstream in `keboola.vcr`'s baseline
sanitizer), since the `code`-field collision is unrelated to credential authenticity.

Development
-----------

Local development uses [uv](https://docs.astral.sh/uv/):

```
uv sync --all-groups
uv run pytest
uv run ruff check src/ tests/
uv run ty check
```

Run the component against the documentation mock server — no Shoptet account needed:

```
KBC_DATADIR=./data uv run python src/component.py
```

with `data/config.json` containing:

```json
{
  "parameters": {
    "auth_type": "private_token",
    "#private_api_token": "mock",
    "api_base_url": "https://api.docs.shoptet.com/_mock/shoptet-api/openapi",
    "object": "categories",
    "load_type": "full_load"
  }
}
```

To customize the local data folder path, replace the `CUSTOM_FOLDER` placeholder with your
desired path in the `docker-compose.yml` file:

```
    volumes:
      - ./:/code
      - ./CUSTOM_FOLDER:/data
```

Clone this repository, initialize the workspace, and run the component:

```
git clone https://github.com/keboola/component-ex-shoptet-api
cd component-ex-shoptet-api
docker-compose build
docker-compose run --rm dev
```

Run the test suite and lint checks:

```
docker-compose run --rm test
```

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
