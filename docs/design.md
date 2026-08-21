# keboola.ex-shoptet-api — Design Spec (retro-authored)

> Type: extractor
> Component ID: keboola.ex-shoptet-api
> Status: retro-spec, amended in place after Phase 3 (Phase 2 planning was skipped; this document was
> written after implementation to capture the spec that should have existed, then checked against it —
> see `docs/gap-analysis.md` for every deviation found). §4, §7 and §9 were updated after Phase 3
> implemented the fixes; §4 also gained a "Deferred: candidate objects for a follow-up" section.
> Date: 2026-08-20 (retro-spec written); amended 2026-08-20 (Phase 3)

## 0. How this document was produced

The implementation (`src/client.py`, `src/component.py`, `src/configuration.py`,
`component_config/*.json`, `README.md`) was written first; no design spec preceded it. This document
reverse-engineers the spec the code implies, then every factual claim in it — every path, data key,
filter parameter, primary key, and `include` value — was checked against the authoritative source: the
bundled OpenAPI 3.1 description of the Shoptet REST API (209 paths, `/api/*`, base URL
`https://api.myshoptet.com`). Deviations found during that check are **not** silently folded in here;
they are catalogued in `docs/gap-analysis.md` with severity and a concrete fix, and this document
describes the design **as implemented**, warts included, with a pointer to the gap analysis wherever a
claim turned out to be wrong.

## 1. Overview & source system

`keboola.ex-shoptet-api` extracts e-shop data (orders, products, customers, accounting documents,
stock, catalogue/reference data, marketing, content) from a Shoptet e-shop through the official
[Shoptet REST API](https://developers.shoptet.com/api/documentation/), replacing the CSV-permalink
extractor `kds-team.ex-shoptet-permalink`.

Primary use case: give Keboola customers structured, incrementally-loadable Shoptet data (full order
detail, nested line items, customer accounts/addresses, stock movements, …) without depending on
permalink URLs a merchant can regenerate or a fixed CSV column set.

## 2. Keboola mapping

- **One object per config row.** Each row's `object` parameter selects one Shoptet collection; the row
  produces one parent output table plus one child table per nested array split out
  (`extract_child_tables`, default on). This follows the Tier-A convention (multiple independent
  objects → config rows, one per object) — correctly applied here, not questioned.
- **Config level vs row level.** Connection (`auth_type`, the two token fields, `oauth_token_url`,
  `api_base_url`) lives at config level; extraction settings (`object`, `load_type`, `date_range`,
  `include`, `stock_id`, `extract_child_tables`) live at row level. This matches the Tier-A default
  (credentials once, per-object settings per row).
- **Incremental strategy.** `load_type` is a `full_load` / `incremental_load` dropdown (not a bare
  boolean), defaulting to `incremental_load`, matching the canonical shape in
  `keboola-context/references/incremental-state.md`. The watermark (`last_run`) is captured with
  `datetime.now(UTC)` **before** the fetch starts and written to `state.json` only after a successful
  write — correct ordering per the convention (a crash mid-run keeps the old watermark; a record edited
  during the run is re-fetched next time rather than skipped). State is row-scoped automatically
  because the component uses config rows (`keboola-context/references/config-rows.md`); no manual
  per-object nesting was needed and none was added.
- **Output mapping.** `incremental=True` is only ever set together with a non-empty effective primary
  key (`_finalize_table`: `incremental=incremental and bool(effective_pk)`), which is the correct
  upsert-vs-append-forever guard from `keboola-context/references/output-mapping.md`. Where this
  defensive guard actually *fires* in practice (because a declared PK column never appears in the real
  payload) is exactly the class of bug the gap analysis found repeatedly — see
  `docs/gap-analysis.md` findings 1–3 and 5.
- **Secrets.** `#private_api_token` and `#oauth_access_token` are the two `#`-prefixed encrypted keys;
  `oauth_token_url` and `api_base_url` are plain (not secrets — they are per-e-shop configuration, not
  credentials).
- **Sync actions.** `testConnection` (validates credentials via the cheapest possible call, `/api/eshop`),
  `listStocks` and `listPriceLists` (dropdown population), `listIncludeSections` (row-scoped, driven off
  the endpoint registry so the offered sections match what the selected object actually supports), and
  `listApprovedEndpoints` (surfaces which endpoint groups the current token/addon may read, to explain a
  403 before it happens). This covers the Tier-A "enumerable choices → sync-action dropdown" and
  "connection validation → test-connection" defaults.
- **Native data types / manifest.** Output tables are written with an explicit `schema=` (authoritative
  manifest format, `ColumnDefinition(data_types=...)`), column type inferred per-column from observed
  values (`integer` / `numeric` / `boolean` / `timestamp` (by column-name hint) / `string`), headerless
  CSV matching the schema-carries-the-header-names convention from
  `keboola-context/references/native-data-types.md`. This is implemented correctly and consistently.
- **Output bucket.** No bucket is hardcoded in the component; naming follows the Developer Portal's
  default-bucket behaviour (out of scope for this code review — a Dev Portal property, not code).

## 3. Authentication & connection

Two methods, both real Shoptet auth models, both implemented:

- **`private_token`** — `Shoptet-Private-Api-Token` header, a merchant-generated token (e-shop admin →
  Connections → Private API). Simple, but **Shoptet Premium only**.
- **`addon_oauth`** — exchange the addon's permanent OAuth access token for a 30-minute
  `Shoptet-Access-Token` API token at `<eshop>/action/ApiOAuthServer/getAccessToken`; the client
  transparently refreshes it on expiry or a 401. Works on any tariff that can install marketplace
  addons, but requires the integration to be a **registered Shoptet API partner** (multi-week approval,
  no self-serve sandbox).

**Provisioning / blockers (documented in the README, correctly):**
- No self-service Shoptet sandbox exists. The only frictionless testing surface is the public
  documentation mock server (`https://api.docs.shoptet.com/_mock/shoptet-api/openapi`), reachable via
  the `api_base_url` override — but its snapshot `resultUrl` does not resolve, so only
  paginated/list/single objects are exercisable against it; every `SNAPSHOT`-mode object (orders,
  products, customers, all accounting documents, abandoned carts) needs a real token to test.
- A free trial e-shop cannot install addons and has no Private API screen — it cannot produce
  *any* usable credential.
- A Shoptet Premium e-shop (private token) or a signed API-partner contract (addon OAuth, weeks) are
  the only two paths to real credentials. This is an accurate, already-surfaced blocker, not something
  this phase needs to re-flag.

## 4. Capability inventory & scope

The Shoptet OpenAPI bundle exposes 209 paths. The component implements 56 `ObjectType` values (one
`_REGISTRY` entry each). The table below is grouped the same way the README groups them; verdicts
reflect what is **implemented today**, with correctness caveats pointing at the gap analysis where the
registry entry's paths/keys were found to be wrong.

| Capability | Verdict | Notes |
|---|---|---|
| Orders (`orders`, snapshot) | In scope | Children: items, shippings, payment methods, completion, payment transactions. `payment_transactions` child likely always empty — gap analysis #10 (deferred, needs a live payload to confirm). |
| Order history / remarks (`order_history`, snapshot) | In scope | PK fixed to `["orderCode", "id"]` — gap analysis #8 (resolved, then amended: `id` reads as a per-order sequence, so `id` alone would merge every order's first remark). |
| Orders — change feed (`orders_changes`) | In scope | PK is now `["code", "changeTime", "changeType"]` — gap analysis #9 (resolved). |
| Products (`products`, snapshot) | In scope | `include` menu (20 values) verified exact match to spec, and now pinned by a test. Two dead child declarations removed and documented — gap analysis #6 (resolved; grandchild-of-a-child splitting deferred, see §9). |
| Products — prices in all price lists (`product_pricelist_prices`) | In scope | PK fixed to `["productGuid", "code"]`, dead child declaration removed — gap analysis #7 (resolved). |
| Products — change feed (`products_changes`) | In scope | PK fixed to key on `guid` — gap analysis #1 (resolved). |
| Customers (`customers`, snapshot) | In scope | Child field corrected to `deliveryAddress` (singular) — gap analysis #4 (resolved). |
| Customers — change feed (`customers_changes`) | In scope | Same fix as products_changes — gap analysis #1 (resolved). |
| Customer groups (`customer_groups`) | In scope | PK fixed to `["id"]` — gap analysis #3 (resolved). |
| Customer regions (`customer_regions`) | In scope | Verified correct. |
| Invoices / proforma invoices / credit notes / delivery notes (snapshot + change feeds) | In scope | Verified correct (PK `code`, `include=surchargeParameters` where documented). |
| Proofs of payment (`proof_payments`, snapshot + change feed) | In scope | Dead `items` child declaration removed — gap analysis #11 (resolved). |
| Abandoned carts (`abandoned_carts`, snapshot) | In scope | No natural id in the API at all; PK is now `[]`, `full_load_only=True` forces a full load with a warning regardless of `load_type` — gap analysis #5 (resolved). |
| Stock — list, supplies, movements | In scope | `stock_movements` now wires `changeTimeFrom` — gap analysis #13 (resolved). |
| Categories / parametric categories / brands / suppliers / price lists | In scope | `brands` PK fixed to `["guid"]` — gap analysis #2 (resolved). `categories.guid` nullable-in-schema edge case — gap analysis #14 (resolved via general `_finalize_table` hardening, no registry change). |
| Product reference data (availabilities, flags, units, measure units, warranties, filtering/variant/surcharge parameters, recycling-fee categories, consumption taxes) | In scope | Verified correct. |
| Order/customer reference data (statuses, sources, shipping methods, payment methods) | In scope | Verified correct. |
| Marketing (discount coupons, quantity/volume/XY discounts, mailing lists, unsubscribed e-mails, reviews) | In scope | `discount_coupons` intentionally avoids its `/snapshot` variant (mandatory `template`+`shippingPrice`) — correct call. Now wires the creation-date filter — gap analysis #12 (resolved). |
| Content (articles, sections, pages, discussion posts) | In scope | Verified correct, including the oddly-named `discussion` data key. |
| E-shop settings (`eshop`, single record) | In scope | Verified correct; dead fallback fields in `test_connection` removed — gap analysis #19 (resolved). |
| Shipments (`/api/shipments`) | **Deferred to a follow-up phase** | Shipment tracking/status/carrier per order; no snapshot but a plain filterable list; no blocking required params. Plausibly valuable. Explicitly deferred, not silently dropped — see "Deferred: candidate objects for a follow-up" below. |
| Order claims (`/api/orders/claims`) | **Deferred to a follow-up phase** | RMA/product-claim records per order+product, paginated, no blocking params. Same disposition as shipments — see "Deferred: candidate objects for a follow-up" below. |
| Discount coupon templates (`/api/discount-coupons/templates`) | **Deferred to a follow-up phase** | Small reference table for the `template` field already surfaced on `discount_coupons` rows. See "Deferred: candidate objects for a follow-up" below. |
| E-shop sub-settings (`/api/eshop/customer-fields`, `/design`, `/document-settings`), reviews/discussion settings | **Deferred to a follow-up phase** | Same family as the already-included `/api/eshop`; low value, cheap to add later. See "Deferred: candidate objects for a follow-up" below. |
| Webhooks (`/api/webhooks*`), write-only/admin endpoints (batch updates, order status changes, `/api/discount-coupons/set`, `/api/categories/products-priority/batch`, …) | Excluded | Correctly out of scope for a read-only extractor. |

Because Phase 2 (research) never happened, none of the objects above marked "Deferred to a follow-up
phase" carried an actual user sign-off before this pass — they were simply capabilities nobody had
surfaced. Phase 3 makes that decision explicit rather than leaving it silent: see "Deferred: candidate
objects for a follow-up" immediately below.

### Deferred: candidate objects for a follow-up

Phase 3's brief was explicit that these are out of scope for this pass — adding a new object expands the
component's surface and each one needs a schema enum entry, a UI label and test coverage of its own, none
of which this phase's mandate covered. Recorded here so the decision is visible rather than forgotten,
per `docs/gap-analysis.md` findings 15-17:

- **`GET /api/shipments`** (finding 15) — shipment tracking (`guid`, `status`, `carrierAddress`,
  `serviceCode`, `packages[]`, `cod`, linked `orderCode`), filterable by `status`/`orderCode`. A plain
  list, not a snapshot; needs a pagination check before adding (not fully verified). Plausibly valuable
  for fulfilment/carrier-performance analysis.
- **`GET /api/orders/claims`** (finding 15) — RMA/product-claim records, one row per (`orderCode`,
  `productCode`) with `statusId`, `amount`, `amountCompleted`; paginated, no blocking required params.
  (`orderCode`, `productCode`) is a plausible composite key, not confirmed unique across repeated claims.
- **`GET /api/discount-coupons/templates`, `/api/eshop/customer-fields`, `/api/eshop/design`,
  `/api/eshop/document-settings`, `/api/reviews/settings`, `/api/discussions/settings`** (finding 16) —
  small, cheap-to-add `SINGLE`/reference endpoints with no required parameters. Low value on their own;
  `discount-coupons/templates` is the most immediately useful of the group since `discount_coupons` rows
  already surface a `template` field with nothing to join it against.
- **`GET /api/quantity-discounts/snapshot`** (finding 17) — a drop-in replacement for the currently-used
  paginated `/api/quantity-discounts` that would scale better if this collection is ever expected to grow
  beyond what a 20-per-page paginated list comfortably handles. Not a correctness issue today.

A follow-up planning pass should bring these to a user as an explicit scope question (the same Tier-C
decision the gap analysis recommended), rather than adding them unreviewed.

## 5. Configuration & schema

Already implemented in `component_config/configSchema.json` (connection: `auth_type`,
`#private_api_token`, `#oauth_access_token`, `oauth_token_url`, `test_connection` sync-action button)
and `component_config/configRowSchema.json` (`object` enum with 56 titled values, `load_type`,
`lookback_hours`, `date_range.{date_from,date_to}`, `include` async multi-select driven by
`listIncludeSections`, `stock_id` async select gated to `stock_supplies`/`stock_movements`,
`extract_child_tables` checkbox). This phase did not re-derive the schema from scratch (that is
`component-build-ui`'s job); it is described here for completeness and was spot-checked against the
Python config model (`src/configuration.py`) for consistency — the two agree.

## 6. Code architecture

- **`src/configuration.py`** — Pydantic models (`Credentials`, `Configuration`, plus `AuthType`,
  `LoadType`, `ObjectType`, `DateRange` enums/models). Validation failures are re-raised as
  `UserException` inside `__init__`, matching the exit-code convention. `Credentials` is parsed
  separately from `Configuration` so sync actions never trip over an unrelated extraction-setting
  problem — a deliberate, sound separation.
- **`src/client.py`** — all HTTP concerns: dual auth (private token vs. addon OAuth exchange +
  refresh), leaky-bucket throttling (proactive slow-down at 75% fill, computed from the documented
  10 drops/s drain rate), retry/back-off (429 honouring `Retry-After` as a *datetime* per the API's own
  documentation quirk, 423 write-lock, 5xx, network errors — all via `tenacity`), pagination
  (`page`/`itemsPerPage`, driven by `paginator.pageCount`, not record counting), and the three-step
  snapshot flow (submit → poll `/api/system/jobs/{jobId}` → stream gzipped JSON Lines from
  `resultUrl`, downloaded unauthenticated first since the result URL is an unguessable one-off link on
  the e-shop's own domain). This module was the most heavily verified against the spec and came out
  clean — see the "what was verified and found correct" section of the gap analysis.
- **`src/component.py`** — `Component.run()` is a genuinely thin orchestrator (~25 lines): resolve the
  registry entry for `cfg.object`, validate `include`, capture the watermark, stream records through
  `_iter_records`, split into parent/child `_TableWriter`s via `_write_record`, finalize each table with
  an explicit primary key and inferred schema. All endpoint-specific knowledge (path, fetch mode, PK,
  filter parameter names, children, `include` menu, page-size cap) lives once in the `_REGISTRY` dict —
  a real "one source of truth" design, which is exactly why the registry's mistakes (see gap analysis)
  are each a single, easily-fixed edit rather than scattered control-flow bugs.
- **`_TableWriter`** spools rows to an NDJSON temp file (`tempfile.gettempdir()`, correctly *not*
  `/data/out/tables/`) while tracking the observed column superset and per-column type, so a
  hundreds-of-thousands-of-record snapshot doesn't need the full column set known up front and doesn't
  hold everything in memory.
- **Error handling.** `UserException` → exit 1 with the message logged via `logger.error(str(exc))`
  (not `logger.exception`, so the user-facing message survives instead of being buried in a traceback —
  this is the exact fix the project's own memory note flags as a recurring cookiecutter pitfall, and it
  is already applied correctly here). Unexpected exceptions → `logger.exception(...)` + exit 2. This
  matches the exit-code convention precisely.

## 7. Testing

State as of Phase 3 (implementation of the gap-analysis fixes):

- Unit tests for the client (`tests/test_client.py` — auth, throttling, retry, pagination, snapshot
  polling, all against a stub `requests.Session` in `tests/fake_api.py`), the component
  (`tests/test_component.py`), and configuration validation (`tests/test_configuration.py`) — all
  extended this phase with cases for every blocker and should-fix finding (wrong-PK objects, the
  `deliveryAddress` child fix, `abandoned_carts`'s forced full load and warning, the new date/change
  filters, the empty-primary-key-collision warning).
- **New this phase: `tests/test_registry_invariants.py`.** Every declared primary-key column and every
  declared child field in `_REGISTRY` is checked against
  `tests/fixtures/shoptet_field_extract.json` — a checked-in extract of the real
  API response schemas (top-level field names only, for every object in the registry). This is
  deliberately *not* a reference to the full OpenAPI bundle at its scratchpad path from the planning
  phase: that path is machine-local and would not exist in CI or on another contributor's machine. This
  single test class would have caught gap-analysis findings 1-4 and 11 mechanically, without a human
  cross-check against the spec — it is the highest-leverage artifact from this phase, more valuable than
  any individual fix, because it keeps the same class of drift from recurring silently as the registry
  grows.
- A VCR/datadir functional harness is wired up (`tests/test_functional.py`, using
  `keboola.datadirtest.vcr.VCRDataDirTester`) but **`tests/functional/` still does not exist and
  `tests/setup/` is still empty** — there are zero recorded end-to-end test cases as of this phase.
  Recording cassettes is explicitly **Phase 5's job, not this phase's** (an earlier draft of this
  document incorrectly said "Phase 3" — corrected here). The README's "Testing without a Shoptet
  account" section was rewritten this phase to describe today's actual coverage accurately rather than
  implying a functional suite already exists; it also documents in one place why the snapshot endpoints
  in particular will stay stub-tested until a real Shoptet token is available (the mock server's
  `resultUrl` host does not resolve, so no `SNAPSHOT`-mode cassette can be recorded against it).

## 8. Deployment & validation (CF test project)

Out of scope for this phase (planning/design only; no Developer Portal or deployment actions were
taken, per this phase's constraints). Once the gap-analysis blockers are fixed, Phase 6/7 of the normal
lifecycle (`component-dev-portal`, `component-test`) would register the app and smoke-test it in the CF
test project.

## 9. Open risks & blockers (resolved in Phase 3)

The five blockers below were the reason this phase existed. All five are now resolved in
`src/component.py`; kept here, marked resolved, as the historical record of what was wrong and why (full
detail, evidence, and fixes in `docs/gap-analysis.md`):

1. **RESOLVED — silent primary-key corruption on `products_changes` and `customers_changes`.** The
   shared `_changes_endpoint()` helper hardcoded `pk=["code", "changeTime"]`, but both endpoints key
   their change entries by `guid`, not `code`. Fixed by giving the helper an `id_field` parameter
   (`"guid"` for these two) and widening the key to `[id_field, "changeTime", "changeType"]` for all
   seven change feeds (also resolves gap-analysis finding 9).
2. **RESOLVED — `brands` had no working primary key.** Declared PK `["code"]`; the API never returns a
   `code` field for a brand (only `guid`). Fixed: PK is now `["guid"]`.
3. **RESOLVED — `customer_groups` had no working primary key.** Declared PK `["guid"]`; the API's
   identifier is `id` (there is no `guid` field on this object at all). Fixed: PK is now `["id"]`.
4. **RESOLVED — `customers_delivery_addresses` was permanently empty.** The registry looked for a field
   named `deliveryAddresses`; the API's field is `deliveryAddress` (singular, but an array). Fixed: the
   child now reads `deliveryAddress`; the output table keeps its plural name (it holds many rows).
5. **RESOLVED — `abandoned_carts` lost history on every incremental run.** The API's abandoned-cart
   snapshot record has no identifier field of any kind, so the declared PK (`guid`) never matched
   anything; combined with the date-windowed incremental fetch, each run overwrote the whole table with
   just the newest slice, destroying previously accumulated carts. Fixed: PK is now `[]`, the automatic
   change window (`changed_from`) was removed entirely, and a new `_Endpoint.full_load_only` flag forces
   a full load with a `logger.warning` regardless of the row's `load_type` — general enough that a
   registry-invariant test (`tests/test_registry_invariants.py`) now asserts any future object with an
   empty primary key *and* a `changed_from` filter must also set this flag.

Also resolved this phase: findings 6-14 (should-fix) and 18-20 — see `docs/gap-analysis.md` for the
per-finding status. Findings 15-17 (new objects: `/api/shipments`, `/api/orders/claims`, small
settings/reference endpoints, the `quantity_discounts` snapshot variant) remain an explicit, recorded
open decision — see "Deferred: candidate objects for a follow-up" in §4 — rather than a silently
narrowed scope; a human (or a follow-up Tier-C planning pass) still needs to decide whether to add them.

## Post-gate corrections (Phase 3 gate)

An independent gate re-derived all 56 registry entries from the OpenAPI description and
found two things the original gap analysis missed, plus stale counts in this document.
Recorded here because both are classes of bug, not one-off slips.

### `variant_parameters` declared a child table that could never populate

`values` is a real field on that endpoint's record, so the existence check in
`tests/test_registry_invariants.py` passed — but it is a **section on demand**: the
endpoint only sends it when asked via `include`, and the entry requested nothing. The
child table was therefore empty on every run for every e-shop. Its two sibling entries
(`filtering_parameters`, `surcharge_parameters`) look identical but have `values` in
their `required` set and no `include` parameter at all, which is why the entry read as
correct.

Two things made this hard to see, and both are now closed:

- **The docs mock server hides it.** `https://api.docs.shoptet.com/_mock/...` returns the
  full example payload regardless of `include`, so a smoke run against the mock showed
  the child table populating normally. Mock coverage cannot prove availability — only a
  schema-derived check can.
- **The fixture recorded existence, not availability.** It listed field *names* only, so a
  gated field was indistinguishable from a guaranteed one. It now also records `required`,
  `has_include_param`, `documented_include_sections` and `items_per_page_cap`, and
  `scripts/regenerate_field_extract.py` derives all of it from the published description
  (`--check` fails when stale). New invariants: a child field that is a documented section
  must actually be requested; no `include_options` entry may be undocumented; and
  `items_per_page` must equal the documented cap — the last was previously unguarded, so
  raising `articles` from its real cap of 10 to 1000 passed every test and would have
  failed on the first live run.

### `full_load_only` did not cover change feeds

The flag forces a full load for an object with no identifier, so a date-windowed
incremental run cannot overwrite the table with only its newest slice. But change feeds
need a mandatory `from` and so deliberately bypass the incremental check — meaning a
*keyless* change feed would still have been date-windowed while writing a table it cannot
upsert into. The same history-destroying trap, re-armed, with both the flag and its test
asserting it was safe. `_incremental_since` now refuses a watermark for any
`full_load_only` endpoint before that branch is reached, while still honouring an explicit
user `date_range`. No object hits this today; the test exists so adding one stays safe.

The test that was supposed to cover this could not fail: it asserted `visitTimeFrom` was
absent from an `abandoned_carts` request, but that entry has no `changed_from` at all, so
`_incremental_since` is never consulted on that path. Replaced with two tests that
exercise the guard directly on a synthetic keyless change feed.

### Deferred: documented `include` sections that are not surfaced

Three endpoints document optional sections the component never offers, so `_validate_include`
tells the user the object "supports no optional sections":

| Endpoint | Undeclared sections |
|---|---|
| `/api/eshop` | `orderAdditionalFields`, `orderStatuses`, `paymentMethods`, `shippingMethods`, `imageCuts`, `countries`, `cashDesk` |
| `/api/parametric-categories` | `parameters` |
| `/api/stocks/{stockId}/movements` | `orderCode`, `productGuid`, `historicalProductGuid` |

These are data-completeness gaps, not bugs — nothing returns wrong data, and most of the
`eshop` sections are separately extractable objects in their own right. Deferred rather
than silently omitted: surfacing them widens the output schema of existing tables, which
is a backward-compatibility decision for an existing configuration, not a bug fix. The
`stock_movements` ones are the most clearly useful (they name the related order and
product) and are the natural first follow-up.

## Known limitation: column types are inferred per run

`_TableWriter._observe_kind` infers each column's native type from the values seen in
**that run only**, and `_finalize_table` writes a fresh manifest schema from it. So a
column that happens to be all-integer in one incremental run and picks up a single
fractional value in the next moves from `INTEGER` to `NUMERIC` in the manifest between
runs — and could narrow the other way if the fractional values fall out of a later
window.

This is deliberate for a first release: the alternative is pinning a type per column per
object across 56 objects, which means asserting types for fields the OpenAPI description
often declares only as `string` (Shoptet returns monetary amounts as decimal strings, so
inference is frequently *better* informed than the schema).

The open question is how Keboola Storage handles a **narrowing** native-type change on an
existing typed table between incremental loads. That was not verifiable without a live
project, so it is flagged rather than resolved: worth a two-run smoke test on a
numeric-ish field (`price`, `vatRate`) in cf-dev before a wide rollout. If narrowing turns
out to be rejected, the fix is to widen monotonically by carrying the previous run's
observed kinds in the state file, rather than to hardcode a type table.


## Findings from the first real platform run (Phase 7)

Two defects that only a job on the platform could surface. Both were invisible locally
because the local suite compares the component's *own* output, never what Storage does
with it.

### Storage strips a leading underscore from a column name

The child-row position column was written as `_row_number` and landed in Storage as
`row_number`. The primary key was applied correctly to the renamed column
(`parameter_id|row_number`), so nothing broke — but every document and manifest said
`_row_number`, so a transformation written against the documented name would reference
a column that does not exist. The column is now named `row_number` at the source, so
what the component writes is what arrives.

### Typed manifests were silently ignored

Every column arrived as `VARCHAR` with `keboola_base_type: null`, despite the component
declaring INTEGER / NUMERIC / BOOLEAN / TIMESTAMP for each one. The cause is the
Developer Portal's `dataTypeSupport`, which the bootstrap release left at `none`; with
that setting Storage discards the schema in the manifest. All the type inference was
dead weight on the platform.

Turning it to `authoritative` would honour the schema — but doing that alone would have
converted the per-run inference into a live failure, which is why the inference was
fixed first. Types were previously derived from the values a single run happened to see,
so a column holding `3.5` in one run and only `3` in the next would be re-declared
INTEGER over a NUMERIC column. Harmless while everything is VARCHAR; a hard load
failure once types are real. The state file now carries each table's observed kinds
(`column_kinds`) and every run seeds its inference with them, so `_merge_kind` can only
broaden. Kinds are kept for tables a given run wrote nothing to, since an optional child
table that appears only in some runs would otherwise be free to narrow.

This resolves the open question recorded above under "Known limitation: column types are
inferred per run" — the narrowing risk is closed at the source rather than left to
Storage's tolerance.

### Authoritative types exposed a broken timestamp contract

Turning `dataTypeSupport` to `authoritative` made the first typed load fail outright:

    Timestamp '2018-05-29T09:02:27+0200' is not recognized

Shoptet writes ISO 8601 with a **colon-less** offset, which Snowflake refuses. Because a
rejected value fails the whole table import, every object carrying `creationTime`,
`changeTime`, `visitTime` or `taxDate` — most of them — would have been unloadable. It
went unnoticed locally because the test suite compares the component's own CSV and
manifest; nothing in it asks the warehouse whether the values are actually loadable.

Two things were wrong:

1. **The format.** Timestamp columns are now normalised on write to `YYYY-MM-DD
   HH:MM:SS+HH:MM`, which Snowflake accepts.
2. **How a timestamp was identified.** The type came from a *column-name* heuristic —
   anything containing "time" or "date" was declared TIMESTAMP regardless of content.
   That is both too eager (a `productOrdering` value like `alphabetically` sits in no
   such column, but `dateFormat`-style names would) and dangerous under authoritative
   types, since a mis-typed column fails the load rather than degrading. Typing is now
   inferred from whether the value actually parses, consistent with how integer,
   numeric and boolean are already inferred. A string that does not parse stays text, so
   a mixed column degrades to STRING instead of breaking the import.

Still unverified: a **null** timestamp. Shoptet declares most of these fields nullable,
but the documentation mock returns fully-populated examples, so no run so far has loaded
an empty value into a TIMESTAMP column. Empty cells are written as empty strings, and
whether Storage coerces those to NULL for a typed column or rejects them has not been
observed. This wants either a real e-shop with sparse data or a crafted fixture before a
wide rollout — it is the one remaining known gap in the typed-output path.
