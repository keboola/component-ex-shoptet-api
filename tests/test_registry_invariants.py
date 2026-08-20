"""Registry invariants checked against a vendored extract of the Shoptet API schema.

Every ``_REGISTRY`` entry in ``component.py`` is hand-written from the API's
documented behaviour, not generated from the OpenAPI schema, so nothing stops a
declared primary-key column or child field name from silently drifting from
what the API actually returns. That is exactly the class of bug the Phase 3 gap
analysis found by hand five times: ``brands`` and ``customer_groups`` declared
a primary key that never appears in the response; ``products_changes`` and
``customers_changes`` keyed their change events by a ``code`` field that
doesn't exist (the real field is ``guid``); ``customers`` declared a
``deliveryAddresses`` child that never fires because the field is actually
named ``deliveryAddress`` (singular); ``proof_payments`` declared an ``items``
child that has no matching field at all. None of the unit tests in
``test_component.py`` could catch this, because their mock responses were
themselves shaped from the same (wrong) assumption as the registry entry.

``tests/fixtures/shoptet_field_extract.json`` is an extract of the real response
schemas — just the top-level field names of the record each object actually
returns — for every object in the registry. It is checked into the repository
like any other test fixture, so this test has no dependency on network access
or on a machine-local path to the full OpenAPI bundle (which would not exist on
another machine or in CI).

The fixture is **generated, not maintained by hand**: run
``uv run python scripts/regenerate_field_extract.py`` to rebuild it from the
published Shoptet description, or ``--check`` to fail if it has gone stale. That
matters more than it looks — a hand-maintained fixture can be wrong in exactly
the same way as the registry entry it is meant to police, in which case this
whole test passes while proving nothing.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from component import _REGISTRY
from configuration import ObjectType

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "shoptet_field_extract.json"

# Columns a registry entry may declare that are never present in the raw API
# record because the component injects them itself before writing the row.
_INJECTED_COLUMNS = {"stock_id"}


class TestRegistryAgainstTheApiSchema(unittest.TestCase):
    """Would have mechanically caught gap-analysis findings 1-4 and 11."""

    @classmethod
    def setUpClass(cls) -> None:
        raw = json.loads(_FIXTURE_PATH.read_text())
        cls.extract: dict[str, dict] = {key: value for key, value in raw.items() if not key.startswith("_")}
        cls.fields: dict[str, set[str]] = {key: set(value["fields"]) for key, value in cls.extract.items()}
        cls.required: dict[str, set[str]] = {key: set(value["required"]) for key, value in cls.extract.items()}

    def test_fixture_covers_every_registry_entry(self):
        # A gap here would silently narrow every other test in this file rather
        # than fail loudly, so it is checked on its own first.
        missing = [obj.value for obj in ObjectType if obj.value not in self.fields]
        self.assertEqual([], missing, "tests/fixtures/shoptet_field_extract.json is missing these objects")

    def test_every_primary_key_column_exists_in_the_api_response(self):
        violations = []
        for obj, endpoint in _REGISTRY.items():
            known_fields = self.fields.get(obj.value)
            if known_fields is None:
                continue
            for column in endpoint.primary_key:
                if column in _INJECTED_COLUMNS:
                    continue
                if column not in known_fields:
                    violations.append(
                        f"{obj.value}: primary_key column '{column}' is not a real field on the API record"
                    )
        self.assertEqual([], violations)

    def test_every_declared_child_field_exists_in_the_api_response(self):
        violations = []
        for obj, endpoint in _REGISTRY.items():
            known_fields = self.fields.get(obj.value)
            if known_fields is None:
                continue
            for child in endpoint.children:
                if child.field not in known_fields:
                    violations.append(f"{obj.value}: child field '{child.field}' is not a real field on the API record")
        self.assertEqual([], violations)

    def test_every_declared_child_field_is_actually_requested(self):
        """Existence is not availability — the gap that let a dead child table ship.

        ``variant_parameters`` declared a ``values`` child against a field that is
        genuinely in the schema, so the existence check above passed. But ``values``
        is a *section on demand*: the endpoint only sends it when asked via
        ``include``, so with no request the child table was empty on every run, for
        every e-shop. A mock server hides this completely — Shoptet's docs mock
        returns the full example payload regardless of ``include`` — which is why
        this needs a schema-derived check rather than a recorded response.

        Deliberately keyed on the endpoint's *documented section list*, not on
        "absent from ``required``": plenty of fields are legitimately optional
        without being gated (an order with no ``shippings``), and flagging those
        would be noise.
        """
        violations = []
        for obj, endpoint in _REGISTRY.items():
            entry = self.extract.get(obj.value)
            if entry is None:
                continue
            sections = set(entry["documented_include_sections"])
            requested = set(endpoint.include_options) | {
                part.strip() for part in str(endpoint.extra_params.get("include", "")).split(",") if part.strip()
            }
            for child in endpoint.children:
                if child.field in sections and child.field not in requested:
                    violations.append(
                        f"{obj.value}: child '{child.field}' is a section on demand but is never requested — "
                        f"add it to include_options (user-selectable) or extra_params['include'] (always)"
                    )
        self.assertEqual([], violations)

    def test_no_include_option_is_invented(self):
        """Every offered section must be one the endpoint documents.

        Offering a section the API does not know means the request is rejected
        outright, so a typo here breaks the object entirely rather than degrading it.
        """
        violations = []
        for obj, endpoint in _REGISTRY.items():
            entry = self.extract.get(obj.value)
            if entry is None or not endpoint.include_options:
                continue
            unknown = sorted(set(endpoint.include_options) - set(entry["documented_include_sections"]))
            if unknown:
                violations.append(f"{obj.value}: include_options not documented by the endpoint: {unknown}")
        self.assertEqual([], violations)

    def test_page_size_matches_the_documented_cap(self):
        """Shoptet rejects an over-cap ``itemsPerPage``, and every collection differs.

        The caps live in prose in the API description, so the registry copies them by
        hand — which is exactly the kind of constant that rots silently. Nothing else
        in the suite pins them: raising ``articles`` from its real cap of 10 to 1000
        used to pass every test, and would have failed on the first live run.
        """
        violations = []
        for obj, endpoint in _REGISTRY.items():
            entry = self.extract.get(obj.value)
            if entry is None:
                continue
            cap = entry["items_per_page_cap"]
            if endpoint.items_per_page is None:
                if cap is not None and endpoint.mode.name in {"PAGINATED", "PER_STOCK"}:
                    violations.append(f"{obj.value}: endpoint documents a cap of {cap} but none is declared")
                continue
            if cap is None:
                violations.append(
                    f"{obj.value}: declares items_per_page={endpoint.items_per_page} but the endpoint "
                    f"documents no itemsPerPage parameter"
                )
            elif endpoint.items_per_page != cap:
                violations.append(
                    f"{obj.value}: declares items_per_page={endpoint.items_per_page}, documented cap is {cap}"
                )
        self.assertEqual([], violations)

    def test_a_keyless_object_with_a_change_window_must_be_full_load_only(self):
        """Guards the general shape of the ``abandoned_carts`` trap (blocker 5).

        An object with no primary key at all can never upsert, so if its fetch
        is *also* automatically narrowed by an incremental watermark
        (``changed_from``), an incremental run would silently overwrite the
        whole table with only the newest slice — destroying every previously
        accumulated row (this is exactly what happened before ``abandoned_carts``
        was given ``full_load_only=True`` and had its ``changed_from`` removed).
        This is written against the general shape (empty ``primary_key`` +
        ``changed_from``), not against ``abandoned_carts`` by name, so it still
        fires if a *future* object is added with the same trap.
        """
        violations = [
            obj.value
            for obj, endpoint in _REGISTRY.items()
            if not endpoint.primary_key and endpoint.changed_from and not endpoint.full_load_only
        ]
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
