"""Regenerate ``tests/fixtures/shoptet_field_extract.json`` from the Shoptet OpenAPI description.

The fixture backs ``tests/test_registry_invariants.py``, which asserts that every
primary-key column, child field and page-size cap declared in ``_REGISTRY`` matches
what the API actually offers. That test is only as trustworthy as the fixture, so the
fixture must be derivable mechanically rather than maintained by hand — otherwise it
can be wrong in exactly the same way as the registry entry it is supposed to police,
and the test passes while proving nothing.

Per object it records:

``fields``
    Top-level property names of the record the component parses — for a snapshot
    object the linked ``*Snapshot`` component schema (the record is a JSON Lines
    line, not the 202 body), otherwise the item schema under ``data.<data_key>[]``.
``required``
    Which of those are guaranteed present. A field outside this set may simply not
    be sent, which is how a child table can be declared against a genuinely real
    field and still never populate — recording existence alone cannot catch that.
``has_include_param``
    Whether the endpoint gates optional sections behind ``include``.
``documented_include_sections``
    The section names the endpoint's description documents for ``include``, parsed
    from its markdown table. Best-effort: the tables are prose and a few endpoints
    format them unusually, so this can under-report — it never over-reports for an
    endpoint that has an ``include`` parameter, which is what the invariants rely on.
    Only populated when the endpoint actually has an ``include`` parameter, because
    unrelated markdown tables in a description otherwise parse as false sections.
``items_per_page_cap``
    The documented maximum page size, which differs per collection and is rejected
    when exceeded.

Usage::

    uv run python scripts/regenerate_field_extract.py            # fetch the spec, rewrite the fixture
    uv run python scripts/regenerate_field_extract.py --check    # fail if the fixture is stale
    uv run python scripts/regenerate_field_extract.py --spec openapi.json

Deliberately NOT part of the test suite: it needs network access to fetch the spec,
and the whole point of vendoring the extract is that CI stays offline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from component import _REGISTRY, FetchMode
from configuration import ObjectType

# Redocly serves the bundled description behind the rendered reference docs.
SPEC_URL = "https://api.docs.shoptet.com/_bundle/Shoptet%20API/openapi.json"

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "shoptet_field_extract.json"

# Snapshot endpoints return a JSON Lines file, so the record shape is not in the 202
# response — it lives in a `*Snapshot` component schema the description links to.
# `abandoned-carts` is the one snapshot endpoint whose description carries no link.
_SNAPSHOT_SCHEMA_OVERRIDES = {
    ObjectType.abandoned_carts: "abandonedCartSnapshot",
}

_SNAPSHOT_LINK = re.compile(r"/shoptet-api/openapi/snapshot/([a-zA-Z]+)")
_MAX_PAGE_SIZE = re.compile(r"max value is (\d+)", re.IGNORECASE)
# Section tables render as "Include parameter | Meaning --- | --- images | ... " or
# "Value | Section --- | --- `surchargeParameters` | ...", so split on the header
# separator and take the first cell of each row.
_TABLE_SEPARATOR = re.compile(r"-{3}\s*\|\s*-{3}")
_TABLE_CELL = re.compile(r"`?([a-zA-Z][a-zA-Z0-9]*)`?\s*\|")


def _resolve(node: Any, schemas: dict[str, Any], depth: int = 0) -> Any:
    """Follow `$ref` chains to the underlying schema object."""
    while isinstance(node, dict) and "$ref" in node and depth < 12:
        node = schemas.get(node["$ref"].split("/")[-1], {})
        depth += 1
    return node


def _properties(node: Any, schemas: dict[str, Any]) -> list[str]:
    """Property names, merging a node's own `properties` with every `allOf` member's.

    The snapshot schemas do exactly that — `orderHistorySnapshot` adds `orderCode`
    and `productPricelistSnapshot` adds `productGuid`/`productId` in their own
    `properties`, alongside an `allOf` of the base detail schema. Returning either
    half alone silently loses fields, and both of those are primary-key columns.
    """
    node = _resolve(node, schemas)
    if not isinstance(node, dict):
        return []
    merged: list[str] = list((node.get("properties") or {}).keys())
    for part in node.get("allOf") or ():
        merged.extend(_properties(part, schemas))
    return merged


def _required(node: Any, schemas: dict[str, Any]) -> list[str]:
    """Required property names, merged across `allOf` the same way."""
    node = _resolve(node, schemas)
    if not isinstance(node, dict):
        return []
    merged: list[str] = list(node.get("required") or ())
    for part in node.get("allOf") or ():
        merged.extend(_required(part, schemas))
    return merged


def _query_params(path: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Query parameters of a GET, with `$ref`s into components.parameters resolved."""
    params: dict[str, Any] = {}
    for raw in spec["paths"][path]["get"].get("parameters") or ():
        param = spec["components"]["parameters"][raw["$ref"].split("/")[-1]] if "$ref" in raw else raw
        if param.get("in") == "query":
            params[param["name"]] = param
    return params


def _items_per_page_cap(query: dict[str, Any]) -> int | None:
    """Maximum page size, parsed from the parameter description.

    Shoptet states the cap in prose ("Default and max value is 1000.") rather than as
    a JSON Schema `maximum`, so it has to be read out of the text.
    """
    param = query.get("itemsPerPage")
    if not param:
        return None
    match = _MAX_PAGE_SIZE.search(str(param.get("description") or ""))
    return int(match.group(1)) if match else None


def _include_sections(description: str) -> list[str]:
    """Section names documented for the `include` parameter."""
    sections: list[str] = []
    for chunk in _TABLE_SEPARATOR.split(description)[1:]:
        sections.extend(match.group(1) for match in _TABLE_CELL.finditer(chunk))
    seen: dict[str, None] = {}
    for name in sections:
        seen.setdefault(name, None)
    return list(seen)


def _record_node(path: str, obj: ObjectType, endpoint: Any, spec: dict[str, Any], schemas: dict[str, Any]) -> Any:
    """The schema node of the record the component actually parses for this object."""
    if endpoint.mode == FetchMode.SNAPSHOT:
        override = _SNAPSHOT_SCHEMA_OVERRIDES.get(obj)
        if override:
            return schemas[override]
        names = _SNAPSHOT_LINK.findall(spec["paths"][path]["get"].get("description") or "")
        if not names:
            raise SystemExit(f"{obj.value}: no snapshot schema link on {path}; add an override")
        wanted = names[0].lower()
        for key in schemas:
            if key.lower() == wanted:
                return schemas[key]
        raise SystemExit(f"{obj.value}: snapshot schema '{names[0]}' not in components.schemas")

    responses = spec["paths"][path]["get"]["responses"]
    ok = responses.get("200") or responses.get("202")
    schema = _resolve((ok.get("content", {}).get("application/json", {}) or {}).get("schema", {}), schemas)
    data = _resolve(schema.get("properties", {}).get("data", {}), schemas)
    if endpoint.data_key is None:
        # Single-record endpoints (e.g. /api/eshop) put the record straight on `data`.
        return data
    node = _resolve((data.get("properties") or {}).get(endpoint.data_key, {}), schemas)
    return node.get("items", {}) if node.get("type") == "array" else node


def build(spec: dict[str, Any]) -> dict[str, Any]:
    schemas = spec["components"]["schemas"]
    extract: dict[str, Any] = {
        "_meta": {
            "contents": "See scripts/regenerate_field_extract.py. Backs tests/test_registry_invariants.py.",
            "regenerate": "uv run python scripts/regenerate_field_extract.py (--check to verify freshness)",
            "source": SPEC_URL,
            "spec_version": spec.get("info", {}).get("version"),
        }
    }
    for obj, endpoint in _REGISTRY.items():
        # PER_STOCK paths are templated; the schema is the same for any stock id.
        path = endpoint.path.replace("{stock_id}", "{stockId}")
        node = _record_node(path, obj, endpoint, spec, schemas)
        fields = _properties(node, schemas)
        if not fields:
            raise SystemExit(f"{obj.value}: extracted no fields from {path} — the walk is wrong, not the API")
        query = _query_params(path, spec)
        has_include = "include" in query
        description = spec["paths"][path]["get"].get("description") or ""
        extract[obj.value] = {
            "fields": sorted(set(fields)),
            "required": sorted(set(_required(node, schemas))),
            "has_include_param": has_include,
            "documented_include_sections": _include_sections(description) if has_include else [],
            "items_per_page_cap": _items_per_page_cap(query),
        }
    return extract


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the registry-invariant fixture.")
    parser.add_argument("--check", action="store_true", help="fail if the committed fixture is stale")
    parser.add_argument("--spec", help="path to a local openapi.json instead of fetching it")
    args = parser.parse_args()

    if args.spec:
        spec = json.loads(Path(args.spec).read_text())
    else:
        print(f"fetching {SPEC_URL}")
        with urllib.request.urlopen(SPEC_URL, timeout=120) as response:
            spec = json.loads(response.read())

    generated = build(spec)

    if args.check:
        current = json.loads(FIXTURE_PATH.read_text())
        drift = {
            key: {"fixture": current.get(key), "spec": generated[key]}
            for key in generated
            if not key.startswith("_") and current.get(key) != generated[key]
        }
        orphaned = [key for key in current if not key.startswith("_") and key not in generated]
        if drift or orphaned:
            print(json.dumps({"drifted": drift, "no_longer_in_registry": orphaned}, indent=2))
            print(f"\nFIXTURE IS STALE: {len(drift)} object(s) differ, {len(orphaned)} orphaned.")
            return 1
        print(f"fixture is current ({len(generated) - 1} objects).")
        return 0

    FIXTURE_PATH.write_text(json.dumps(generated, indent=2, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE_PATH} ({len(generated) - 1} objects)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
