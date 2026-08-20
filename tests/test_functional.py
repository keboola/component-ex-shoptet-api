"""Functional tests for component using VCR cassettes."""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")


def _discover_test_cases(functional_dir: str) -> list[str]:
    """Leaf test directories, plus any "chained" test containers.

    ``keboola.datadirtest.vcr.get_test_cases`` only finds leaf directories that
    have their own ``source/data/cassettes`` (a normal, standalone test). It
    does not know about a directory that instead holds an ordered sequence of
    sub-tests with no ``source`` of its own — a "chained" test, in
    ``DataDirTester`` terms (see ``DataDirTester._is_chained_test`` /
    ``TestChainedDatadirTest``) — used here for
    ``15_16_orders_changes_chain``. Chaining threads one sub-test's real
    ``out/state.json`` into the next sub-test's ``in/state.json``, at both
    record and replay time; that is the only way to prove a non-trivial
    previous-run watermark reaches a request, because a *standalone* test's
    ``in/state.json`` is unconditionally reset to ``{}`` in ``setUp`` (see
    ``TestDataDir._override_input_state``), so a merely-committed static seed
    would never actually reach the component on replay.

    ``VCRDataDirTester.run()`` already dispatches such a container to
    ``TestChainedDatadirTest`` correctly (via the inherited, unmodified
    ``DataDirTester._is_chained_test``); this just needs to be included in the
    discovered test names so pytest actually runs it.
    """
    leaf_cases = set(get_test_cases(functional_dir))
    root = Path(functional_dir)
    if not root.exists():
        return sorted(leaf_cases)
    chain_cases = {
        d.name
        for d in root.iterdir()
        if d.is_dir()
        and not d.name.startswith("_")
        and d.name not in leaf_cases
        and not (d / "source").exists()
        and any((child / "source" / "data" / "cassettes").exists() for child in d.iterdir() if child.is_dir())
    }
    return sorted(leaf_cases | chain_cases)


@pytest.mark.parametrize("test_name", _discover_test_cases(FUNCTIONAL_DIR))
def test_functional(test_name):
    """Run a single VCR functional test case (or a chained sequence of them)."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()
