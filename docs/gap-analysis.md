# Gap Analysis — keboola.ex-shoptet-api vs. the Shoptet OpenAPI bundle

Every `_REGISTRY` entry in `src/component.py` was checked against the bundled OpenAPI 3.1 description
of the Shoptet API (209 paths, `/private/tmp/.../scratchpad/openapi.json`, authoritative over the HTML
docs per the task brief). This document lists every deviation found, in severity order, each with the
exact file:line, the OpenAPI evidence, and the concrete fix. A "what was checked and found correct"
section follows, so the scope of the check is auditable.

All line numbers refer to `src/component.py` as it exists at the time of this review.

---

## Blockers

### 1. `products_changes` / `customers_changes` — wrong identifier field in the primary key

**Status (Phase 3): RESOLVED.** `_changes_endpoint()` now takes an `id_field` parameter (`"code"` by default, `"guid"` for `products_changes`/`customers_changes`); the primary key is now `[id_field, "changeTime", "changeType"]` for all seven change feeds (see finding 9 — folded into the same fix). Verified against the bundle: `products_changes`/`customers_changes` responses have `guid`, not `code`.

**Where:** `src/component.py:311` (`ObjectType.products_changes: _changes_endpoint("/api/products/changes", 1000)`)
and `:312` (`ObjectType.customers_changes: _changes_endpoint("/api/customers/changes", 20)`), sharing the
helper at `:158-167`:

```python
def _changes_endpoint(path: str, items_per_page: int) -> _Endpoint:
    return _Endpoint(
        path, FetchMode.PAGINATED, ["code", "changeTime"],
        data_key="changes", changed_from="from", items_per_page=items_per_page,
    )
```

**Evidence:** the response schema for `GET /api/products/changes` and `GET /api/customers/changes`
(`data.changes[]`) has properties `guid`, `changeTime`, `changeType` — there is **no `code` field**.
(Contrast `GET /api/orders/changes`, `/api/invoices/changes`, `/api/proforma-invoices/changes`,
`/api/credit-notes/changes`, `/api/proof-payments/changes`, which *do* use `code`.) Verified directly
against `components.schemas` for each `*/changes` operation's `200` response.

**Impact:** `_finalize_table`'s defensive guard (`effective_pk = [c for c in primary_key if c in
writer.columns]`) drops the never-present `code` column, leaving the effective primary key as the
single column `changeTime` — which is (a) not unique (Shoptet's own doc for this endpoint says nothing
about `changeTime` being distinct across entities) and (b) itself nullable in the schema
(`type: ["string", "null"]`). Two different products/customers changing in the same run, or any change
with a null `changeTime`, silently collapse into one row on the incremental upsert — real, silent data
loss, not just a missing optimization.

**Fix:** give `_changes_endpoint()` an `id_field` parameter (default `"code"`, override to `"guid"` for
these two), e.g. `_changes_endpoint("/api/products/changes", 1000, id_field="guid")`, and use
`[id_field, "changeTime"]` as the primary key.

### 2. `brands` — declared primary key does not exist in the response

**Status (Phase 3): RESOLVED.** `brands`'s primary key is now `["guid"]`.

**Where:** `src/component.py:341`

```python
ObjectType.brands: _Endpoint("/api/brands", FetchMode.PAGINATED, ["code"], data_key="brands", items_per_page=1000),
```

**Evidence:** `GET /api/brands`'s `data.brands[]` item schema properties are `guid`, `name`, `indexName`,
`description`, `brandWeb`, `postalAddress`, `contactEmail`, … — **no `code` field anywhere.** The only
`code`-named thing in this API family is the path parameter of `GET /api/brands/{code}`, whose own
parameter description reads `"brand GUID"` — i.e. the path segment is (confusingly) named `code` but
is actually the brand's `guid`.

**Impact:** `code` never appears in `writer.columns`, so `effective_pk` is empty. Per
`_finalize_table`, `incremental=incremental and bool(effective_pk)` then forces the table to a full
overwrite on every run regardless of the row's `load_type`, with no primary key in the manifest at all
— any duplicate brand entries in a response would go unresolved, and the UI's incremental promise is
silently broken for this object.

**Fix:** `ObjectType.brands: _Endpoint("/api/brands", FetchMode.PAGINATED, ["guid"], data_key="brands", items_per_page=1000)`.

### 3. `customer_groups` — declared primary key does not exist in the response

**Status (Phase 3): RESOLVED.** `customer_groups`'s primary key is now `["id"]`.

**Where:** `src/component.py:395`

```python
ObjectType.customer_groups: _Endpoint("/api/customers/groups", FetchMode.LIST, ["guid"], data_key="customerGroups"),
```

**Evidence:** `GET /api/customers/groups`'s `data.customerGroups[]` item schema has no `guid` property
at all. Its properties are `id` (integer, required, non-nullable), `name`, `customerGroupCode`
(nullable), `description`, `defaultPricelistId`, `maxDiscount`, `priority`, `emailNotification`,
`authRequired`, `registrationAllowed`, `wholesale`, `tableLayout`, `fullProfileRequired`,
`defaultDueDays`, `defaultOrderStatusId`. `id` is the only required, non-nullable, unique-looking field.

**Impact:** same failure mode as #2 — `guid` never appears, `effective_pk` is empty, the table is
always fully overwritten with no primary key, and duplicate/changed groups across runs are not
deduplicated.

**Fix:** `ObjectType.customer_groups: _Endpoint("/api/customers/groups", FetchMode.LIST, ["id"], data_key="customerGroups")`.

### 4. `customers` — `deliveryAddresses` child table is permanently empty (wrong field name)

**Status (Phase 3): RESOLVED.** The child is now `_Child("deliveryAddress", "delivery_addresses")` — API field singular, output table suffix stays plural (it holds many rows); a code comment says so explicitly to stop a future "fix" back to the wrong plural spelling.

**Where:** `src/component.py:240`, inside the `customers` registry entry's `children` tuple:

```python
children=(
    _Child("accounts", "accounts"),
    _Child("deliveryAddresses", "delivery_addresses"),
    _Child("remarks", "remarks"),
),
```

**Evidence:** the `customerSnapshot` schema (merged via its `allOf` → `customer` base schema) has a
field named **`deliveryAddress`** (singular), `type: array`, described as `"delivery addresses"` —
there is no `deliveryAddresses` (plural) field anywhere in the schema. (`accounts` and `remarks` are
both correctly named and confirmed present as arrays; only the delivery-address field is misspelled.)

**Impact:** `_write_record`'s child-splitting loop does `rows = remaining.pop(child.field, None)` with
`child.field = "deliveryAddresses"`; this is always `None`, so the `if not isinstance(rows, list) or
not rows: continue` guard always skips it. The `customers_delivery_addresses` table — one of the more
operationally useful parts of the customer object for order-fulfilment analysis — is silently never
created, on every run, for every e-shop.

**Fix:** `_Child("deliveryAddress", "delivery_addresses")` (keep the output suffix; only the API field
name changes).

### 5. `abandoned_carts` — no identifier field exists at all; the current design loses history every run

**Status (Phase 3): RESOLVED.** `abandoned_carts`'s primary key is now `[]`, `changed_from` was removed entirely (the explicit "Date range" filter via `created_from`/`created_to` is kept — that is a user-chosen narrowing, not an automatic watermark), and a new `_Endpoint.full_load_only` flag (set for this object) forces `incremental=False` every run with a `logger.warning` explaining why, regardless of the row's `load_type`. `full_load_only` is general, not a special case for this object, and a registry-invariant test (`tests/test_registry_invariants.py`) asserts that any future keyless-plus-`changed_from` endpoint must set it too.

**Where:** `src/component.py:298-308`

```python
ObjectType.abandoned_carts: _Endpoint(
    "/api/abandoned-carts/snapshot",
    FetchMode.SNAPSHOT,
    ["guid"],
    created_from="visitTimeFrom",
    created_to="visitTimeTo",
    changed_from="visitTimeFrom",
    children=(_Child("items", "items"),),
    parent_prefix="cart",
),
```

**Evidence:** the `abandonedCartSnapshot` schema's full, `required` property list is `date`, `age`,
`items`, `cartValue`, `coupon`, `customer`, `returns`, `lastStep` — there is no `guid`, `id`, or any
other identifier field, at the top level or nested inside `customer` (`name`, `email`, `phone` only).
An abandoned cart, per this schema, is not a persistently-identified entity in the Shoptet API at all.

**Impact:** this is worse than #2/#3. Because `guid` never appears, `effective_pk` is empty, which
forces `incremental=False` in the output-mapping call regardless of the row's configured `load_type`.
But `abandoned_carts` *also* uses `visitTimeFrom`/`visitTimeTo` to narrow each incremental run's fetch
to "since the last watermark" (per `_incremental_since`). The combination — a date-windowed fetch
**and** a forced full-table overwrite — means every incremental run **replaces the entire table with
only the carts visited since the last run**, destroying every previously-accumulated abandoned cart.
A user who sets `load_type: incremental_load` (the row default) is silently getting a shrinking,
rolling window instead of an accumulating history — this is real data loss, not a missed
optimization, and it is the single most damaging finding in this review because the object's own
selling point (incremental cart-abandonment tracking for remarketing) is exactly what it breaks.

**Fix options, in order of preference:**
- Treat `abandoned_carts` as full-load-only (no incremental upsert is possible without a stable key):
  disable/hide `load_type=incremental_load` for this object in the UI, and always fetch the *entire*
  history (no `visitTimeFrom` filter) on every run, so the full overwrite is at least complete each
  time. Document the tradeoff (no dedup, full re-read every run) in the README.
- Or synthesize a best-effort composite key from the fields that exist (e.g. `date` + `customer.email`
  + `cartValue`), accepting that two genuinely distinct abandoned visits with identical values on the
  same timestamp would still collide — better than the current guaranteed data loss, but not exact.
- Either way, the current PK (`["guid"]`) must not stay as-is: it silently defeats the row's own
  `load_type` setting.

---

## Should-fix

### 6. `products` — `perStockAmounts` / `perPricelistPrices` children are declared one nesting level too shallow

**Status (Phase 3): RESOLVED (documented, not grandchild-split).** Went with fix option (b): the two dead child declarations (`perStockAmounts`, `perPricelistPrices` on `_PRODUCT_CHILDREN`) are removed — they never fired anyway, since the fields live inside `variants`, not on the product record — and the real behaviour is now documented (a code comment on `_PRODUCT_CHILDREN` plus a README paragraph under "Output"): requesting these two `include` sections only widens the JSON blob inside `products_variants`, it does not produce dedicated tables. Grandchild-of-a-child splitting (fix option (a)) is a bigger architectural change than this pass's scope; recorded as a candidate for a follow-up in `docs/design.md` §9 if the two fields turn out to matter enough to warrant it.

**Where:** `src/component.py:149-150`, inside `_PRODUCT_CHILDREN`:

```python
_Child("perStockAmounts", "stock_amounts"),
_Child("perPricelistPrices", "pricelist_prices"),
```

**Evidence:** the `productSnapshot` schema has no top-level `perStockAmounts` or `perPricelistPrices`
property. Both fields exist, but **inside each element of the `variants` array** (confirmed:
`productSnapshot.properties.variants.items.properties` includes both `perStockAmounts` and
`perPricelistPrices`, alongside `code`, `price`, `stock`, etc.). The `include` option table on
`GET /api/products/snapshot` documents them ("amounts/claims per individual stocks", "prices per
individual price lists") without specifying the nesting level, which is presumably how this was missed.

**Impact:** `remaining.pop("perStockAmounts", None)` / `remaining.pop("perPricelistPrices", None)` on
the *product* record are always `None`, so the dedicated `products_stock_amounts` /
`products_pricelist_prices` tables are never created. The underlying data is not entirely lost — because
`_flatten()` JSON-stringifies any nested list/dict it encounters, both fields survive as JSON-string
values inside the `products_variants` child table — but the two `include` options are effectively
false advertising: requesting them changes nothing observable in Storage beyond a wider JSON blob in an
existing column.

**Fix:** requires one more level of child-splitting than the current architecture supports (child of a
child). Either (a) extend `_write_record`/`_Child` to support grandchild extraction — declare
`perStockAmounts`/`perPricelistPrices` as children **of the `variants` child**, writing to
`products_variants_stock_amounts` / `products_variants_pricelist_prices` keyed by
(product PK, variant row number, own row number) — or (b) if that's too big a lift for this pass, at
minimum document the current, real behaviour ("these two `include` options only add JSON inside
`products_variants`; they do not produce separate tables") so the README's promise matches reality.

### 7. `product_pricelist_prices` — wrong primary key and a child-table declaration that can never fire

**Status (Phase 3): RESOLVED.** Primary key is now `["productGuid", "code"]`; the dead `children=(_Child("prices", "prices"),)` and the now-meaningless `parent_prefix="product"` were both removed, since this endpoint's rows are already flattened (one row per product × price list) with no parent/child relationship to express.

**Where:** `src/component.py:224-230`

```python
ObjectType.product_pricelist_prices: _Endpoint(
    "/api/products/snapshot/pricelists",
    FetchMode.SNAPSHOT,
    ["guid"],
    children=(_Child("prices", "prices"),),
    parent_prefix="product",
),
```

**Evidence:** the `productPricelistSnapshot` schema (`{productGuid, productId} allOf pricelistDetail`)
has **no top-level `guid` field** — the product identifier field is `productGuid`. Also, contrary to
the operation description ("each product taking one line ... its prices across all pricelists"), the
schema shows each JSONL record is actually **one (product × price-list) row already flattened**: the
merged-in `pricelistDetail` fields (`code` = the price list's own code, `currencyCode`, `price`,
`vatRate`, `orderableAmount`, `sales`) sit directly at the top level, not nested in an array. The
`prices` field that *does* exist at the top level (inherited from `pricelistDetail.prices`, schema
`productPrices`) is an unrelated, non-array object describing a preview "purchase price," not a
per-price-list breakdown array.

**Impact:** two independent problems. First, `guid` never appears → empty `effective_pk` → forced
full overwrite every run (harmless in isolation, since this is a whole-catalogue scan anyway, but the
row's `load_type=incremental_load` setting is silently ignored, same class of issue as #2/#3). Second,
the declared child `_Child("prices", "prices")`: `remaining.pop("prices", None)` returns the
non-array `purchasePrice`-shaped object, which fails `isinstance(rows, list)` and is silently skipped
— dead code, no data loss (the field would have been low-value anyway), but a table that's declared and
never populated is worth removing to avoid confusion.

**Fix:** `primary_key=["productGuid", "code"]` (product + the pricelist code this row's price applies
to), drop the `children=` declaration entirely (there is nothing to split), and adjust `parent_prefix`
usage accordingly since there is no longer a parent/child relationship for this object.

### 8. `order_history` — a solid `id` field exists but isn't used; the chosen key has a nullable half

**Status (Phase 3): RESOLVED, then amended.** Primary key is now `["orderCode", "id"]`, not `["orderCode", "creationTime"]`. The first Phase 3 pass used `["id"]` alone on the reading that `id` is globally unique, but the spec only calls it an "order history identifier" with example value `1`, and `orderHistorySnapshot` adds `orderCode` expressly to "identify the relation to the order" — both point to a per-order sequence. `id` alone would then merge every order's first remark into one row. The composite key is correct under either reading. `creationTime` stays out of the key because it is schema-nullable.

**Where:** `src/component.py:185-191`

```python
ObjectType.order_history: _Endpoint(
    "/api/orders/history/snapshot",
    FetchMode.SNAPSHOT,
    ["orderCode", "creationTime"],
    created_from="creationTimeFrom",
    created_to="creationTimeTo",
),
```

**Evidence:** `orderHistorySnapshot`'s properties are `id` (integer, **explicitly documented as "order
history identifier"**, required, non-nullable), `orderCode` (string, required, non-nullable),
`creationTime` (`type: ["string", "null"]` — nullable), `system`, `text`, `type`, `user`.

**Impact:** not a total break (both declared PK columns are at least present in the response, unlike
findings 1–5), but the second half of the composite key is schema-nullable, and the API hands you a
purpose-built, guaranteed-unique-looking `id` field that the registry ignores. Two remarks for the same
order with a null `creationTime` would collide on upsert.

**Fix:** `primary_key=["id"]` (or `["orderCode", "id"]` if per-order grouping in the key is wanted for
readability) instead of `["orderCode", "creationTime"]`.

### 9. All seven `*_changes` feeds — the `changeTime` half of the primary key is schema-nullable

**Status (Phase 3): RESOLVED — folded into finding 1's fix.** Went with design option (a) (an accumulating event log, the apparent original intent): the primary key for all seven change feeds is now `[identifier, "changeTime", "changeType"]`, not just `[identifier, "changeTime"]`. This was a blocker-level design decision handed down for this phase, not a judgement call made here.

**Where:** `src/component.py:310-316`, via the shared `_changes_endpoint()` helper (`:158-167`),
`primary_key=["code", "changeTime"]` (or `["guid", "changeTime"]` once #1 is fixed).

**Evidence:** for every one of the seven `*/changes` endpoints, `changeTime`'s schema is
`type: ["string", "null"]` (only `/api/proof-payments/changes` has a non-nullable `changeTime`, oddly
— confirmed by direct inspection of all seven response schemas). The operation description for
`GET /api/orders/changes` states *"Each order in the log is only mentioned with its last change"* —
i.e. within a single API response, the entity id is already unique; `changeTime` was added to the key
presumably to let the *component's own* multi-run incremental upsert keep one row per historical change
event rather than collapsing to "latest state per entity."

**Impact:** lower severity than #1 because it's an edge case (would require two null-`changeTime`
change events for the *same* entity, observed across *different* runs, since within one page the id is
already unique) rather than a routine collision. But it is unaddressed: nothing in the code guards
against it, and the nullability is real, not hypothetical, per the schema.

**Fix (design decision, not just a bug fix):** decide deliberately whether these tables should be (a)
an accumulating log of every distinct change event (current apparent intent — in which case, harden
against the null case, e.g. fall back to a monotonic counter when `changeTime` is null), or (b) a
"latest known change per entity" table (drop `changeTime` from the key entirely, upsert on `code`/`guid`
alone — arguably simpler and still supports the README's stated "detect deletions" use case via
`changeType == delete`). Either is defensible; the current state is neither deliberately.

### 10. `orders` — `payment_transactions` child likely never populates via the snapshot endpoint

**Status (Phase 3): DEFERRED, unchanged.** Still cannot be resolved without a live `/api/orders/snapshot` payload (the mock server cannot serve `SNAPSHOT`-mode objects at all — its `resultUrl` host does not resolve — and no real Shoptet credentials were available in this phase). Left `paymentTransactions` in `_ORDER_CHILDREN` per the finding's own recommendation ("preferably verify... if it can appear, no code change is needed"), with a code comment recording the uncertainty and the exact condition to check before removing it.

**Where:** `src/component.py:132` (`_Child("paymentTransactions", "payment_transactions")` inside
`_ORDER_CHILDREN`, used by the `orders` registry entry at `:174-184`).

**Evidence:** `orderSnapshot.paymentTransactions` exists as a field (type `array`) but is **not** in
the schema's `required` list (i.e., it's conditionally present). `GET /api/orders/snapshot`'s own
operation-level `include` table lists exactly six requestable sections — `notes`, `images`,
`shippingDetails`, `stockLocation`, `surchargeParameters`, `productFlags` — and `paymentTransactions`
is **not** one of them. (It *is* one of the seven sections documented for `GET /api/orders/{code}`, the
single-order detail endpoint — a different operation.) The registry's `include_options` for `orders`
correctly lists only the six real snapshot sections (verified exact match), so `paymentTransactions`
was never wired as a requestable `include` value — consistent with it not being requestable at all on
this endpoint.

**Impact:** low — no incorrect data, just an `orders_payment_transactions` table that is declared but,
per the spec, will most likely always report 0 rows and be skipped (`_finalize_table`'s
`if writer.count == 0: ... skipping table` path). Worth confirming with one live snapshot payload before
assuming it's truly always empty (the schema only says it's optional, not that it's snapshot-exclusive
unavailable) — flagged here as "likely," not "certain."

**Fix:** either remove `paymentTransactions` from `_ORDER_CHILDREN` (if a live payload confirms it's
never present on the snapshot), or — preferably — verify with one real `/api/orders/snapshot` run
whether the field ever appears unconditionally (some APIs include base fields regardless of `include`
and only *gate the detail depth* of already-present arrays); if it can appear, no code change is needed
beyond documenting that it isn't controllable via this endpoint's `include`.

### 11. `proof_payments` — declared `items` child has no matching field

**Status (Phase 3): RESOLVED.** `proof_payments` now declares no children at all (previously `children=_DOCUMENT_CHILDREN`).

**Where:** `src/component.py:288-297`, `children=_DOCUMENT_CHILDREN` (defined at `:153` as
`(_Child("items", "items"),)`), reused from the invoice/proforma/credit-note/delivery-note family.

**Evidence:** `proofPaymentSnapshot`'s full property list (49 fields — billing/bank details, `code`,
`isValid`, `payment`, `vatBreakdown`, `invoiceCode`, …) has **no `items` field**. Unlike invoices,
proforma invoices, credit notes and delivery notes (all genuinely line-itemized documents, all
confirmed to have `items: array`), a proof of payment is a payment receipt with no line items of its
own — reusing `_DOCUMENT_CHILDREN` for it was an unchecked generalization.

**Impact:** low — `proof_payments_items` is declared but will always report 0 rows and be skipped, same
harmless-dead-declaration pattern as #10.

**Fix:** give `proof_payments` `children=()` instead of `children=_DOCUMENT_CHILDREN`. (Optional
enhancement: `vatBreakdown`, which does exist on this object, might be worth its own child table if it
turns out to be an array — not verified here, out of scope for this pass.)

### 12. `discount_coupons` — an available creation-date filter isn't wired

**Status (Phase 3): RESOLVED.** `discount_coupons` now sets `created_from="creationTimeFrom", created_to="creationTimeTo"`.

**Where:** `src/component.py:404-406`

```python
ObjectType.discount_coupons: _Endpoint(
    "/api/discount-coupons", FetchMode.PAGINATED, ["code"], data_key="coupons", items_per_page=1000
),
```

**Evidence:** `GET /api/discount-coupons` (the paginated endpoint deliberately used here *instead of*
`/api/discount-coupons/snapshot`, because the snapshot variant requires `template` and `shippingPrice`
— a correct, already-documented design decision, see the code comment at the registry's module
docstring) accepts `creationTimeFrom` and `creationTimeTo` query parameters. The registry entry sets
neither `created_from` nor `created_to`.

**Impact:** low — the row-level "Date range" UI fields silently do nothing for `discount_coupons`, same
as for genuinely date-less reference data (which is documented, expected behaviour per the README).
The difference here is that this *specific* endpoint actually could honour the filter; it's a missed
narrowing opportunity, not a correctness bug (results are still complete, just not narrowed).

**Fix:** add `created_from="creationTimeFrom", created_to="creationTimeTo"` to the entry.

### 13. `stock_movements` — an available `changeTimeFrom` filter isn't wired

**Status (Phase 3): RESOLVED.** `stock_movements` now sets `changed_from="changeTimeFrom"`, matching the sibling `stock_supplies` entry's existing pattern.

**Where:** `src/component.py:327-333`

```python
ObjectType.stock_movements: _Endpoint(
    "/api/stocks/{stock_id}/movements",
    FetchMode.PER_STOCK,
    ["stock_id", "id"],
    data_key="movements",
    items_per_page=1000,
),
```

**Evidence:** `GET /api/stocks/{stockId}/movements` accepts a `changeTimeFrom` query parameter (confirmed
in the operation's parameter list, alongside `lastId`, `orderCode`, `include`). The registry entry sets
no `changed_from`.

**Impact:** the primary key (`stock_id`, `id`) is solid (both verified present, non-nullable), so this
is not a correctness bug — but every run of `stock_movements`, regardless of `load_type`, re-fetches the
*entire* movement history for every stock. On an e-shop with a long-running, high-churn stock ledger,
that is a real, avoidable cost (extra pages, extra rate-limit budget, longer job runtime) and it
silently defeats the row's own `load_type=incremental_load` setting (the fetch itself isn't narrowed,
even though the upsert on `id` is still correct once fetched).

**Fix:** add `changed_from="changeTimeFrom"` to the entry so `_incremental_since()` narrows the fetch on
incremental runs, matching what `stock_supplies` (the sibling `PER_STOCK` object) already does
correctly with `changedFrom`.

### 14. `categories` — `guid` is schema-nullable despite being a `required` key

**Status (Phase 3): RESOLVED (general hardening, no registry change).** No registry change was needed or made, per the finding's own conclusion. Implemented the suggested general hardening instead: `_finalize_table` now counts rows whose entire effective primary key serializes to the empty-PK placeholder and logs a warning if more than one such row exists for *any* table, not just `categories` — covered by `TestEmptyPrimaryKeyCollisions` in `tests/test_component.py`.

**Where:** `src/component.py:335-337`

```python
ObjectType.categories: _Endpoint(
    "/api/categories", FetchMode.PAGINATED, ["guid"], data_key="categories", items_per_page=1000
),
```

**Evidence:** `GET /api/categories`'s `data.categories[]` item schema lists `guid` in `required`, but
its type is `["string", "null"]` — i.e., the key must be present, but its value may legitimately be
`null` (OpenAPI 3.1 distinguishes "must appear" from "must not be null"; this is not a spec-quality
artifact, since sibling fields like `parametric-categories`' `guid` are `required` **and**
non-nullable `string`, so the nullability here is a deliberate schema choice, most plausibly to allow a
virtual/root category with no real guid).

**Impact:** low probability, real if it occurs — at most one category (a synthetic root) is plausible;
two categories with a simultaneously-null `guid` would collide via `_EMPTY_PK_PLACEHOLDER`
("`__empty__`") and one would silently overwrite the other.

**Fix:** no registry change needed (there is no better identifier documented for this object), but
worth a defensive integration-test assertion once real data is available: confirm at most one category
per e-shop has a null `guid`, and consider logging a warning if `_finalize_table` ever detects more than
one row collapsing onto `_EMPTY_PK_PLACEHOLDER` for any table (a general hardening, not specific to
`categories`).

---

## Nice-to-have

### 15. Valuable read endpoints the component doesn't expose

**Status (Phase 3): DEFERRED, as instructed.** Out of scope for this phase (adding new objects expands surface area and needs a schema enum entry, a UI label and test coverage of its own). Recorded in `docs/design.md` §9's new "Deferred: candidate objects for a follow-up" section so the decision stays visible.

Neither of these was declared "Excluded" with a recorded reason — they simply were never surfaced to
a user for a decision, which is exactly the silent-scope-narrowing failure mode the planning skill
exists to prevent:

- **`GET /api/shipments`** — shipment tracking (`guid`, `status`, `carrierAddress`, `serviceCode`,
  `packages[]`, `cod`, linked `orderCode`), filterable by `status`/`orderCode`, no snapshot but a plain
  list (needs a pagination/no-pagination check before adding — not fully verified in this pass). A
  Shoptet merchant analysing fulfilment/carrier performance would plausibly want this.
- **`GET /api/orders/claims`** — RMA / product-claim records, one row per (`orderCode`, `productCode`)
  with `statusId`, `amount`, `amountCompleted`; paginated, no blocking required params. No natural
  single-column PK, but (`orderCode`, `productCode`) is a plausible composite (not fully verified for
  uniqueness across repeated claims on the same product/order — would need confirmation before adding).

Both are read-only GETs with no mandatory-parameter blocker (the same class of check that correctly
excluded `discount-coupons/snapshot`). Recommendation: bring both to the user as an explicit Tier-C
scope question in a follow-up planning pass, rather than adding them unreviewed.

### 16. Small settings/reference endpoints not covered

**Status (Phase 3): DEFERRED, as instructed.** Same disposition and reasoning as finding 15; recorded in the same `docs/design.md` §9 section.

`GET /api/discount-coupons/templates` (coupon templates — referenced by the `template` field already
present on every `discount_coupons` row), `GET /api/eshop/customer-fields`, `GET /api/eshop/design`,
`GET /api/eshop/document-settings` (same family as the already-included `/api/eshop`), `GET
/api/reviews/settings`, `GET /api/discussions/settings`. Low value, cheap to add (all `SINGLE`-mode, no
params) — worth a mention in a future scope pass, not urgent.

### 17. `quantity_discounts` could use its `/snapshot` variant

**Status (Phase 3): DEFERRED, as instructed.** Same disposition and reasoning as finding 15; recorded in the same `docs/design.md` §9 section.

`GET /api/quantity-discounts/snapshot` exists and — unlike `discount-coupons/snapshot` — has **no**
required query parameters. The registry uses the plain paginated `/api/quantity-discounts` instead
(`items_per_page=20`, matching the documented max). Harmless today (this list is typically small), but
if it's ever expected to grow, the snapshot path would scale better. Not a bug, just a missed
opportunity noted for completeness.

### 18. Header capitalization cosmetic mismatch (not a functional issue)

**Status (Phase 3): RESOLVED (cosmetic, as instructed).** `src/client.py` now sends `Shoptet-Private-Api-Token`, matching the OpenAPI `securitySchemes` casing exactly; the module docstring, `component.py`'s VCR `_SENSITIVE_FIELDS` list, and the design doc were updated to match. No functional effect (HTTP header names are case-insensitive per RFC 7230, as already noted below), fixed only because the task brief asked for it explicitly.

The OpenAPI `securitySchemes` declare the private-token header as `Shoptet-Private-Api-Token`
(mixed-case "Api"); `src/client.py` sends `Shoptet-Private-API-Token` (capital "API"), and
`src/component.py`'s VCR sanitizer list uses the same capitalization as the client. HTTP header names
are case-insensitive per RFC 7230, so this has no functional effect and needs no fix — noted only so a
future reader doesn't mistake it for a bug.

### 19. `test_connection` sync action references two fields that don't exist

**Status (Phase 3): RESOLVED.** The two dead fallbacks (`contact.get("companyName")`, `eshop.get("projectId")`) are removed; `test_connection` now falls back straight from `contact.get("eshopName")` to the literal `"the e-shop"`.

**Where:** `src/component.py:819-821`

```python
contact = eshop.get("contactInformation") or {}
name = contact.get("eshopName") or contact.get("companyName") or eshop.get("projectId") or "the e-shop"
```

**Evidence:** `/api/eshop`'s `contactInformation` object has no `companyName` field (it has `eshopName`,
required, non-nullable), and `data` has no top-level `projectId` field at all (`billingInformation`
has `company`/`billingName`, if a company name were ever wanted as a fallback).

**Impact:** none in practice — `eshopName` is required and non-nullable, so the fallback chain never
reaches the two dead branches. Purely a code-cleanliness note.

**Fix (optional):** drop the two dead fallbacks, or replace `companyName` with the real
`billingInformation.company` field if a company-name fallback is actually wanted.

### 20. README claims a test suite built on the mock server; no functional test cases exist yet

**Status (Phase 3): RESOLVED.** `README.md`'s "Testing without a Shoptet account" section now states plainly that `tests/functional/` has zero recorded cassettes today, that recording them is a later phase's job, and that the mock server cannot serve any `SNAPSHOT`-mode object (its `resultUrl` host does not resolve) — so that path stays stub-tested via the unit tests until a real Shoptet token is available. A new `tests/test_registry_invariants.py` (vendoring a small, checked-in extract of the real API schemas) was also added this phase; it is described there, not claimed as the "mock-server-built" suite the README used to imply.

`README.md` states the documentation mock server "is what the test suite is built on," and
`tests/test_functional.py` wires up `keboola.datadirtest.vcr.VCRDataDirTester`, but `tests/functional/`
does not exist and `tests/setup/` is empty — there are zero recorded functional test cases today. Not
an OpenAPI-grounded finding, but adversarially worth flagging: the README's claim doesn't match the
repo's current state. Recording cassettes is Phase 3's job, not this phase's, but the discrepancy
should be fixed in one direction or the other (record the cassettes, or soften the README claim) before
release.

---

## What was checked and found correct (for scope transparency)

To make clear how much of the registry was actually exercised against the spec rather than assumed:

- **Every `items_per_page` value** in the registry (26 paginated/per-stock entries: 24 PAGINATED + 2 PER_STOCK) exactly matches the
  API's documented per-collection maximum (e.g. articles=10, reviews_products=20, categories=1000,
  suppliers=500, stock_supplies=1000, …) — a full match, zero discrepancies.
- **Every `include` menu** wired up (`orders`: 6 values; `products`: 20 values; `invoices` /
  `proforma_invoices` / `credit_notes`: 1 value each, `surchargeParameters`) exactly matches the
  operation-level documentation table for that specific endpoint, in the same order.
- **Fetch-mode-vs-capability**: all 18 `FetchMode.LIST` entries were confirmed to have *no*
  `page`/`itemsPerPage` parameters at all (so `LIST` is correct, not a truncated `PAGINATED`); all
  `FetchMode.PAGINATED` entries were confirmed to have both a `page` parameter and a `paginator` object
  in the response. No mode/capability mismatches found.
- **Required query parameters**: across every `SNAPSHOT` endpoint used, the only non-trivial required
  parameter anywhere is `Content-Type` (satisfied by the client's default session header) — except
  `/api/discount-coupons/snapshot`, which requires `template` and `shippingPrice`, correctly avoided by
  using the paginated `/api/discount-coupons` instead (the one instance the task brief pre-flagged, and
  the registry already gets it right). `/api/quantity-discounts/snapshot` was checked too and has *no*
  required parameters (see nice-to-have #17 — not a bug that it's unused, just an opportunity).
- **Change-feed `from` parameter**: all seven `*/changes` endpoints require `from`; `_incremental_since()`
  always supplies a value for them (watermark, configured `date_from`, or a 7-day fallback window on
  first run) — verified the code path guarantees this is never omitted.
- **Snapshot record shapes for orders/products/customers/documents**: `orderSnapshot`, `productSnapshot`,
  `customerSnapshot`, `invoiceSnapshot`, `proformaInvoiceSnapshot`, `creditNoteSnapshot`,
  `deliveryNoteSnapshot`, `proofPaymentSnapshot` all carry their declared primary-key field as
  `required` and non-nullable (`code` for the five document types, `guid` for products/customers).
  Order children `items`, `shippings`, `paymentMethods`, `completion` are all confirmed `type: array`
  on `orderSnapshot`.
- **Customer children** `accounts` and `remarks` (as opposed to the broken `deliveryAddresses`, #4) are
  both confirmed present, correctly named, `type: array`.
- **All plain `LIST`-mode reference/catalogue objects** (stocks, price lists, product availabilities,
  flags, units, measure units, warranties, recycling-fee categories, consumption taxes, order
  statuses/sources, shipping/payment methods, customer regions, mailing lists, sales channels, article
  sections) were checked for their declared primary key's presence, non-nullability, and required
  status — all correct except `customer_groups` (#3).
- **`eshop` (`FetchMode.SINGLE`, no primary key)** — confirmed the API genuinely returns one settings
  document with no id of its own; the deliberate "no PK, always overwritten" design is correct for this
  object.
- **`discussion_posts`'s oddly-named data key `discussion`** (singular, not "posts" or
  "discussionPosts") — confirmed exactly correct against the response schema (`data.discussion[]`,
  `data.paginator`) despite looking like it could be a typo.

## Overall verdict

The `_REGISTRY` design (one source of truth per object, verified against the spec rather than assumed)
is sound, and the *majority* of the 56 entries — including every `items_per_page` cap and every
`include` menu — are exactly correct. But five entries have a genuinely broken primary key (three of
which — `products_changes`, `customers_changes`, `abandoned_carts` — cause real, silent data loss or
corruption on incremental runs, not just a missed optimization), and two more child-table declarations
point at fields that don't exist at the level the code expects. All five blockers share the same root
cause and the same fix shape: the registry entry was written from the endpoint's *English description*
("prices across all pricelists," "list of customer delivery addresses") rather than from the actual
response schema, and nothing in the test suite currently catches the mismatch because the mocked
fixtures were built from the same assumption. None of the five blockers requires an architecture
change — each is a one-line (or one-tuple) edit in `_REGISTRY` — so this is not a rebuild, it is a
fix-five-lines-and-re-verify-against-live-data pass.

**Phase 3 resolution summary.** All five blockers (1-5) and all nine should-fix findings (6-14) are
resolved or explicitly deferred with reasoning — see the per-finding "Status (Phase 3)" note added
above each. Findings 15-17 (new objects) are deferred out of scope, as instructed, and recorded in
`docs/design.md` §9. Findings 18-20 (cosmetic header casing, dead `test_connection` fallbacks, the
README's overstated test coverage) are resolved. A new `tests/test_registry_invariants.py`, checked
against a vendored extract of the real API schemas (`tests/fixtures/shoptet_field_extract.json`, no
scratchpad-path or network dependency), asserts mechanically that every declared primary-key column and
every declared child field is a real field on the API record — the exact class of error findings 1-4
and 11 were. It would have caught all five on its own, before a human review was ever needed.
