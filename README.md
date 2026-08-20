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

Testing without a Shoptet account
=================================

There is no self-service sandbox. Ranked by effort:

1. **Documentation mock server — free, no account, works today.** Shoptet's API reference is
   served with a mock at `https://api.docs.shoptet.com/_mock/shoptet-api/openapi`, which
   returns the documented example payloads for any dummy token. Set `api_base_url` to it and
   the component runs end to end against realistic responses. It is what the test suite is
   built on. Limitation: snapshot jobs return a `resultUrl` that does not exist, so only the
   paginated / list / single objects can be exercised this way.
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
