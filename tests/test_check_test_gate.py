"""
Tests for scripts/check_test_gate.py: the check the workflow runs BEFORE `python -m pytest`.

`python -m pytest` exits 0 after running nothing when pytest.ini, a conftest.py, the environment or
the set of test files says so. The gate must turn each of those into a red step, and it must say so
in a PUBLIC log without echoing a line of any file or a word of pytest's own output.

Groups
    pytest.ini        what the strict reader accepts and refuses, and that it reads what pytest reads
    stray files       conftest.py and the other places pytest reads configuration from
    environment       PYTEST_* variables, counted and never named
    collect-only run  the command, the clean environment, the count, every way it can fail
    main              exit codes and output, in process
    real processes    a throwaway project holding a copy of the script, one failure mode at a time,
                      and the real tree, which must pass

Every offending file or value below carries MARKER, so an echo is attributable.
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_test_gate as gate

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_test_gate.py"
# No spaces and no secret shape: only "never echo what a file says" keeps it out of a public CI log.
MARKER = "Gate-Marker-77213-Palm-Residences-Tower"
GOOD_INI = "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -q\n"
# The only sentences the gate may print about pytest.ini; a line number is the only variable part.
FIXED_INI_PHRASE = re.compile(
    r"pytest\.ini (?:is missing|cannot be read|line \d+ is not one the gate accepts|has no \[pytest\] section"
    r"|does not set (?:testpaths to tests|pythonpath to \.)|addopts holds something other than -q)"
)


# ---------------------------------------------------------------------------
# pytest.ini
# ---------------------------------------------------------------------------
def ini_problems_of(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "pytest.ini"
    path.write_bytes(text.encode("utf-8"))
    return gate.ini_problems(path)


ACCEPTED_INIS = {
    "the expected file": GOOD_INI,
    "without addopts": "[pytest]\ntestpaths = tests\npythonpath = .\n",
    "two -q": "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -q -q\n",
    "empty addopts": "[pytest]\ntestpaths = tests\npythonpath = .\naddopts =\n",
    "comments and blank lines": "# the suite\n; a note\n\n[pytest]\n# why\ntestpaths = tests\n\npythonpath = .\naddopts = -q\n",
    "another order": "[pytest]\naddopts = -q\npythonpath = .\ntestpaths = tests\n",
    "spacing": "[pytest]\ntestpaths=tests\npythonpath   =   .\naddopts =\t-q  \n",
    "trailing blanks": "[pytest]  \ntestpaths = tests  \npythonpath = .\n",
    "windows line endings": "[pytest]\r\ntestpaths = tests\r\npythonpath = .\r\naddopts = -q\r\n",
    "no final newline": "[pytest]\ntestpaths = tests\npythonpath = .",
}


@pytest.mark.parametrize("label", sorted(ACCEPTED_INIS))
def test_the_expected_pytest_ini_is_accepted(tmp_path, label):
    assert ini_problems_of(tmp_path, ACCEPTED_INIS[label]) == []


# Prints what pytest itself made of the three options in the pytest.ini next to it.
READ_BACK_PLUGIN = (
    "import json\n"
    "\n"
    "\n"
    "def pytest_configure(config):\n"
    "    options = {name: config.getini(name) for name in ('testpaths', 'pythonpath', 'addopts')}\n"
    "    print('READ-BACK ' + json.dumps(options, default=str))\n"
)


@pytest.mark.parametrize("label", sorted(ACCEPTED_INIS))
def test_what_the_gate_accepts_is_read_by_pytest_itself_as_the_same_three_options(tmp_path, label):
    """The reason for the strict grammar: the gate and pytest must not read one file two ways."""
    (tmp_path / "pytest.ini").write_bytes(ACCEPTED_INIS[label].encode("utf-8"))
    (tmp_path / "read_back.py").write_text(READ_BACK_PLUGIN, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "read_back", "--collect-only", "-p", "no:cacheprovider"],
        cwd=tmp_path, env=clean_environment(PYTHONPATH=str(tmp_path)), capture_output=True, text=True, timeout=300,
    )
    printed = [line for line in done.stdout.splitlines() if line.startswith("READ-BACK ")]
    assert len(printed) == 1, (done.stdout, done.stderr)
    options = json.loads(printed[0].removeprefix("READ-BACK "))
    assert options["testpaths"] == ["tests"]
    assert [Path(p).resolve() for p in options["pythonpath"]] == [tmp_path.resolve()]  # "." relative to the pytest.ini
    assert set(options["addopts"]) <= {"-q"}


BAD_INIS = {
    "collect only": GOOD_INI.replace("-q", "-q --collect-only"),
    "collect only, short": GOOD_INI.replace("-q", "--co"),
    "keyword filter": GOOD_INI.replace("-q", "-q -k nothing_matches_this"),
    "marker filter": GOOD_INI.replace("-q", "-q -m nothing"),
    "deselect": GOOD_INI.replace("-q", "-q --deselect tests/test_main.py"),
    "ignore": GOOD_INI.replace("-q", "-q --ignore=tests"),
    "plugin switch": GOOD_INI.replace("-q", "-q -p no:cacheprovider"),
    "stop at the first failure": GOOD_INI.replace("-q", "-q -x"),
    "doctest only": GOOD_INI.replace("-q", "-q --doctest-modules"),
    "quieter": GOOD_INI.replace("-q", "-qq"),
    "verbose": GOOD_INI.replace("-q", "-v"),
    "unbalanced quote": GOOD_INI.replace("-q", "-q 'oops"),
    "a comment after the option": GOOD_INI.replace("-q", "-q # --collect-only"),
    "testpaths one file": GOOD_INI.replace("testpaths = tests", "testpaths = tests/test_main.py"),
    "testpaths elsewhere": GOOD_INI.replace("testpaths = tests", "testpaths = docs"),
    "testpaths widened": GOOD_INI.replace("testpaths = tests", "testpaths = tests docs"),
    "testpaths with a slash": GOOD_INI.replace("testpaths = tests", "testpaths = tests/"),
    "testpaths missing": GOOD_INI.replace("testpaths = tests\n", ""),
    "testpaths empty": GOOD_INI.replace("testpaths = tests", "testpaths ="),
    "pythonpath moved": GOOD_INI.replace("pythonpath = .", "pythonpath = src"),
    "pythonpath missing": GOOD_INI.replace("pythonpath = .\n", ""),
    "norecursedirs": GOOD_INI + "norecursedirs = tests\n",
    "python_files": GOOD_INI + "python_files = nothing_*.py\n",
    "python_functions": GOOD_INI + "python_functions = nothing_*\n",
    "confcutdir": GOOD_INI + "confcutdir = tests\n",
    "filterwarnings": GOOD_INI + "filterwarnings = ignore\n",
    "minversion": GOOD_INI + "minversion = 99\n",
    "capitalised option": GOOD_INI + "ADDOPTS = --co\n",
    "upper-case option names": "[pytest]\nTESTPATHS = tests\nPYTHONPATH = .\nADDOPTS = -q\n",
    "duplicate addopts": GOOD_INI + "addopts = --co\n",
    "duplicate testpaths": GOOD_INI + "testpaths = docs\n",
    # The last value is the expected one, so only refusing the repeat itself catches these: a reader that
    # keeps the first value (or errors out) reads a different file than one that keeps the last.
    "duplicate addopts, the unsafe one first": GOOD_INI.replace("addopts = -q\n", "addopts = --co\naddopts = -q\n"),
    "duplicate testpaths, the unsafe one first": GOOD_INI.replace("testpaths = tests\n", "testpaths = docs\ntestpaths = tests\n"),
    "duplicate addopts, repeated as it was": GOOD_INI + "addopts = -q\n",
    "duplicate testpaths, repeated as it was": GOOD_INI + "testpaths = tests\n",
    "colon instead of equals": "[pytest]\ntestpaths: tests\npythonpath: .\n",
    "continuation line": "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -q\n    --collect-only\n",
    "indented option": "[pytest]\n  testpaths = tests\npythonpath = .\n",
    "indented comment": "[pytest]\n  # a note\ntestpaths = tests\npythonpath = .\n",
    "second section": GOOD_INI + "[tool:pytest]\naddopts = --co\n",
    "the header twice": GOOD_INI + "[pytest]\naddopts = --co\n",
    "an empty second section": GOOD_INI + "[pytest]\n",
    "default section": "[DEFAULT]\naddopts = --co\n" + GOOD_INI,
    "wrong section name": GOOD_INI.replace("[pytest]", "[tool:pytest]"),
    "text after the header": GOOD_INI.replace("[pytest]", "[pytest] # x"),
    "no section": "testpaths = tests\npythonpath = .\n",
    "options before the header": "testpaths = tests\n[pytest]\npythonpath = .\n",
    "empty file": "",
    "comments only": "# nothing here\n",
    "unparseable header": "[pytest\ntestpaths = tests\n",
    "byte-order mark": "\ufeff" + GOOD_INI,
    "a stray word": GOOD_INI + "collect-only\n",
    "a null byte": GOOD_INI.replace("tests", "te\x00sts"),
    "a form feed starting a new line": GOOD_INI.replace("-q", "-q\x0c--collect-only"),
}


@pytest.mark.parametrize("label", sorted(BAD_INIS))
def test_a_changed_pytest_ini_is_refused_in_fixed_phrases(tmp_path, label):
    problems = ini_problems_of(tmp_path, BAD_INIS[label])
    assert problems, label
    assert all(FIXED_INI_PHRASE.fullmatch(problem) for problem in problems), problems


def test_a_refused_pytest_ini_never_has_its_text_echoed(tmp_path):
    text = GOOD_INI.replace("-q", f"-q --ignore={MARKER}") + f"{MARKER} = 1\n[{MARKER}]\n"
    problems = ini_problems_of(tmp_path, text)
    assert problems and MARKER not in " ".join(problems)


def test_the_line_of_an_unexpected_line_is_named_by_number(tmp_path):
    assert ini_problems_of(tmp_path, "[pytest]\ntestpaths = tests\npythonpath = .\nfoo = bar\n") == [
        "pytest.ini line 4 is not one the gate accepts"
    ]


def test_a_missing_pytest_ini_is_reported(tmp_path):
    assert gate.ini_problems(tmp_path / "pytest.ini") == ["pytest.ini is missing"]


def test_a_pytest_ini_that_is_not_text_or_not_a_file_is_reported_not_raised(tmp_path):
    (tmp_path / "pytest.ini").write_bytes(b"\xff\xfe\x00[pytest]")
    assert gate.ini_problems(tmp_path / "pytest.ini") == ["pytest.ini cannot be read"]
    directory = tmp_path / "sub" / "pytest.ini"
    directory.mkdir(parents=True)
    assert gate.ini_problems(directory) == ["pytest.ini cannot be read"]


def test_the_real_pytest_ini_is_accepted():
    assert gate.ini_problems(ROOT / "pytest.ini") == []


# ---------------------------------------------------------------------------
# Stray files and the environment
# ---------------------------------------------------------------------------
def touch(root: Path, *relative: str) -> None:
    for name in relative:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {MARKER}\n", encoding="utf-8")


@pytest.mark.parametrize(
    "stray",
    ["conftest.py", "tests/conftest.py", "src/conftest.py", "tests/sub/dir/conftest.py", "scripts/conftest.py", "docs/tox.ini", "scripts/pyproject.toml"]
    + list(gate.STRAY_CONFIG),
)
def test_a_conftest_or_a_second_pytest_configuration_file_is_reported_by_name_and_count(tmp_path, stray):
    touch(tmp_path, stray)
    name = Path(stray).name
    assert gate.stray_config_problems(tmp_path) == [f"{name} exists (1 found)"]


def test_the_stray_files_are_counted_and_never_located(tmp_path):
    touch(tmp_path, "conftest.py", "tests/conftest.py", "a/b/conftest.py", "tox.ini", "pytest.toml")
    assert gate.stray_config_problems(tmp_path) == ["conftest.py exists (3 found)", "tox.ini exists (1 found)", "pytest.toml exists (1 found)"]


@pytest.mark.parametrize(
    "harmless",
    [
        "tests/test_conftest.py",
        "my_conftest.py",
        "tests/conftest_helpers.py",
        "tests/conftest.py.txt",
        "docs/pytest.ini.md",
        "pytest.ini",
        # pytest never descends into hidden directories, and the checkout of a CI runner has no other kind
        ".venv/lib/python3.12/site-packages/pkg/conftest.py",
        ".venv/pyproject.toml",
        ".git/hooks/conftest.py",
        "tests/.venv/conftest.py",
    ],
)
def test_files_that_only_look_like_pytest_configuration_are_not_reported(tmp_path, harmless):
    touch(tmp_path, harmless)
    assert gate.stray_config_problems(tmp_path) == []


def test_the_real_tree_has_no_stray_pytest_configuration():
    assert gate.stray_config_problems(ROOT) == []


@pytest.mark.parametrize("name", ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "pytest_addopts"])
def test_a_pytest_variable_is_counted_and_neither_named_nor_quoted(name):
    problems = gate.environment_problems({name: MARKER, "PATH": "x"})
    assert problems == ["1 PYTEST_* environment variable(s) set"]
    assert MARKER not in problems[0] and name.lower() not in problems[0].lower()


def test_several_pytest_variables_are_counted():
    assert gate.environment_problems({"PYTEST_ADDOPTS": "a", "PYTEST_PLUGINS": "b", "PYTEST_X": "c"}) == ["3 PYTEST_* environment variable(s) set"]


def test_other_variables_are_not_a_problem():
    assert gate.environment_problems({"PATH": "x", "PYTHONPATH": "y", "PYTESTER": "z", "MY_PYTEST_FLAG": "1"}) == []
    assert gate.environment_problems({}) == []


# ---------------------------------------------------------------------------
# Counting test modules
# ---------------------------------------------------------------------------
def test_test_modules_are_counted_recursively_and_only_by_name(tmp_path):
    touch(tmp_path, "tests/test_a.py", "tests/sub/test_b.py", "tests/helpers.py", "tests/test_c.txt", "tests/sub/deeper/test_d.py", "elsewhere/test_e.py")
    assert gate.count_test_modules(tmp_path) == 3
    assert gate.count_test_modules(tmp_path / "no_such_project") == 0


def test_a_directory_named_like_a_test_module_is_not_one(tmp_path):
    (tmp_path / "tests" / "test_dir.py").mkdir(parents=True)
    assert gate.count_test_modules(tmp_path) == 0


def test_too_few_test_modules_are_reported_with_numbers_only(tmp_path):
    touch(tmp_path, "tests/test_a.py")
    assert gate.module_problems(tmp_path) == [f"1 test modules found under tests, at least {gate.MIN_TEST_MODULES} expected"]
    touch(tmp_path, *[f"tests/test_{n}.py" for n in range(gate.MIN_TEST_MODULES)])
    assert gate.module_problems(tmp_path) == []


def test_the_static_checks_are_the_sum_of_the_four_kinds(tmp_path):
    touch(tmp_path, "conftest.py")
    problems = gate.static_problems(tmp_path, {"PYTEST_ADDOPTS": MARKER})
    assert problems[0] == "pytest.ini is missing"
    assert "conftest.py exists (1 found)" in problems
    assert "1 PYTEST_* environment variable(s) set" in problems
    assert any("test modules found under tests" in problem for problem in problems)
    assert MARKER not in " ".join(problems)


# ---------------------------------------------------------------------------
# The collect-only run
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("output", "count"),
    [
        ("tests/test_a.py: 5\ntests/test_b.py: 7\n\n", 12),  # -qq, what pytest.ini's -q plus the command's -q give
        ("tests/a.py::test_x[0]\ntests/a.py::test_x[1]\n\n2 tests collected in 0.02s\n", 2),  # -q alone
        ("1 test collected in 0.01s\n", 1),
        ("5/9 tests collected (4 deselected) in 0.02s\n", 5),
        ("tests/a.py::test_x[0]\ntests/a.py::test_x[1]\ntests/a.py::test_y\n", 3),  # node ids without the summary
        ("tests/test_a.py: 5\ntests/test_a.py:12: DeprecationWarning: x\n", 5),  # a warning line is not a count
    ],
)
def test_the_count_is_read_from_every_verbosity_pytest_prints(output, count):
    assert gate.parse_collected(output) == count


@pytest.mark.parametrize("output", ["", "\n", "hello", "no tests collected in 0.01s\n", "ERROR collecting tests/test_a.py\n", "tests/a.py: many\n"])
def test_output_without_a_count_reads_as_none(output):
    assert gate.parse_collected(output) is None


class FakeRun:
    """Stands in for subprocess.run inside the gate and records how it was called."""

    def __init__(self, stdout: str = "", returncode: int = 0, raises: Exception | None = None):
        self.stdout, self.returncode, self.raises = stdout, returncode, raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(command, self.returncode, stdout=self.stdout, stderr=f"stderr {MARKER}")


def fake_subprocess(monkeypatch, fake: FakeRun) -> None:
    """Only the gate sees the stand-in; the rest of the process keeps the real subprocess module."""
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=fake, TimeoutExpired=subprocess.TimeoutExpired))


def run_collect(monkeypatch, tmp_path, fake: FakeRun, environ=None):
    fake_subprocess(monkeypatch, fake)
    return gate.collect_problems(tmp_path, {"PATH": "x"} if environ is None else environ)


def test_the_collect_only_run_is_the_plain_command_in_the_project_with_a_clean_environment(monkeypatch, tmp_path):
    fake = FakeRun(stdout=f"tests/test_a.py: {gate.MIN_COLLECTED_TESTS}\n")
    problems, collected = run_collect(monkeypatch, tmp_path, fake, {"PATH": "x", "PYTEST_ADDOPTS": "--co", "pytest_plugins": "p", "HOME": "h"})
    assert (problems, collected) == ([], gate.MIN_COLLECTED_TESTS)
    (command, kwargs), = fake.calls
    assert command == [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    assert kwargs["cwd"] == tmp_path
    assert set(kwargs["env"]) == {"PATH", "HOME", "PYTHONIOENCODING"}
    assert kwargs["timeout"] == gate.COLLECT_TIMEOUT_SECONDS and kwargs["capture_output"] is True


def test_a_count_exactly_at_the_floor_passes_and_one_under_fails(monkeypatch, tmp_path):
    floor = gate.MIN_COLLECTED_TESTS
    assert run_collect(monkeypatch, tmp_path, FakeRun(stdout=f"{floor} tests collected in 1s\n")) == ([], floor)
    problems, collected = run_collect(monkeypatch, tmp_path, FakeRun(stdout=f"{floor - 1} tests collected in 1s\n"))
    assert problems == [f"{floor - 1} tests collected, at least {floor} expected"] and collected == floor - 1


@pytest.mark.parametrize("code", [1, 2, 3, 4, 5])
def test_a_failed_collect_only_run_is_reported_by_exit_code_alone(monkeypatch, tmp_path, code):
    fake = FakeRun(stdout=f"E   RuntimeError: {MARKER}\n{gate.MIN_COLLECTED_TESTS} tests collected in 1s\n", returncode=code)
    problems, collected = run_collect(monkeypatch, tmp_path, fake)
    assert problems == [f"the collect-only run failed (exit code {code})"] and collected is None
    assert MARKER not in " ".join(problems)


def test_a_collect_only_run_that_hangs_is_reported(monkeypatch, tmp_path):
    fake = FakeRun(raises=subprocess.TimeoutExpired([MARKER], 1))
    problems, collected = run_collect(monkeypatch, tmp_path, fake)
    assert problems == [f"the collect-only run did not finish within {gate.COLLECT_TIMEOUT_SECONDS} seconds"] and collected is None
    assert MARKER not in " ".join(problems)


def test_output_the_gate_cannot_read_is_a_failure_and_not_a_pass(monkeypatch, tmp_path):
    problems, collected = run_collect(monkeypatch, tmp_path, FakeRun(stdout=f"{MARKER}\n"))
    assert problems == ["the collect-only run printed no test count the gate can read"] and collected is None
    assert MARKER not in " ".join(problems)


def test_check_runs_pytest_only_when_everything_that_can_be_seen_is_in_order(monkeypatch, tmp_path):
    fake = FakeRun(stdout=f"{gate.MIN_COLLECTED_TESTS} tests collected in 1s\n")
    fake_subprocess(monkeypatch, fake)
    problems, collected = gate.check(tmp_path, {})  # no pytest.ini, no modules
    assert problems and collected is None and fake.calls == []


def test_check_runs_the_collect_only_run_after_the_static_checks_pass(monkeypatch, tmp_path):
    (tmp_path / "pytest.ini").write_text(GOOD_INI, encoding="utf-8")
    touch(tmp_path, *[f"tests/test_{n}.py" for n in range(gate.MIN_TEST_MODULES)])
    fake = FakeRun(stdout=f"{gate.MIN_COLLECTED_TESTS + 7} tests collected in 1s\n")
    fake_subprocess(monkeypatch, fake)
    assert gate.check(tmp_path, {}) == ([], gate.MIN_COLLECTED_TESTS + 7)
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# main, in process
# ---------------------------------------------------------------------------
def test_main_prints_each_problem_as_a_fixed_line_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(gate, "check", lambda root, environ: (["pytest.ini is missing", "3 PYTEST_* environment variable(s) set"], None))
    assert gate.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "test gate: pytest.ini is missing\ntest gate: 3 PYTEST_* environment variable(s) set\n"


def test_main_prints_numbers_only_and_exits_0_when_the_gate_holds(monkeypatch, capsys):
    monkeypatch.setattr(gate, "check", lambda root, environ: ([], 5123))
    monkeypatch.setattr(gate, "count_test_modules", lambda root: 24)
    assert gate.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == "test gate: ok (24 test modules, 5123 tests collected)\n" and captured.err == ""


def test_main_reports_the_class_of_an_unplanned_failure_and_nothing_else(monkeypatch, capsys):
    def explode(root, environ):
        raise RuntimeError(f"unplanned {MARKER}")

    monkeypatch.setattr(gate, "check", explode)
    assert gate.main([]) == 1
    captured = capsys.readouterr()
    assert captured.err == "test gate: the gate itself failed (RuntimeError)\n" and captured.out == ""


def test_main_takes_no_arguments(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(gate, "check", lambda root, environ: called.append(1) or ([], 1))
    assert gate.main(["--skip"]) == 1
    assert called == [] and capsys.readouterr().err == "test gate: takes no arguments\n"


def test_main_hands_check_the_script_s_own_project_and_the_real_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "check", lambda root, environ: seen.append((root, environ)) or ([], 1))
    gate.main([])
    assert seen == [(ROOT, os.environ)]


# ---------------------------------------------------------------------------
# The script itself
# ---------------------------------------------------------------------------
def test_the_script_imports_only_the_standard_library():
    """It runs before the suite and decides whether the suite may run: a broken dependency must not disable it."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").partition(".")[0])
    assert imported and imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


def test_the_floors_and_the_timeout_are_sane():
    assert isinstance(gate.MIN_TEST_MODULES, int) and isinstance(gate.MIN_COLLECTED_TESTS, int)
    assert 0 < gate.MIN_TEST_MODULES < gate.MIN_COLLECTED_TESTS and 0 < gate.COLLECT_TIMEOUT_SECONDS <= 1800


def test_the_expected_pytest_ini_values_are_the_ones_the_repository_holds():
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert gate.EXPECTED_OPTIONS == {"testpaths": "tests", "pythonpath": "."} and gate.ADDOPTS_ALLOWED == {"-q"}
    for name, value in gate.EXPECTED_OPTIONS.items():
        assert f"{name} = {value}\n" in ini


# ---------------------------------------------------------------------------
# Real processes: a throwaway project holding a copy of the script
# ---------------------------------------------------------------------------
def clean_environment(**extra: str) -> dict[str, str]:
    """The environment of this process without pytest's own variables (PYTEST_CURRENT_TEST is set inside a test)."""
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("PYTEST") and name != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def stub_test_module(count: int, *, skipped: bool = False, broken: bool = False) -> str:
    if broken:
        return f"raise RuntimeError('{MARKER}')\n"
    if skipped:
        return f"import pytest\n\npytest.skip('{MARKER}', allow_module_level=True)\n"
    lines = ["import pytest", "", "", f"@pytest.mark.parametrize('n', range({count}))", "def test_stub(n):", "    pass", ""]
    return "\n".join(lines)


def make_project(base: Path, *, ini: str | None = GOOD_INI, modules: int | None = None, per_module: int | None = None) -> Path:
    """
    A project that passes: a copy of the script, a pytest.ini, and exactly as many trivial test modules
    and tests as the real floors ask for. Each test below then breaks one thing.
    """
    modules = gate.MIN_TEST_MODULES if modules is None else modules
    per_module = math.ceil(gate.MIN_COLLECTED_TESTS / gate.MIN_TEST_MODULES) if per_module is None else per_module
    project = base / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(SCRIPT, project / "scripts" / "check_test_gate.py")
    if ini is not None:
        (project / "pytest.ini").write_text(ini, encoding="utf-8")
    for number in range(modules):
        (project / "tests" / f"test_stub_{number:02d}.py").write_text(stub_test_module(per_module), encoding="utf-8")
    return project


def run_gate(project: Path, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.check_test_gate"], cwd=project, env=clean_environment(**extra_env),
        capture_output=True, text=True, timeout=300,
    )


def set_ini(project: Path, text: str) -> None:
    (project / "pytest.ini").write_text(text, encoding="utf-8")


def delete_modules(project: Path, keep: int) -> None:
    for path in sorted((project / "tests").glob("test_stub_*.py"))[keep:]:
        path.unlink()


def replace_module(project: Path, number: int, source: str) -> None:
    (project / "tests" / f"test_stub_{number:02d}.py").write_text(source, encoding="utf-8")


SKIPPING_CONFTEST = (
    "def pytest_collection_modifyitems(config, items):\n"
    f"    items[:] = []  # {MARKER}\n"
)

# label -> (what to break, extra environment, the one line the gate must print)
FAILURE_MODES = {
    "collect-only in addopts": (
        lambda p: set_ini(p, GOOD_INI.replace("-q", f"-q --collect-only {MARKER}")),
        {}, r"pytest\.ini addopts holds something other than -q",
    ),
    "an --ignore in addopts": (
        lambda p: set_ini(p, GOOD_INI.replace("-q", f"-q --ignore=tests/{MARKER}")),
        {}, r"pytest\.ini addopts holds something other than -q",
    ),
    "a deselect in addopts": (
        lambda p: set_ini(p, GOOD_INI.replace("-q", f"-q --deselect {MARKER}")),
        {}, r"pytest\.ini addopts holds something other than -q",
    ),
    "testpaths narrowed": (
        lambda p: set_ini(p, GOOD_INI.replace("testpaths = tests", f"testpaths = tests/{MARKER}")),
        {}, r"pytest\.ini does not set testpaths to tests",
    ),
    "a second section": (
        lambda p: set_ini(p, GOOD_INI + f"[{MARKER}]\naddopts = --co\n"),
        {}, r"pytest\.ini line 5 is not one the gate accepts",
    ),
    "pytest.ini deleted": (lambda p: (p / "pytest.ini").unlink(), {}, r"pytest\.ini is missing"),
    "a skipping conftest.py at the root": (
        lambda p: (p / "conftest.py").write_text(SKIPPING_CONFTEST, encoding="utf-8"),
        {}, r"conftest\.py exists \(1 found\)",
    ),
    "a skipping conftest.py inside tests": (
        lambda p: (p / "tests" / "conftest.py").write_text(SKIPPING_CONFTEST, encoding="utf-8"),
        {}, r"conftest\.py exists \(1 found\)",
    ),
    "PYTEST_ADDOPTS in the environment": (lambda p: None, {"PYTEST_ADDOPTS": f"--collect-only {MARKER}"}, r"1 PYTEST_\* environment variable\(s\) set"),
    "PYTEST_PLUGINS in the environment": (lambda p: None, {"PYTEST_PLUGINS": MARKER}, r"1 PYTEST_\* environment variable\(s\) set"),
    "test files removed": (
        lambda p: delete_modules(p, keep=3),
        {}, rf"3 test modules found under tests, at least {gate.MIN_TEST_MODULES} expected",
    ),
    "a pytest.toml": (
        lambda p: (p / "pytest.toml").write_text(f'[pytest]\naddopts = ["--co", "{MARKER}"]\n', encoding="utf-8"),
        {}, r"pytest\.toml exists \(1 found\)",
    ),
    "a pyproject.toml": (
        lambda p: (p / "pyproject.toml").write_text(f"# {MARKER}\n", encoding="utf-8"),
        {}, r"pyproject\.toml exists \(1 found\)",
    ),
    "every module skips itself": (
        lambda p: [replace_module(p, n, stub_test_module(1, skipped=True)) for n in range(gate.MIN_TEST_MODULES)],
        {}, r"the collect-only run failed \(exit code 5\)",
    ),
    "all but one module skips itself": (
        lambda p: [replace_module(p, n, stub_test_module(1, skipped=True)) for n in range(1, gate.MIN_TEST_MODULES)],
        {}, rf"{math.ceil(gate.MIN_COLLECTED_TESTS / gate.MIN_TEST_MODULES)} tests collected, at least {gate.MIN_COLLECTED_TESTS} expected",
    ),
    "tests that collect too few": (
        lambda p: [replace_module(p, n, stub_test_module(1)) for n in range(gate.MIN_TEST_MODULES)],
        {}, rf"{gate.MIN_TEST_MODULES} tests collected, at least {gate.MIN_COLLECTED_TESTS} expected",
    ),
    "a module that does not import": (
        lambda p: replace_module(p, 0, stub_test_module(1, broken=True)),
        {}, r"the collect-only run failed \(exit code 2\)",
    ),
}


def test_a_project_with_the_expected_files_passes_the_copied_script(tmp_path):
    """The baseline for every failure below: without it, a red run could come from the wrong cause."""
    done = run_gate(make_project(tmp_path))
    expected_tests = gate.MIN_TEST_MODULES * math.ceil(gate.MIN_COLLECTED_TESTS / gate.MIN_TEST_MODULES)
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"test gate: ok ({gate.MIN_TEST_MODULES} test modules, {expected_tests} tests collected)\n"
    assert done.stderr == ""


@pytest.mark.parametrize("label", sorted(FAILURE_MODES))
def test_each_way_to_make_the_test_step_pass_without_the_suite_turns_the_gate_red_without_an_echo(tmp_path, label):
    mutate, extra_env, expected = FAILURE_MODES[label]
    project = make_project(tmp_path)
    mutate(project)
    done = run_gate(project, **extra_env)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert lines and all(line.startswith("test gate: ") for line in lines), done.stderr
    assert any(re.fullmatch(rf"test gate: {expected}", line) for line in lines), done.stderr
    assert MARKER not in done.stdout + done.stderr and "Traceback" not in done.stderr


@pytest.mark.parametrize("stray", gate.STRAY_CONFIG)
def test_each_other_pytest_configuration_file_turns_the_gate_red(tmp_path, stray):
    project = make_project(tmp_path)
    touch(project, stray)
    done = run_gate(project)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr == f"test gate: {stray} exists (1 found)\n"


def test_the_gate_reports_every_static_problem_at_once(tmp_path):
    project = make_project(tmp_path)
    set_ini(project, GOOD_INI.replace("-q", "--co"))
    touch(project, "tests/conftest.py")
    delete_modules(project, keep=1)
    done = run_gate(project, PYTEST_ADDOPTS=MARKER)
    assert done.returncode == 1 and done.stdout == ""
    assert sorted(done.stderr.splitlines()) == sorted(
        [
            "test gate: pytest.ini addopts holds something other than -q",
            "test gate: conftest.py exists (1 found)",
            "test gate: 1 PYTEST_* environment variable(s) set",
            f"test gate: 1 test modules found under tests, at least {gate.MIN_TEST_MODULES} expected",
        ]
    )


def test_the_real_tree_passes_the_gate_in_a_real_process():
    done = subprocess.run(
        [sys.executable, "-m", "scripts.check_test_gate"], cwd=ROOT, env=clean_environment(), capture_output=True, text=True, timeout=600
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert done.stderr == ""
    found = re.fullmatch(r"test gate: ok \((\d+) test modules, (\d+) tests collected\)\n", done.stdout)
    assert found, done.stdout
    modules, collected = int(found.group(1)), int(found.group(2))
    assert modules >= gate.MIN_TEST_MODULES and collected >= gate.MIN_COLLECTED_TESTS
    # A floor far under the suite catches nothing: when the suite has grown well past it, raise it.
    assert modules - gate.MIN_TEST_MODULES <= 25, "MIN_TEST_MODULES in scripts/check_test_gate.py is out of date"
    assert collected <= gate.MIN_COLLECTED_TESTS * 1.5, "MIN_COLLECTED_TESTS in scripts/check_test_gate.py is out of date"
