"""Recording helper for objects whose payload uses a JSON key literally named "code".

Why this exists
----------------
``keboola.vcr.recorder.VCRRecorder`` always prepends its own baseline sanitizer
(``create_default_sanitizer``, see ``keboola/vcr/recorder.py``) *ahead of* whatever
sanitizer chain a component declares via ``VCR_SANITIZERS`` — the two are composed,
not swapped. That baseline sanitizer redacts any JSON key found in
``DefaultSanitizer.DEFAULT_SENSITIVE_FIELDS``, which includes ``"code"`` (an
OAuth-authorization-code heuristic), purely by key name — regardless of the actual
value observed.

Shoptet's API uses ``"code"`` pervasively as a non-secret business identifier: it is
part of the primary key of orders, invoices, proforma invoices, credit notes,
delivery notes, proofs of payment, discount coupons, mailing lists, filtering/
surcharge parameters and stock supplies; it is also the identifier field on five of
the seven change feeds (``orders_changes`` among them); and it appears on nested,
unrelated fields such as ``eshop.currencies[].code`` / ``eshop.languages[].code``.
``component.py``'s own ``VCR_SANITIZERS`` deliberately excludes ``"code"`` from its
redacted-field list for exactly this reason (see the comment above
``_SENSITIVE_FIELDS`` there) — but the recorder's baseline sanitizer still wins
during recording, silently turning every one of those values into the literal
string ``"REDACTED"`` inside the persisted cassette, while ``expected/`` (built
from the live, unsanitized in-memory response at record time) keeps the real
value. Replay then deterministically diverges from ``expected/`` for any object
whose payload happens to use the key ``"code"`` anywhere — not a flaky test, a
guaranteed one.

This is safe to work around for this component specifically: the addon OAuth flow
here exchanges a permanent token for a short-lived one over a Bearer header (see
``ShoptetClient._access_token``) — there is no OAuth "code" grant parameter
anywhere in this component's request or response surface, so excluding "code"
from the redacted-field set never risks leaking a real secret.

What this script does
----------------------
Monkey-patches ``keboola.vcr.recorder.create_default_sanitizer`` to build its
``DefaultSanitizer`` from ``DEFAULT_SENSITIVE_FIELDS`` minus ``"code"`` — every
other default (``access_token``, ``refresh_token``, ``id_token``, ``client_id``,
``client_secret``, ``client_assertion``, ``password``, ``token``) is untouched —
then delegates straight to ``keboola.vcr.scaffolder.TestScaffolder``, i.e. the same
machinery ``python -m keboola.datadirtest scaffold`` uses. Only the cassette
*content that gets recorded* changes; nothing here hand-edits an already-recorded
cassette.

Usage
-----
Whenever a test whose object touches a literal "code" key needs to be re-recorded
(currently: ``10_eshop_single``, and the chained pair under
``15_16_orders_changes_chain/``), delete its folder under ``tests/functional/`` and
run this instead of the normal scaffold CLI, e.g.::

    rm -rf tests/functional/10_eshop_single
    uv run python tests/setup/record_code_safe.py \\
        --definitions tests/setup/configs.json --freeze-time 2026-08-20T12:00:00

For the chained change-feed pair (``01_default_window`` depends on nothing,
``02_incremental_watermark`` is chained from its own recorded ``out/state.json``),
delete ``tests/functional/15_16_orders_changes_chain/`` and use the dedicated
definitions file with ``--chain-state`` and a nested ``--output``, e.g.::

    rm -rf tests/functional/15_16_orders_changes_chain
    uv run python tests/setup/record_code_safe.py \\
        --definitions tests/setup/orders_changes_chain_configs.json \\
        --output tests/functional/15_16_orders_changes_chain \\
        --freeze-time 2026-08-20T12:00:00 --chain-state

(see README.md's "Current test coverage" section for the full picture).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from keboola.vcr import recorder as _recorder_module
from keboola.vcr.sanitizers import DefaultSanitizer, _collect_hash_values

_SAFE_DEFAULT_FIELDS = [field for field in DefaultSanitizer.DEFAULT_SENSITIVE_FIELDS if field != "code"]


def _code_safe_default_sanitizer(secrets: dict[str, Any]) -> DefaultSanitizer:
    """Drop-in replacement for ``keboola.vcr.sanitizers.create_default_sanitizer``."""
    secret_values: list[str] = []
    _collect_hash_values(secrets, secret_values)
    return DefaultSanitizer(sensitive_fields=_SAFE_DEFAULT_FIELDS, sensitive_values=secret_values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--definitions", default="tests/setup/configs.json")
    parser.add_argument("--output", default="tests/functional")
    parser.add_argument("--component", default="src/component.py")
    parser.add_argument("--freeze-time", default=None)
    parser.add_argument("--chain-state", action="store_true")
    parser.add_argument("--regenerate", action="store_true")
    args = parser.parse_args()

    # Patch the name as imported into the recorder module's namespace — patching
    # keboola.vcr.sanitizers.create_default_sanitizer would not affect the recorder,
    # which already bound its own reference to the original function at import time.
    # The suppression below is unavoidable, not laziness: ty types this attribute as
    # the specific original function object, so rebinding it to any replacement — even
    # one with an identical signature — is unassignable by definition.
    _recorder_module.create_default_sanitizer = _code_safe_default_sanitizer  # ty: ignore[invalid-assignment]

    from keboola.vcr.scaffolder import TestScaffolder

    created = TestScaffolder().scaffold_from_json(
        definitions_file=Path(args.definitions),
        output_dir=Path(args.output),
        component_script=Path(args.component),
        record=True,
        freeze_time_at=args.freeze_time,
        chain_state=args.chain_state,
        regenerate=args.regenerate,
    )
    print(f"Recorded {len(created)} test folder(s) with the code-safe sanitizer:")
    for path in created:
        print(f"  - {path}")


if __name__ == "__main__":
    main()
