"""Ground truth must never reach the estimator.

`case_outcomes.truth_*` holds both potential outcomes — what would have happened
under treatment *and* under no treatment. No real system can observe both. They
exist so the evaluation report can score the estimator against a known answer.

If they ever reached `app/causal/` or `app/engine/`, every number this project
produces would be circular: the estimator would be reading the answer it claims
to have derived. This is the Phase 3 ground-truth-isolation test applied to the
new schema, and it is the same reasoning that keeps the simulator out of `app/`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.models import Base
from app.models.case_outcome import TRUTH_COLUMNS

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"

#: The only two files that may name a truth column, by path relative to `app/`.
#:
#: The model that declares them, and the evaluation reporter — which section 10
#: of the pre-registration names as the single permitted reader, because the
#: report is where the estimator gets scored against the answer key.
#:
#: Matched on the **path**, not the basename. A basename allowlist would have
#: admitted any file called `evaluation.py` anywhere under `app/`, and would
#: already have admitted a stray `case_outcome.py` outside `models/`.
PERMITTED_PATHS = frozenset(
    {
        "models/case_outcome.py",
        "reporting/evaluation.py",
    }
)

#: Directories that must never reference ground truth, whether or not they
#: exist yet. `causal/` lands on Day 4; the guard is written first on purpose.
FORBIDDEN_PACKAGES = ("causal", "engine", "experiments", "services", "api", "repositories")


def python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("*.py")) if root.exists() else []


def references_truth(path: pathlib.Path) -> set[str]:
    """Truth column names appearing as identifiers, attributes, or literals."""
    source = path.read_text(encoding="utf-8")
    found: set[str] = set()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in TRUTH_COLUMNS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in TRUTH_COLUMNS:
            found.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found |= {c for c in TRUTH_COLUMNS if c == node.value}
    return found


class TestTruthColumnsExist:
    def test_the_column_list_matches_the_table(self) -> None:
        columns = set(Base.metadata.tables["case_outcomes"].columns.keys())
        assert set(TRUTH_COLUMNS) <= columns

    def test_every_truth_column_is_prefixed(self) -> None:
        """The prefix is what makes the guard below mechanical."""
        for column in TRUTH_COLUMNS:
            assert column.startswith("truth_")

    def test_no_other_table_carries_a_truth_column(self) -> None:
        for name, table in Base.metadata.tables.items():
            if name == "case_outcomes":
                continue
            leaked = [c for c in table.columns.keys() if c.startswith("truth_")]
            assert not leaked, f"{name} carries {leaked}"

    def test_truth_columns_are_nullable(self) -> None:
        """Only the simulator populates them; real ingestion leaves them NULL."""
        columns = Base.metadata.tables["case_outcomes"].columns
        for column in TRUTH_COLUMNS:
            assert columns[column].nullable


class TestIsolation:
    @staticmethod
    def _is_permitted(path: pathlib.Path) -> bool:
        return path.relative_to(APP_ROOT).as_posix() in PERMITTED_PATHS

    @pytest.mark.parametrize("package", FORBIDDEN_PACKAGES)
    def test_package_never_references_ground_truth(self, package: str) -> None:
        offenders: list[str] = []
        for path in python_files(APP_ROOT / package):
            if self._is_permitted(path):
                continue
            found = references_truth(path)
            if found:
                offenders.append(f"{path.relative_to(APP_ROOT)} -> {sorted(found)}")
        assert not offenders, f"ground truth leaked into app/{package}: {offenders}"

    def test_only_the_permitted_files_name_them(self) -> None:
        offenders: list[str] = []
        for path in python_files(APP_ROOT):
            if self._is_permitted(path):
                continue
            found = references_truth(path)
            if found:
                offenders.append(f"{path.relative_to(APP_ROOT)} -> {sorted(found)}")
        assert not offenders, f"ground truth referenced outside the allowlist: {offenders}"

    def test_the_allowlist_is_exactly_two_files(self) -> None:
        """Widening this has to be a deliberate act with a test to change."""
        assert PERMITTED_PATHS == {"models/case_outcome.py", "reporting/evaluation.py"}

    def test_the_allowlist_is_matched_by_path_not_by_name(self) -> None:
        """A basename allowlist would admit any `evaluation.py` under app/."""
        assert all("/" in entry for entry in PERMITTED_PATHS)

    def test_the_permitted_files_exist(self) -> None:
        """An allowlist entry for a file that does not exist is dead permission."""
        for entry in PERMITTED_PATHS:
            assert (APP_ROOT / entry).is_file(), entry

    def test_the_reporter_is_the_only_permitted_reader_outside_the_model(self) -> None:
        """Section 10 names `reporting/evaluation.py`. Nothing else in the
        application may reach the answer key, including the rest of
        `app/reporting/`."""
        readers = {
            path.relative_to(APP_ROOT).as_posix()
            for path in python_files(APP_ROOT)
            if references_truth(path)
        }
        assert readers <= PERMITTED_PATHS
        assert "reporting/evaluation.py" in PERMITTED_PATHS

    def test_the_guard_actually_scans_something(self) -> None:
        """A scan over zero files passes trivially and proves nothing."""
        assert len(python_files(APP_ROOT / "engine")) > 0
        assert len(python_files(APP_ROOT / "experiments")) > 0

    def test_the_guard_would_catch_a_leak(self) -> None:
        """Negative control for the detector itself."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write("value = outcome.truth_y0\n")
            leak_path = pathlib.Path(handle.name)

        try:
            assert references_truth(leak_path) == {"truth_y0"}
        finally:
            leak_path.unlink()

    def test_a_string_literal_also_counts_as_a_leak(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write('columns = ["truth_segment"]\n')
            leak_path = pathlib.Path(handle.name)

        try:
            assert references_truth(leak_path) == {"truth_segment"}
        finally:
            leak_path.unlink()


class TestSimulatorBoundaryUnchanged:
    def test_app_still_does_not_import_the_simulator(self) -> None:
        """The Phase 2 direction rule survives the roadmap change."""
        for path in python_files(APP_ROOT):
            source = path.read_text(encoding="utf-8")
            assert "import simulator" not in source, path
            assert "from simulator" not in source, path
