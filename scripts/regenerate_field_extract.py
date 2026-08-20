#!/usr/bin/env python3
"""Regenerate ``tests/fixtures/shoptet_field_extract.json`` from the Shoptet OpenAPI description.

The fixture backs ``tests/test_registry_invariants.py``, which asserts that every
primary-key column and child field declared in ``_REGISTRY`` is a field the API
actually returns. That test is only as trustworthy as the fixture, so the fixture
must be derivable mechanically rather than maintained by hand — otherwise it can
drift from the API, or be wrong in exactly the same way as the registry entry it
is supposed to police, and the test passes while proving nothing.

Usage::

    uv run python scripts/regenerate_field_extract.py            # fetch the spec, rewrite the fixture
    uv run python scripts/regenerate_field_extract.py --check     # fail if the fixture is stale
    uv run python scripts/regenerate_field_extract.py --spec x.json

Run it (with ``--check``) whenever Shoptet publishes a spec change, and after
adding an object to the registry.

Deliberately NOT part of the test suite: it needs network access to fetch the
spec, and the whole point of vendoring the extract is that CI stays offline.
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

from component import _REGISTRY, FetchMode  # noqa: E402
from configuration import ObjectType  # noqa: E402

# Redocly serves the bundled description behind the rendered reference docs.
SPEC_URL = "https://api.docs.shoptet.com/_bundle/Shoptet%20API/openapi.json"

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "shoptet_field_extract.json"

# Snapshot endpoints return a JSON Lines file, so the record shape is not in the
# 202 response — it lives in a `*Snapshot` component schema that the endpoint
# description links to. `abandoned-carts` is the one snapshot endpoint whose
# description carries no such link, so it needs naming explicitly.
_SNAPSHOT_SCHEMA_OVERRIDES = {
    ObjectType.abandoned_carts: "abandonedCartSnapshot",
}

_SNAPSHOT_LINK = re.compile(r"/shoptet-api/openapi/snapshot/([a-zA-Z]+)")


def _resolve(node: Any, schemas: dict[str, Any], depth: int = 0) -> Any:
    """Follow `$ref` chains to the underlying schema object."""
    while isinstance(node, dict) and "$ref" in node and depth < 12:
        node = schemas.get(node["$ref"].split("/")[-1], {})
        depth += 1
    return node


def _properties(node: Any, schemas: dict[str, Any]) -> list[str]:
    node = _resolve(node, schemas)
    if not isinstance(node, dict):
        return []
    # A record may carry its own `properties` AND compose more in via `allOf` —
    # the snapshot schemas do exactly that, adding the parent-linking field
    # (orderCode, productGuid) alongside an allOf of the base detail schema. Both
    # halves must be merged; returning either one alone silently loses fields.
    merged: list[str] = list((node.get("properties") or {}).keys())
    for part in node.get("allOf") or ():
        merged.extend(_properties(part, schemas))
    return merged


def _snapshot_fields(path: str, obj: ObjectType, spec: dict[str, Any], schemas: dict[str, Any]) -> list[str]:
    override = _SNAPSHOT_SCHEMA_OVERRIDES.get(obj)
    if override:
        return _properties(schemas[override], schemas)
    description = spec["paths"][path]["get"].get("description") or ""
    names = _SNAPSHOT_LINK.findall(description)
    if not names:
        raise SystemExit(f"{obj.value}: no snapshot schema link in the description of {path}; add an override")
    wanted = names[0].lower()
    # The doc links are lowercased; component schema keys are camelCase.
    for key in schemas:
        if key.lower() == wanted:
            return _properties(schemas[key], schemas)
    raise SystemExit(f"{obj.value}: snapshot schema '{names[0]}' not found in components.schemas")


def _response_fields(path: str, data_key: str | None, spec: dict[str, Any], schemas: dict[str, Any]) -> list[str]:
    responses = spec["paths"][path]["get"]["responses"]
    ok = responses.get("200") or responses.get("202")
    schema = _resolve((ok.get("content", {}).get("application/json", {}) or {}).get("schema", {}), schemas)
    data = _resolve(schema.get("properties", {}).get("data", {}), schemas)
    if data_key is None:
        # Single-record endpoints (e.g. /api/eshop) put the record straight on `data`.
        return _properties(data, schemas)
    node = _resolve((data.get("properties") or {}).get(data_key, {}), schemas)
    if node.get("type") == "array":
        return _properties(node.get("items", {}), schemas)
    return _properties(node, schemas)


def build(spec: dict[str, Any]) -> dict[str, Any]:
    schemas = spec["components"]["schemas"]
    extract: dict[str, Any] = {
        "_meta": {
            "contents": (
                "Top-level property names of the API record each registry object returns: the linked "
                "*Snapshot component schema for FetchMode.SNAPSHOT objects, otherwise the item schema "
                "under data.<data_key>[]. Backs tests/test_registry_invariants.py."
            ),
            "regenerate": "uv run python scripts/regenerate_field_extract.py (--check to verify freshness)",
            "source": SPEC_URL,
            "spec_version": spec.get("info", {}).get("version"),
        }
    }
    for obj, endpoint in _REGISTRY.items():
        # PER_STOCK paths are templated; the schema is the same for any stock id.
        path = endpoint.path.replace("{stock_id}", "{stockId}")
        if endpoint.mode == FetchMode.SNAPSHOT:
            fields = _snapshot_fields(path, obj, spec, schemas)
        else:
            fields = _response_fields(path, endpoint.data_key, spec, schemas)
        if not fields:
            raise SystemExit(f"{obj.value}: extracted no fields from {path} — the walk is wrong, not the API")
        extract[obj.value] = sorted(set(fields))
    return extract


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed fixture is stale")
    parser.add_argument("--spec", help="path to a local openapi.json instead of fetching it")
    args = parser.parse_args()

    if args.spec:
        spec = json.loads(Path(args.spec).read_text())
    else:
        print(f"fetching {SPEC_URL}")
        with urllib.request.urlopen(SPEC_URL, timeout=120) as response:  # noqa: S310 - fixed vendor URL
            spec = json.loads(response.read())

    generated = build(spec)

    if args.check:
        current = json.loads(FIXTURE_PATH.read_text())
        drift = {
            key: {"fixture": current.get(key), "spec": generated[key]}
            for key in generated
            if not key.startswith("_") and current.get(key) != generated[key]
        }
        missing = [key for key in current if not key.startswith("_") and key not in generated]
        if drift or missing:
            print(json.dumps({"drifted": drift, "no_longer_in_registry": missing}, indent=2))
            print(f"\nFIXTURE IS STALE: {len(drift)} object(s) differ, {len(missing)} orphaned.")
            return 1
        print(f"fixture is current ({len(generated) - 1} objects).")
        return 0

    FIXTURE_PATH.write_text(json.dumps(generated, indent=2, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE_PATH} ({len(generated) - 1} objects)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
