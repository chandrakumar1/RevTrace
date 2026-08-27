"""Hygiene guards.

These stop nondeterminism and persistence coupling from creeping back in later.

Calls are detected by parsing the AST rather than by scanning text: a substring
scan matches prose in docstrings that *explains* why a call is banned, and it
cannot tell `random.randint(...)` from `self._random.randint(...)`.

Module-loading is checked in a clean subprocess. Asserting on `sys.modules`
inside the pytest session would be meaningless, because the Phase 1 tests in the
same session legitimately import the database layer.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
import simulator

SIMULATOR_ROOT = Path(simulator.__file__).resolve().parent

#: Fully-qualified calls that would make output non-reproducible.
BANNED_CALLS = frozenset(
    {
        "uuid.uuid4",
        "uuid.uuid1",
        "datetime.now",
        "datetime.utcnow",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "time.time",
        "os.urandom",
        "random.random",
        "random.randint",
        "random.randrange",
        "random.choice",
        "random.shuffle",
        "random.randbytes",
        "secrets.token_hex",
        "secrets.token_bytes",
        "secrets.choice",
    }
)

#: Modules the simulator must never import — persistence and network are
#: separate layers entirely.
BANNED_IMPORTS = frozenset(
    {
        "app.db",
        "app.db.session",
        "app.db.base",
        "sqlalchemy",
        "psycopg",
        "requests",
        "httpx",
        "urllib.request",
        "razorpay",
        "google.generativeai",
    }
)

#: The only first-party modules the simulator is allowed to reuse.
PERMITTED_APP_IMPORTS = frozenset({"app.models.enums", "app.models.mixins", "app.core.money"})


def _source_files() -> list[Path]:
    files = sorted(SIMULATOR_ROOT.rglob("*.py"))
    assert files, f"no simulator sources found under {SIMULATOR_ROOT}"
    return files


def _dotted_name(node: ast.expr) -> str | None:
    """Render a call target as a dotted name, or None if it is not one."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _calls_in(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name:
                found.append((name, node.lineno))
    return found


def _imports_in(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
    return found


def _run_clean(code: str) -> str:
    """Execute code in a fresh interpreter and return its stdout."""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


class TestNoNondeterminism:
    def test_no_banned_calls_anywhere(self) -> None:
        offenders = [
            f"{path.name}:{line} -> {name}()"
            for path in _source_files()
            for name, line in _calls_in(path)
            if name in BANNED_CALLS
        ]
        assert not offenders, f"nondeterministic calls found: {offenders}"

    @pytest.mark.parametrize("banned", sorted(BANNED_CALLS))
    def test_specific_banned_call_absent(self, banned: str) -> None:
        offenders = [
            f"{path.name}:{line}"
            for path in _source_files()
            for name, line in _calls_in(path)
            if name == banned
        ]
        assert not offenders, f"{banned}() called at: {offenders}"

    def test_random_module_imported_only_by_rng(self) -> None:
        for path in _source_files():
            if path.name == "rng.py":
                continue
            modules = {name for name, _ in _imports_in(path)}
            assert "random" not in modules, f"{path.name} imports random directly"

    def test_rng_uses_only_a_seeded_random_instance(self) -> None:
        """rng.py may construct random.Random(seed); it must not use the module API."""
        calls = {name for name, _ in _calls_in(SIMULATOR_ROOT / "rng.py")}
        assert "random.Random" in calls
        assert not (calls & BANNED_CALLS)

    def test_uuid_generation_goes_through_the_rng(self) -> None:
        calls = {name for name, _ in _calls_in(SIMULATOR_ROOT / "rng.py")}
        assert "uuid.UUID" in calls
        assert "uuid.uuid4" not in calls


class TestNoPersistenceCoupling:
    @pytest.mark.parametrize("module", sorted(BANNED_IMPORTS))
    def test_banned_module_not_imported(self, module: str) -> None:
        offenders = [
            f"{path.name}:{line}"
            for path in _source_files()
            for name, line in _imports_in(path)
            if name == module or name.startswith(f"{module}.")
        ]
        assert not offenders, f"{module} imported at: {offenders}"

    def test_only_permitted_app_modules_are_reused(self) -> None:
        offenders = [
            f"{path.name}:{line} -> {name}"
            for path in _source_files()
            for name, line in _imports_in(path)
            if name.startswith("app.") and name not in PERMITTED_APP_IMPORTS
        ]
        assert not offenders, f"unexpected first-party imports: {offenders}"

    def test_importing_the_simulator_loads_no_db_session(self) -> None:
        """Verified in a clean interpreter — the pytest session imports app.main."""
        output = _run_clean("import simulator, sys; print('app.db.session' in sys.modules)")
        assert output == "False"

    def test_simulating_loads_no_db_session(self) -> None:
        output = _run_clean(
            "import simulator, sys\n"
            "simulator.simulate('S04', seed=1)\n"
            "print('app.db.session' in sys.modules)"
        )
        assert output == "False"

    def test_simulating_creates_no_sqlalchemy_engine(self) -> None:
        output = _run_clean(
            "import simulator, sys\n"
            "simulator.simulate('S04', seed=1)\n"
            "print(any(m.startswith('psycopg') for m in sys.modules))"
        )
        assert output == "False"

    def test_simulating_loads_no_http_client(self) -> None:
        output = _run_clean(
            "import simulator, sys\n"
            "simulator.simulate('S04', seed=1)\n"
            "print(any(m in sys.modules for m in ('requests', 'httpx', 'urllib3')))"
        )
        assert output == "False"


class TestZeroThirdPartyDependencies:
    def test_declares_no_dependencies(self) -> None:
        pyproject = (SIMULATOR_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "dependencies = []" in pyproject

    def test_no_razorpay_or_ai_sdk_referenced(self) -> None:
        for path in _source_files():
            modules = {name for name, _ in _imports_in(path)}
            assert not any(m.startswith("razorpay") for m in modules)
            assert not any(m.startswith("google") for m in modules)
            assert not any(m.startswith("openai") for m in modules)

    def test_every_import_is_stdlib_or_first_party(self) -> None:
        stdlib = set(sys.stdlib_module_names)
        offenders: list[str] = []
        for path in _source_files():
            for name, line in _imports_in(path):
                root = name.split(".")[0]
                if root in stdlib or root in {"simulator", "app"}:
                    continue
                offenders.append(f"{path.name}:{line} -> {name}")
        assert not offenders, f"third-party imports found: {offenders}"
