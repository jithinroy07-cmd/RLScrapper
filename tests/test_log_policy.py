"""
Tests for src/log_policy.py, the parts a whole run does not exercise.

The handler, the filters and the secret list are covered through main() in tests/test_main.py and
through the exporter in tests/test_export_map_pins.py. Here:

    the hook          what an exception nobody catches prints (class and place, never a message),
                      that it never raises, that installing it touches nothing else
    when it is set    never by an import; both entry points set it, in a real process
    the key reader    a deeply nested value that is no key file stops nothing

Every message below carries MARKER, so a leak is attributable. The real-process tests start a
fresh interpreter (-I, the repository on sys.path) because an uncaught exception is the one thing
pytest catches for itself.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src import log_policy
from src.config import Config

ROOT = Path(__file__).resolve().parent.parent
# Text a chained exception could carry (a keyed URL, a project name). No secret shape on purpose:
# only "never print the message" keeps it out of a public CI log.
MARKER = "Chained-Message-Marker-55102 Palm Residences Tower"
DEEP_JSON_SECRET = '{"private_key": ' + "[" * 100_000  # deeper than json.loads will recurse


@pytest.fixture(autouse=True)
def _keep_the_interpreters_hooks():
    """install_excepthook() changes the process; give the next test the hooks it came with."""
    saved = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    yield
    sys.excepthook, threading.excepthook, sys.unraisablehook = saved


def explode_with_a_cause():
    try:
        raise ValueError(f"inner {MARKER}")
    except ValueError as err:
        raise KeyboardInterrupt(f"outer {MARKER}") from err


def explode_inside_a_handler():
    try:
        raise ValueError(f"inner {MARKER}")
    except ValueError:
        raise KeyError(f"outer {MARKER}")  # noqa: B904 - the implicit __context__ chain is the case under test


def run_hook_on(raiser) -> None:
    """Give the hook the exception `raiser` raises, with the traceback the interpreter would hand it."""
    try:
        raiser()
    except BaseException:  # noqa: BLE001 - the hook is given KeyboardInterrupt too
        log_policy.excepthook(*sys.exc_info())


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raiser", "name", "function"),
    [
        (explode_with_a_cause, "KeyboardInterrupt", "explode_with_a_cause"),
        (explode_inside_a_handler, "KeyError", "explode_inside_a_handler"),
    ],
)
def test_the_hook_prints_the_class_and_the_place_and_nothing_else(capsys, raiser, name, function):
    run_hook_on(raiser)
    captured = capsys.readouterr()
    assert re.fullmatch(rf"{name} \(at test_log_policy\.py:\d+ in {function}\)\n", captured.err), captured.err
    assert captured.out == ""
    assert MARKER not in captured.err and "Palm Residences" not in captured.err and "ValueError" not in captured.err


def test_the_hook_names_the_file_without_its_directory(capsys):
    run_hook_on(explode_with_a_cause)
    err = capsys.readouterr().err
    assert str(ROOT) not in err and "/" not in err and "\\" not in err


def test_the_hook_without_a_traceback_still_names_the_class(capsys):
    log_policy.excepthook(ValueError, None, None)
    assert capsys.readouterr().err == "ValueError (at unknown)\n"


def test_the_hook_given_something_that_is_not_a_traceback_still_names_the_class(capsys):
    log_policy.excepthook(ValueError, ValueError(MARKER), "not a traceback")  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert err == "ValueError (at unknown)\n" and MARKER not in err


def test_the_hook_given_something_that_is_not_a_class_falls_back_to_the_base_class(capsys):
    log_policy.excepthook(object(), None, None)  # type: ignore[arg-type]
    assert capsys.readouterr().err == "BaseException (at unknown)\n"


class BrokenStream:
    def write(self, text):
        raise OSError(f"cannot write {MARKER}")

    def flush(self):
        raise OSError("cannot flush")


@pytest.mark.parametrize("stream", [BrokenStream(), None], ids=["failing", "missing"])
def test_the_hook_never_raises_whatever_stderr_is(monkeypatch, stream):
    monkeypatch.setattr(sys, "stderr", stream)  # a closed stderr, or none at all (pythonw)
    try:
        explode_with_a_cause()
    except KeyboardInterrupt:
        assert log_policy.excepthook(*sys.exc_info()) is None


def test_error_location_is_the_innermost_frame_and_unknown_without_a_traceback():
    try:
        explode_with_a_cause()
    except KeyboardInterrupt as caught:
        assert re.fullmatch(r"test_log_policy\.py:\d+ in explode_with_a_cause", log_policy.error_location(caught))
    assert log_policy.error_location(ValueError(MARKER)) == "unknown"
    assert log_policy.traceback_location(None) == "unknown"


# ---------------------------------------------------------------------------
# Installing it
# ---------------------------------------------------------------------------
def test_installing_the_hook_replaces_sys_excepthook_and_nothing_else(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(sys, "excepthook", sentinel)
    thread_hook, unraisable_hook = threading.excepthook, sys.unraisablehook
    log_policy.install_excepthook()
    assert sys.excepthook is log_policy.excepthook
    log_policy.install_excepthook()  # twice is the same as once
    assert sys.excepthook is log_policy.excepthook
    assert threading.excepthook is thread_hook and sys.unraisablehook is unraisable_hook


def run_python(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT), *args], capture_output=True, text=True, timeout=120
    )


def test_importing_the_modules_installs_nothing():
    code = (
        "import sys, threading; sys.path.insert(0, sys.argv[1])\n"
        "import src.log_policy, src.main, scripts.export_map_pins\n"
        "print(sys.excepthook is sys.__excepthook__)\n"
        "src.log_policy.install_excepthook()\n"
        "print(sys.excepthook is src.log_policy.excepthook)\n"
    )
    done = run_python(code)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["True", "True"]  # untouched after the imports, replaced only by the call
    assert done.stderr == ""


# ---------------------------------------------------------------------------
# A real process: a cancelled run
# ---------------------------------------------------------------------------
# `cancelled` stands in for the first thing main() does that can be interrupted. Run through
# runpy as __main__, the entry point is the real one, `if __name__ == "__main__"` included.
CANCELLED = (
    "import runpy, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import src.config\n"
    "def cancelled(*args, **kwargs):\n"
    "    try:\n"
    f"        raise ValueError('inner {MARKER}')\n"
    "    except ValueError as err:\n"
    f"        raise KeyboardInterrupt('outer {MARKER}') from err\n"
    "src.config.load_config = cancelled\n"
)
ENTRY_POINT = "module = sys.argv[2]\nsys.argv = [module]\nrunpy.run_module(module, run_name='__main__')\n"


def test_a_process_without_the_hook_prints_the_whole_chain():
    """The control: the default hook prints the message, so the next tests prove something."""
    done = run_python(CANCELLED + "cancelled()\n")
    assert done.returncode != 0
    assert "inner" in done.stderr and MARKER in done.stderr and "ValueError" in done.stderr


@pytest.mark.parametrize("module", ["src.main", "scripts.export_map_pins"])
def test_a_cancelled_run_of_either_entry_point_prints_the_class_and_the_place_only(module):
    done = run_python(CANCELLED + ENTRY_POINT, module)
    assert done.returncode != 0
    assert re.fullmatch(r"KeyboardInterrupt \(at <string>:\d+ in cancelled\)\n", done.stderr), done.stderr
    assert MARKER not in done.stdout + done.stderr and done.stdout == ""


# ---------------------------------------------------------------------------
# The key reader
# ---------------------------------------------------------------------------
def test_a_deeply_nested_json_secret_is_no_key_file_and_stops_nothing():
    """REGRESSION: json.loads raises RecursionError on it, which is not a ValueError and ended the run."""
    assert log_policy.json_secret_members(DEEP_JSON_SECRET) == []
    assert log_policy.secret_values(Config(gcp_service_account_key=DEEP_JSON_SECRET)) == [DEEP_JSON_SECRET]
