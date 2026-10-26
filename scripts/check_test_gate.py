"""
The test gate: the workflow runs this BEFORE `python -m pytest`, so that a green test step means the
suite really ran.

    python -m scripts.check_test_gate

`python -m pytest` exits 0 after running nothing when a setting says so: `--collect-only` or
`--ignore` in pytest.ini's addopts, a conftest.py that skips or deselects, a PYTEST_ADDOPTS variable,
test files deleted, a pytest.toml that takes over the configuration. A change like that must stop the
job in plain sight, not turn it quietly green. tests/test_repo_hygiene.py guards the same files from
inside the suite, which a change to the suite can disarm; this check runs from outside it.

Exit code 0 when all of these hold, 1 otherwise (and for a failure of the gate itself):

    * pytest.ini is exactly the expected file: testpaths = tests, pythonpath = ., addopts holding
      nothing but -q. It is read with its own strict grammar (a comment, the [pytest] header, three
      `name = value` lines, nothing else), which accepts only what every ini reader reads the same way.
    * no conftest.py (anywhere outside .git and .venv) and no other pytest configuration file exist.
    * no PYTEST_* variable is set in the environment.
    * at least MIN_TEST_MODULES tests/test_*.py modules exist.
    * a collect-only run, with the project's own configuration and a clean environment, collects at
      least MIN_COLLECTED_TESTS tests.

The log of a public repository is readable by anyone, so what this prints is fixed phrases, counts,
line numbers and the names of a fixed set of files, never a line of any file or a word of pytest's own
output.

The floors sit just under today's suite, so that deleting a whole test module trips them. Raise them
when the suite grows; a drop below them is the thing to look at, and lowering them is a change for a
reviewer, not a way to get a green run.

It is a guard against a mistaken or careless change, not against someone who means to disarm it:
a tree that shadows an interpreter module (a root-level subprocess.py or pytest.py), exits early, or
empties the assertions still passes. That needs a review of what changes the tests, the workflow and
requirements.txt (branch protection and code owners; see docs/DESIGN.md, hardening).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent

MIN_TEST_MODULES = 23  # 24 today: deleting any one module turns the gate red
MIN_COLLECTED_TESTS = 5200  # 5269 today
COLLECT_TIMEOUT_SECONDS = 600

EXPECTED_OPTIONS = {"testpaths": "tests", "pythonpath": "."}
ADDOPTS_ALLOWED = frozenset({"-q"})
# conftest.py is found by name anywhere in the tree. The rest are other places pytest reads its
# configuration from: a pytest.toml or .pytest.toml, even an empty one, takes precedence over
# pytest.ini (which then goes unread), and a second place to configure the gate is a place to hide a change.
CONFTEST = "conftest.py"
STRAY_CONFIG = ("pyproject.toml", "tox.ini", "setup.cfg", "pytest.toml", ".pytest.toml", ".pytest.ini")
SKIPPED_DIRECTORIES = frozenset({".git", ".venv"})  # pytest never descends into either (hidden directories)

_OPTION_LINE = re.compile(r"(testpaths|pythonpath|addopts)[ \t]*=[ \t]*(.*?)[ \t]*")
_COLLECTED_SUMMARY = re.compile(r"^(\d+)(?:/\d+)? tests? collected\b", re.MULTILINE)  # -q:  "5 tests collected in 0.02s"
_COLLECTED_PER_FILE = re.compile(r"^\S[^\n]*\.py: (\d+)$", re.MULTILINE)  # -qq: "tests/test_a.py: 5"
_COLLECTED_NODE_ID = re.compile(r"^\S[^\n]*\.py::", re.MULTILINE)  # -q without the summary


# ---------------------------------------------------------------------------
# pytest.ini
# ---------------------------------------------------------------------------
def ini_problems(path: Path) -> list[str]:
    """What is wrong with pytest.ini, as fixed phrases and line numbers."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ["pytest.ini is missing"]
    except (OSError, UnicodeDecodeError):
        return ["pytest.ini cannot be read"]
    problems: list[str] = []
    options: dict[str, str] = {}
    header_seen = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line or line[0] in "#;":
            continue
        if line == "[pytest]" and not header_seen:
            header_seen = True
            continue
        found = _OPTION_LINE.fullmatch(line)
        if found is None or not header_seen or found.group(1) in options:
            problems.append(f"pytest.ini line {number} is not one the gate accepts")
            continue
        options[found.group(1)] = found.group(2)
    if not header_seen:
        problems.append("pytest.ini has no [pytest] section")
    for name, wanted in EXPECTED_OPTIONS.items():
        if options.get(name) != wanted:
            problems.append(f"pytest.ini does not set {name} to {wanted}")
    try:
        tokens = shlex.split(options.get("addopts", ""))
    except ValueError:
        tokens = ["<unparseable>"]
    if not set(tokens) <= ADDOPTS_ALLOWED:
        problems.append("pytest.ini addopts holds something other than -q")
    return problems


# ---------------------------------------------------------------------------
# The rest of what can change a pytest run
# ---------------------------------------------------------------------------
def stray_config_problems(root: Path) -> list[str]:
    """A conftest.py or a second pytest configuration file, by fixed name and count."""
    found: dict[str, int] = {}
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [d for d in subdirectories if d not in SKIPPED_DIRECTORIES]
        for name in files:
            if name == CONFTEST or name in STRAY_CONFIG:
                found[name] = found.get(name, 0) + 1
    return [f"{name} exists ({found[name]} found)" for name in (CONFTEST, *STRAY_CONFIG) if name in found]


def environment_problems(environ: Mapping[str, str]) -> list[str]:
    """PYTEST_ADDOPTS and friends: counted, never named or quoted."""
    count = sum(1 for name in environ if name.upper().startswith("PYTEST_"))
    return [f"{count} PYTEST_* environment variable(s) set"] if count else []


def count_test_modules(root: Path) -> int:
    tests = root / "tests"
    return sum(1 for path in tests.rglob("test_*.py") if path.is_file()) if tests.is_dir() else 0


def module_problems(root: Path) -> list[str]:
    found = count_test_modules(root)
    return [f"{found} test modules found under tests, at least {MIN_TEST_MODULES} expected"] if found < MIN_TEST_MODULES else []


def static_problems(root: Path, environ: Mapping[str, str]) -> list[str]:
    """Everything that can be decided by looking, before pytest runs at all."""
    return ini_problems(root / "pytest.ini") + stray_config_problems(root) + environment_problems(environ) + module_problems(root)


# ---------------------------------------------------------------------------
# The collect-only run
# ---------------------------------------------------------------------------
def parse_collected(output: str) -> Optional[int]:
    """The number of tests in `pytest --collect-only -q` output, whatever the verbosity added, or None."""
    summary = _COLLECTED_SUMMARY.search(output)
    if summary:
        return int(summary.group(1))
    per_file = _COLLECTED_PER_FILE.findall(output)
    if per_file:
        return sum(int(count) for count in per_file)
    node_ids = len(_COLLECTED_NODE_ID.findall(output))
    return node_ids or None


def collect_problems(root: Path, environ: Mapping[str, str]) -> tuple[list[str], Optional[int]]:
    """Run `python -m pytest --collect-only -q` in `root` and hold the count against the floor."""
    env = {name: value for name, value in environ.items() if not name.upper().startswith("PYTEST")}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=COLLECT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return [f"the collect-only run did not finish within {COLLECT_TIMEOUT_SECONDS} seconds"], None
    if done.returncode != 0:
        return [f"the collect-only run failed (exit code {done.returncode})"], None
    collected = parse_collected(done.stdout)
    if collected is None:
        return ["the collect-only run printed no test count the gate can read"], None
    if collected < MIN_COLLECTED_TESTS:
        return [f"{collected} tests collected, at least {MIN_COLLECTED_TESTS} expected"], collected
    return [], collected


def check(root: Path, environ: Mapping[str, str]) -> tuple[list[str], Optional[int]]:
    """The problems found and, when pytest was run, how many tests it collected."""
    problems = static_problems(root, environ)
    if problems:
        return problems, None  # no point running a suite that is already known not to be the suite
    return collect_problems(root, environ)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Exit code 0 when the gate holds, 1 when it does not or when the gate itself fails."""
    if argv is None:
        argv = sys.argv[1:]
    if list(argv):
        print("test gate: takes no arguments", file=sys.stderr)
        return 1
    try:
        problems, collected = check(ROOT, os.environ)
    except Exception as exc:  # noqa: BLE001 - the class name is all a public log may carry of an unplanned error
        print(f"test gate: the gate itself failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"test gate: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"test gate: ok ({count_test_modules(ROOT)} test modules, {collected} tests collected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
