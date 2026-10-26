"""
Guard against the previous project leaking into this one (docs/DESIGN.md section 11).

This project was modelled on a scraper for a different market. Place names,
currency words, a regulator's name or a currency symbol surviving in a comment, a
fixture or a default would quietly corrupt output, so every text file in the
repository (tracked, or untracked and not ignored) outside docs/ is scanned.

docs/ is exempt because the design documents have to name what was left behind.
This file is exempt for the same reason: it has to contain the forbidden words.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

# Matched case-insensitively, anywhere in the text. Generic markers only (places, the old regulator,
# the old currency): this file is public, so it names no person and no other company, even to forbid them.
FORBIDDEN_SUBSTRINGS = (
    "bangalore",
    "bengaluru",
    "karnataka",
    "k-rera",
    "krera",
    "lakh",
    "crore",
    "rupee",
    "kolkata",
    "calcutta",
    # the rupee sign without the literal character: json.dumps writes a backslash-u escape,
    # HTML a character reference (decimal or hex, any case). Built from parts so this list
    # does not itself spell them out in one piece.
    chr(92) + "u20b9",
    chr(92) + "u000020b9",
    "&#8377",
    "&#x20b9",
    "₹",  # the rupee sign
)
# Matched case-sensitively as a whole word: the old project's time-zone label.
# (A substring match would flag REGISTRY, LIST, EXISTS ...)
FORBIDDEN_WORDS = (re.compile(r"\bIST\b"), re.compile(r"\bINR\b"))

EXEMPT_TOP_LEVEL = {"docs", ".git", ".venv", "venv", "env", "node_modules", ".pytest_cache", "__pycache__"}
MAX_BYTES = 5 * 1024 * 1024


def find_leaks(text: str) -> list[str]:
    """Every forbidden token present in `text` (substrings reported lower-case)."""
    lowered = text.lower()
    found = [token for token in FORBIDDEN_SUBSTRINGS if token in lowered]
    found += [pattern.pattern for pattern in FORBIDDEN_WORDS if pattern.search(text)]
    return found


def _candidate_paths() -> list[str]:
    """Repository files as POSIX paths: tracked plus untracked-but-not-ignored."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            timeout=60,
            check=True,
        )
        listed = [p for p in result.stdout.decode("utf-8", errors="replace").split("\0") if p]
        if listed:
            return sorted(set(listed))
    except (OSError, subprocess.SubprocessError):
        pass
    # No usable git: walk the tree, skipping the usual non-source directories.
    found: list[str] = []
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXEMPT_TOP_LEVEL]
        for name in files:
            found.append(Path(current, name).relative_to(ROOT).as_posix())
    return sorted(found)


def scannable_files() -> list[Path]:
    files = []
    for relative in _candidate_paths():
        parts = relative.split("/")
        if parts[0] in EXEMPT_TOP_LEVEL:
            continue
        path = (ROOT / relative).resolve()
        if path == THIS_FILE or not path.is_file():
            continue
        files.append(path)
    return files


def read_text_or_none(path: Path) -> str | None:
    """The file's text, or None for binary, oversized or non-UTF-8 files."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# The matcher itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Bangalore", "BENGALURU", "Karnataka RERA", "K-RERA", "krera_registry",
        "1.5 lakhs", "10 Crores", "price ₹ 5", "13:00 IST", "(IST)",
    ],
)
def test_the_matcher_catches_each_forbidden_token(text):
    assert find_leaks(text), text


def test_this_public_file_names_no_person_and_no_other_company():
    # A handle or a company name does not belong in a public repository, not even in a list of things to
    # forbid. The names are spelled in pieces so that this test does not contain them either; the message
    # does not repeat the one it found.
    text = THIS_FILE.read_text(encoding="utf-8").lower()
    for piece in (("kvn", "ath"), ("prop", "soch"), ("99", "acres")):
        assert "".join(piece) not in text, "a person's handle or another company's name is in this public file"


BACKSLASH = chr(92)


# The same tokens in forms that do not contain the literal text: json.dumps writes the
# rupee sign as a backslash-u escape by default, HTML pages use a character reference,
# and the currency is also spelled out or abbreviated.
@pytest.mark.parametrize(
    "text",
    [
        BACKSLASH + "u20b9 5", BACKSLASH + "U000020B9", "&#8377; 5", "&#x20B9;", "&#X20b9;",
        "INR 5", "Indian rupees", "one Rupee", "Asia/Kolkata", "Asia/Calcutta",
    ],
)
def test_the_matcher_catches_escaped_and_spelled_out_forms(text):
    assert find_leaks(text), text


def test_the_matcher_catches_a_rupee_sign_written_by_json_dumps():
    import json

    written = json.dumps({"price": chr(0x20B9) + " 5"})  # ensure_ascii is the default: backslash-u form
    assert chr(0x20B9) not in written
    assert find_leaks(written)


@pytest.mark.parametrize(
    "text",
    [
        "REGISTRY", "LIST OF PROJECTS", "EXISTS", "ASSISTANT", "Dubai", "Sharjah", "AED 1.2M",
        "Asia/Dubai", "GST", "ist",  # lower-case 'ist' is not the time-zone label
        "A cluster of listings",
    ],
)
def test_the_matcher_ignores_ordinary_text(text):
    assert find_leaks(text) == [], text


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def test_the_scan_sees_the_project():
    names = {p.relative_to(ROOT).as_posix() for p in scannable_files()}
    assert "src/config.py" in names
    assert "src/models.py" in names
    assert ".env.example" in names
    assert not any(n.startswith(("docs/", ".venv/", ".git/")) for n in names)


def test_no_legacy_references_outside_docs():
    offenders = {}
    for path in scannable_files():
        text = read_text_or_none(path)
        if text is None:
            continue
        leaks = find_leaks(text)
        if leaks:
            offenders[path.relative_to(ROOT).as_posix()] = leaks
    assert not offenders, "legacy references found: " + "; ".join(
        f"{name}: {', '.join(tokens)}" for name, tokens in sorted(offenders.items())
    )


def test_no_legacy_references_in_file_names():
    offenders = {}
    for path in scannable_files():
        relative = path.relative_to(ROOT).as_posix()
        leaks = find_leaks(relative)
        if leaks:
            offenders[relative] = leaks
    assert not offenders, f"legacy references in file names: {offenders}"
