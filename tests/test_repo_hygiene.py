"""
Repository hygiene: nothing secret, personal or real may be pushed to the public repo.

Every file that git would commit (tracked, or untracked and not ignored) is read and
checked for: private-key blocks, service-account key files, Google/GitHub/cloud
tokens, URLs with credentials or key= / token= values, absolute local paths,
real-looking e-mail addresses, and the shapes this project's own data and credentials
take (a Google document URL or a SPREADSHEET_ID value with a real id, a Bearer token,
a 40+ character hex string, a public IPv4 address, a +971 phone number, and a property
portal URL outside docs/). Files over 1 MB, files that are not UTF-8 text, a
byte-order mark, and carriage returns in shell/YAML files fail too. Which data files may
exist at all is an allowlist (src/data, and a data-file suffix anywhere else), and the
test gate is guarded: pytest.ini must hold exactly its three known options, and no
conftest.py or other pytest configuration file may exist, so nothing can make
`python -m pytest` pass without running the suite. The ignore rules, the line-ending
rules, .env.example, the requirement floors and the licence notes are checked as well.

The scan works without git installed: it then walks the tree and applies
.gitignore itself (and, when git is present, a test proves the two lists agree).

Fake values below are built at run time (concatenated or repeated), so this file
never contains a string that a secret scanner could mistake for a real credential.
This file is exempt from the content scan because it has to contain the patterns.
"""

from __future__ import annotations

import ast
import configparser
import ipaddress
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

MAX_FILE_BYTES = 1024 * 1024
# Directories that are never repository content, in the directory walk used when git is unavailable.
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}
# The only non-text files the repository may carry.
BINARY_EXTENSIONS = {".png", ".ico", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2"}
# Files that must keep LF line endings in the working tree too (shell and YAML break on CR).
LF_ONLY_SUFFIXES = (".sh", ".yml", ".yaml")


# ---------------------------------------------------------------------------
# Which files would be committed
# ---------------------------------------------------------------------------
def _translate_glob(pattern: str) -> str:
    """One .gitignore glob as a regex over a POSIX path (no `*` across a slash)."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", i + 2)
            if end == -1:
                out.append(re.escape(char))
            else:
                out.append(pattern[i : end + 1].replace("[!", "[^"))
                i = end
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def parse_gitignore(text: str) -> list[tuple[bool, bool, bool, re.Pattern[str]]]:
    """(negated, directory_only, anchored, regex) for each rule, in file order."""
    rules = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line = line.rstrip() if not line.endswith("\\ ") else line
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        directory_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        if not line:
            continue
        rules.append((negated, directory_only, anchored, re.compile(_translate_glob(line) + r"\Z")))
    return rules


def _rule_matches(rule: tuple[bool, bool, bool, re.Pattern[str]], path: str, is_dir: bool) -> bool:
    _, directory_only, anchored, regex = rule
    if directory_only and not is_dir:
        return False
    if anchored:
        return regex.match(path) is not None
    return regex.match(path.rsplit("/", 1)[-1]) is not None


def is_ignored(path: str, rules: list[tuple[bool, bool, bool, re.Pattern[str]]]) -> bool:
    """git's rule: ignored when the path, or any parent directory of it, is ignored (last match wins)."""
    parts = path.split("/")
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        is_dir = depth < len(parts)
        verdict = False
        for rule in rules:
            if _rule_matches(rule, prefix, is_dir):
                verdict = not rule[0]
        if verdict:
            return True
    return False


def walk_candidates(root: Path = ROOT) -> list[str]:
    """Files git would commit, found without git: a directory walk filtered by .gitignore."""
    gitignore = root / ".gitignore"
    rules = parse_gitignore(gitignore.read_text(encoding="utf-8")) if gitignore.is_file() else []
    found = []
    for current, dirs, files in os.walk(root):
        relative_dir = Path(current).relative_to(root).as_posix()
        prefix = "" if relative_dir == "." else relative_dir + "/"
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not is_ignored(prefix + d, rules))
        for name in files:
            relative = prefix + name
            if not is_ignored(relative, rules):
                found.append(relative)
    return sorted(found)


def git_candidates(root: Path = ROOT) -> list[str] | None:
    """`git ls-files --cached --others --exclude-standard`, or None when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    listed = sorted({p for p in result.stdout.decode("utf-8", errors="replace").split("\0") if p})
    return listed or None


def candidate_paths() -> list[str]:
    listed = git_candidates()
    return listed if listed is not None else walk_candidates()


def candidate_files() -> list[Path]:
    files = []
    for relative in candidate_paths():
        path = ROOT / relative
        if path.is_file() and path.resolve() != THIS_FILE:
            files.append(path)
    return files


# ---------------------------------------------------------------------------
# What must not appear
# ---------------------------------------------------------------------------
# A PEM private-key header followed by something shaped like a real key body: a base64
# first line of 60+ characters. Test doubles are tolerated only when the body says
# "fake" or is so repetitive that no real key could look like it.
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----(?:\\r|\\n|\s)*([A-Za-z0-9+/=]{60,})")
_AIZA = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_GITHUB = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
_OTHER_TOKENS = (
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}")),
    ("google oauth access token", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}")),
    ("json web token", re.compile(r"\beyJ[0-9A-Za-z_\-]{10,}\.eyJ[0-9A-Za-z_\-]{10,}\.")),
    ("api secret key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_\-]{32,}")),
)
_SA_TYPE = re.compile(r"""["']type["']\s*:\s*["']service_account["']""")
_SA_KEY_ID = re.compile(r"""["']private_key_id["']\s*:\s*["'][0-9a-f]{40}["']""")
_SA_CLIENT_ID = re.compile(r"""["']client_id["']\s*:\s*["'](\d{18,})["']""")
_URL_CREDENTIALS = re.compile(r"""(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]*://[^\s/:@'"<>]+:[^\s/@'"<>]+@([^\s/:'"<>?#]+)""", re.I)
_QUERY_SECRET = re.compile(
    r"""[?&](?:api[_-]?key|key|token|access[_-]?token|secret|client[_-]?secret|sig|signature|password)=([^&\s"'#)<>]{20,})""",
    re.I,
)
_LOCAL_PATH = re.compile(r"""(?:\b[A-Za-z]:(?:\\\\?|/)Users(?:\\\\?|/)|(?<![\w.])/Users/\w|(?<![\w./])/home/(?!runner\b)\w)""")
# The look-behinds make a match start only at the start of a run, so a long unbroken token is scanned once.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,})")

# Domains that can never be a real mailbox or host (RFC 2606 / 6761), plus the fake
# service-account project the tests use.
_FAKE_DOMAIN = re.compile(
    r"(?:(?:^|\.)example\.(?:com|org|net|test)|\.(?:invalid|test|example|localhost)|^(?:invalid|test|example|localhost)"
    r"|(?:^|\.)fake[a-z0-9\-]*\.iam\.gserviceaccount\.com)\Z",
    re.I,
)
_PLACEHOLDER = re.compile(r"fake|redact|placeholder|dummy|example|xxxx|\*\*\*|\{|\$|<|%", re.I)
# Exact addresses that are stand-ins, not mailboxes (a fixture that scrubs an address out of a URL fragment).
_ALLOWED_EMAILS = frozenset({"a@b.com"})

# The shapes this project's own credentials and data take. Every quantifier is bounded, or runs over a
# character class that the next token excludes, so a long unbroken run is scanned once, not once per character.
# A Google document, sheet or Drive URL: up to four short path segments (spreadsheets/u/0/d/), then the id.
_GOOGLE_DOC_URL = re.compile(r"\b(?:docs|drive)\.google\.com/(?:[A-Za-z0-9_\-]{1,20}/){1,4}?([A-Za-z0-9_\-]{25,})(?![A-Za-z0-9_\-])", re.I)
# SPREADSHEET_ID=<id>, SPREADSHEET_ID: <id> or "SPREADSHEET_ID": "<id>". An angle-bracket or ${{ ... }} placeholder and an empty value do not match.
_SPREADSHEET_ID_VALUE = re.compile(r"""\bSPREADSHEET_ID["']?\s*[=:]\s*["']?([A-Za-z0-9_\-]{30,})(?![A-Za-z0-9_\-])""")
_BEARER = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=\-]{20,})", re.I)
_HEX_STRING = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{40,}(?![0-9A-Za-z])")
_IPV4 = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\w)(?!\.[0-9])")
# +971 or 00971, then at least seven more digits, with the usual spaces, dashes, dots and brackets between them.
_UAE_PHONE = re.compile(r"(?<![\w+])(?:\+\s?|00)971[\s\-.()]*[0-9](?:[\s\-.()]*[0-9]){6,}")
# A portal host in a URL (or a bare domain name); the research note under docs/ may name them, nothing else may.
_PORTAL = re.compile(r"""//[^\s/"'<>]*(?:propertyfinder|bayut|dubizzle)|\b(?:propertyfinder|bayut|dubizzle)\.(?:com|ae|net|org|co)\b""", re.I)


def _looks_fake(value: str) -> bool:
    """A placeholder word in it, or so repetitive that no generated secret could look like it."""
    return bool(_PLACEHOLDER.search(value)) or len(set(value)) < 8


def _real_tokens(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _looks_fake(m.group(0)) for m in pattern.finditer(text))


def _real_ids(pattern: re.Pattern[str], text: str) -> bool:
    """Like _real_tokens, but judges only the captured id, not the URL or name around it."""
    return any(not _looks_fake(m.group(1)) for m in pattern.finditer(text))


def _has_public_ipv4(text: str) -> bool:
    """A dotted quad that is globally routable: not private, loopback, link-local, documentation or reserved."""
    for match in _IPV4.finditer(text):
        try:
            if ipaddress.IPv4Address(match.group(0)).is_global:
                return True
        except ValueError:
            continue  # an octet over 255 or a leading zero: a version or a number, not an address
    return False


def find_problems(relative: str, text: str) -> list[str]:
    """Every hygiene problem in one file's text, as short human-readable strings."""
    problems: list[str] = []
    for match in _PEM.finditer(text):
        body = match.group(1)
        if "fake" not in body.lower() and len(set(body)) >= 20:
            problems.append("private key block with a real-looking body")
    if _real_tokens(_AIZA, text):
        problems.append("Google API key (AIza...)")
    if _real_tokens(_GITHUB, text):
        problems.append("GitHub token")
    for label, pattern in _OTHER_TOKENS:
        if _real_tokens(pattern, text):
            problems.append(label)
    if _SA_TYPE.search(text) and (
        _SA_KEY_ID.search(text) or any(len(set(m.group(1))) >= 5 for m in _SA_CLIENT_ID.finditer(text))
    ):
        problems.append("service-account key file shape")
    for match in _URL_CREDENTIALS.finditer(text):
        if not _FAKE_DOMAIN.search(match.group(1)):
            problems.append(f"URL with credentials (host {match.group(1)})")
    for match in _QUERY_SECRET.finditer(text):
        if not _looks_fake(match.group(1)):
            problems.append("URL with a key=/token= value")
    if _LOCAL_PATH.search(text):
        problems.append("absolute local path (C:\\Users, /Users or /home)")
    for match in _EMAIL.finditer(text):
        if match.group(0).lower() not in _ALLOWED_EMAILS and not _FAKE_DOMAIN.search(match.group(1)):
            problems.append(f"real-looking e-mail address (@{match.group(1)})")
    # The messages below say what was found, never the value: a failure is printed in a public log.
    if _real_ids(_GOOGLE_DOC_URL, text):
        problems.append("Google document, sheet or Drive URL with a real-looking id")
    if _real_ids(_SPREADSHEET_ID_VALUE, text):
        problems.append("SPREADSHEET_ID assigned a real-looking id")
    if _real_ids(_BEARER, text):
        problems.append("Bearer token")
    if _real_tokens(_HEX_STRING, text):
        problems.append("hex string of 40 or more characters (a key, token or hash)")
    if _has_public_ipv4(text):
        problems.append("public IPv4 address")
    if _UAE_PHONE.search(text):
        problems.append("UAE phone number (+971 or 00971)")
    if not relative.startswith("docs/") and _PORTAL.search(text):
        problems.append("property portal host (only docs/ may name one)")
    if relative.endswith(LF_ONLY_SUFFIXES) and "\r" in text:
        problems.append("carriage return in a shell/YAML file")
    return sorted(set(problems))


# ---------------------------------------------------------------------------
# The detectors themselves: each must catch its target and leave fakes alone
# ---------------------------------------------------------------------------
def _b64_line(n: int = 64) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return "".join(alphabet[(i * 7 + 3) % len(alphabet)] for i in range(n))


def _id_chars(n: int) -> str:
    """An id-shaped run (letters and digits, no punctuation) that no placeholder rule would pass."""
    return _b64_line(n).replace("+", "a").replace("/", "b")


# Shapes of this project's own data and credentials: label -> (the problem it must raise, a sample).
# The samples are assembled from fragments so that this file never holds a live-looking value.
SHAPE_SAMPLES = {
    "google sheet url": ("Google document", "https://docs.google" + ".com/spreadsheets/d/" + _id_chars(44) + "/edit#gid=0"),
    "google sheet url with account segment": (
        "Google document",
        "see https://docs.google" + ".com/spreadsheets/u/0/d/" + _id_chars(44) + "/edit",
    ),
    "google published sheet url": ("Google document", "https://docs.google" + ".com/spreadsheets/d/e/" + _id_chars(60) + "/pubhtml"),
    "google doc url": ("Google document", "https://docs.google" + ".com/document/d/" + _id_chars(44) + "/edit"),
    "google drive folder url": ("Google document", "https://drive.google" + ".com/drive/folders/" + _id_chars(33)),
    "spreadsheet id assignment": ("SPREADSHEET_ID", "SPREADSHEET" + "_ID=" + _id_chars(44)),
    "spreadsheet id quoted": ("SPREADSHEET_ID", "SPREADSHEET" + "_ID = '" + _id_chars(44) + "'"),
    "spreadsheet id yaml": ("SPREADSHEET_ID", "SPREADSHEET" + "_ID: " + _id_chars(44)),
    "spreadsheet id json": ("SPREADSHEET_ID", '{"SPREADSHEET' + '_ID": "' + _id_chars(44) + '"}'),
    "bearer header": ("Bearer token", "Authorization: Bear" + "er " + _id_chars(40)),
    "bearer lower case": ("Bearer token", "bear" + "er " + _id_chars(24)),
    "bearer with base64 padding": ("Bearer token", "Bear" + "er " + _id_chars(30) + "=="),
    "hex 40": ("hex string", "sha = '" + "0123456789abcdef" * 2 + "01234567" + "'"),
    "hex 64 upper case": ("hex string", ("0123456789ABCDEF" * 4)),
    "hex in a line of prose": ("hex string", "the key is " + "9f86d081884c7d65" + "9a2feaa0c55ad015" + "a3bf4f1b" + ", keep it safe"),
    "ipv4": ("public IPv4", "host = '" + "8.8" + ".4.4'"),
    "ipv4 in a url": ("public IPv4", "http://" + "34.120" + ".5.9:8080/api"),
    "ipv4 at the end of a sentence": ("public IPv4", "It listens on " + "1.1" + ".1.1."),
    "phone with plus": ("UAE phone", "call +971" + " 50 123 4567 after nine"),
    "phone without separators": ("UAE phone", "tel:+971" + "501234567"),
    "phone with dashes": ("UAE phone", "(+971" + ")-4-123-4567"),
    "phone with 00": ("UAE phone", "whatsapp 00971" + " 55 123 4567"),
    "phone with a space after the plus": ("UAE phone", "+ 971" + " 4 123 4567"),
    "portal url": ("property portal", "https://www.property" + "finder.ae/en/search"),
    "portal bayut": ("property portal", "https://www.ba" + "yut.com/to-buy/property/dubai/"),
    "portal dubizzle": ("property portal", "https://dubai.du" + "bizzle.com/property-for-sale/"),
    "portal protocol-relative": ("property portal", "src = '//www.ba" + "yut.com/x.js'"),
    "portal bare domain": ("property portal", "scraped from www.property" + "finder.ae daily"),
    "portal host without a known suffix": ("property portal", "http://api.du" + "bizzle.internal/v1"),
}

BAD_SAMPLES = {
    "pem": "-----BEGIN " + "PRIVATE KEY-----\n" + _b64_line() + "\n-----END " + "PRIVATE KEY-----\n",
    "pem rsa": "-----BEGIN RSA " + "PRIVATE KEY-----\\n" + _b64_line() + "\\n",
    "pem openssh": "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n" + _b64_line(70) + "\n",
    "aiza": "key = '" + "AI" + "za" + _b64_line(35).replace("+", "a").replace("/", "b") + "'",
    "github classic": "token: gh" + "p_" + _b64_line(36).replace("+", "a").replace("/", "b"),
    "github fine grained": "github" + "_pat_" + _b64_line(40).replace("+", "a").replace("/", "b"),
    "aws": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "slack": "xo" + "xb-" + "1234567890-abcdefghij",
    "oauth access token": "ya" + "29." + _b64_line(30).replace("+", "a").replace("/", "b"),
    "sa file": '{"type": "service_account", "private_key_id": "' + "a1b2c3d4e5" * 4 + '"}',
    "url credentials": "https://admin:s3cretpass@db.internal.corp/path",
    "query key": "https://maps.test-provider.net/geocode?q=x&key=" + _b64_line(24).replace("+", "a").replace("/", "b"),
    "query token": "https://host.net/x?access_token=" + _b64_line(20).replace("+", "a").replace("/", "b"),
    "windows path": "path = 'C:" + "\\Users\\someone\\project'",
    "windows path escaped": "path = 'C:" + "\\\\Users\\\\someone'",
    "windows path forward": "C:" + "/Users/someone/project",
    "mac path": "see /Users" + "/someone/project/file.py",
    "linux home": "cd /home" + "/someone/project",
    "email": "contact someone.real" + "@" + "gmail.com for access",
    "email custom domain": "owner" + "@" + "company-name.ae",
    "crlf yaml": "name: x\r\non: push\r\n",
}


@pytest.mark.parametrize("label", sorted(BAD_SAMPLES))
def test_each_detector_catches_its_target(label):
    relative = "x.yml" if label == "crlf yaml" else "x.txt"
    assert find_problems(relative, BAD_SAMPLES[label]), label


GOOD_SAMPLES = [
    # the regexes the project's own scrubbers use are not keys
    'PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)',
    # test doubles: header plus an obviously fake or repetitive body
    "-----BEGIN " + "PRIVATE KEY-----\\nFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0001\\n",
    "-----BEGIN " + "PRIVATE KEY-----\n" + "A" * 64 + "\n",
    "-----BEGIN " + "PRIVATE KEY-----\nfake-material\n-----END " + "PRIVATE KEY-----",
    # reserved and fake domains
    "ops@example.com ops@example.org a@b.invalid a@host.test a@host.example a@mail.example.com",
    "fake-sa@fake-project.iam.gserviceaccount.com",
    "https://user:pw@example.com/path",
    "https://svc:fake-pass-123@example.com",
    "https://hunter2:pw@host.example/x",
    # placeholders for a key value
    "https://host.net/x?key={API_KEY}",
    "https://host.net/x?key=fake-key-12345",
    "https://host.net/x?token=REDACTED12",
    "https://host.net/x?key=$GEOAPIFY_KEY",
    # the CI runner's home and ordinary prose
    "/home/runner/work/repo/repo",
    "relative/path/Users/ok.txt and the word Users",
    "A decorator @pytest.fixture and a handle @name without a domain",
    "name: x\non: push\n",
    # Google documents named by a placeholder, by a short name, or with no id at all
    "https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit",
    "https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid=0",
    "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit",
    "https://docs.google.com/spreadsheets/d/fake-spreadsheet-id-abcdefghij0123456789/edit",
    "https://docs.google.com/spreadsheets/d/" + "a" * 44 + "/edit",
    "https://docs.google.com/spreadsheets/",
    "https://drive.google.com/drive/folders/<FOLDER_ID>",
    "https://drive.google.com/drive/my-drive",
    "https://support.google.com/docs/answer/" + "1" * 30,
    "see docs.google.com for the sheet",
    # SPREADSHEET_ID with no value, a placeholder, a short value, or the workflow's secret reference
    "SPREADSHEET_ID=",
    "SPREADSHEET_ID=\n",
    "SPREADSHEET_ID=<your-sheet-id>",
    "SPREADSHEET_ID: ${{ secrets.SPREADSHEET_ID }}",
    "SPREADSHEET_ID=short-test-id",
    "SPREADSHEET_ID=fake-spreadsheet-id-abcdefghijklmnopqrst0123456789",
    "SPREADSHEET_ID=" + "1" * 44,
    "monkeypatch.setenv('SPREADSHEET_ID', 'x')",
    "the spreadsheet_id = extract_the_spreadsheet_identifier_from_url(url) call",
    # Bearer with a placeholder, a short word or a template
    "Authorization: Bearer {token}",
    "Authorization: Bearer <token>",
    "Authorization: Bearer $TOKEN",
    "send it as a Bearer token over TLS",
    "headers = {'Authorization': f'Bearer {self.token}'}",
    "Bearer fake-token-abcdefghijklmnop1234",
    "Bearer " + "a" * 40,
    "Bearer short",
    # hex strings: too short, glued to other letters, or repetitive
    "0123456789abcdef" * 2 + "0123456",
    "0123456789abcdef" * 2 + "01234567" + "z",
    "x" + "0123456789abcdef" * 2 + "01234567",
    "feedface" * 6,
    "ab" * 32,
    "a" * 40,
    # IP addresses: documentation ranges, loopback, unspecified, private, link-local, versions and numbers
    "192.0.2.1 198.51.100.7 203.0.113.9",
    "127.0.0.1 0.0.0.0 255.255.255.255",
    "10.0.0.1 172.16.0.1 192.168.1.1 169.254.1.1",
    "pytest 9.0.3 and python 3.12.4",
    "version 1.2.3.4.5 and 999.1.1.1 and 1.2.3.256 and 1.2.3",
    "v8.8.8.8 and x8.8.8.8 and 8.8.8.8x and 18.8.8.8.8",
    "bucket 2026.10.25.1",
    # phones: too few digits, no +971 or 00971, someone else's country code, a digit run that contains 971
    "+971 4",
    "+971 50 123",
    "971 50 123 4567",
    "+44 20 7946 0958",
    "id 1000971123456789 and a+971",
    "order +9715",
    # portals named in prose, or a different host
    "the listing portals forbid scraping, so none is read",
    "Property Finder, Bayut and Dubizzle are named in docs only",
    "https://www.example.com/propertyfinders-guide",
    "https://data.dubai.gov.ae/api/projects",
]


@pytest.mark.parametrize("text", GOOD_SAMPLES)
def test_the_detectors_leave_fake_and_reserved_values_alone(text):
    assert find_problems("x.yml", text) == [], text


@pytest.mark.parametrize("label", sorted(SHAPE_SAMPLES))
def test_each_shape_detector_names_what_it_found(label):
    expected, sample = SHAPE_SAMPLES[label]
    problems = find_problems("src/x.py", sample)
    assert any(expected in problem for problem in problems), (label, problems)


@pytest.mark.parametrize("label", sorted(SHAPE_SAMPLES))
def test_a_problem_message_never_repeats_the_value_it_matched(label):
    # A failure is printed in a public CI log, so the message may say what was found but not what it was.
    _, sample = SHAPE_SAMPLES[label]
    secret_part = max(re.findall(r"[A-Za-z0-9_\-]{12,}", sample), key=len, default="")
    problems = " ".join(find_problems("src/x.py", sample))
    assert not secret_part or secret_part not in problems


def test_the_shape_samples_do_not_depend_on_one_another():
    # Each sample trips only its own detector, so disabling one detector cannot be hidden by another.
    for label, (expected, sample) in SHAPE_SAMPLES.items():
        others = [p for p in find_problems("src/x.py", sample) if expected not in p]
        assert not others, (label, others)
    assert {expected for expected, _ in SHAPE_SAMPLES.values()} == {
        "Google document",
        "SPREADSHEET_ID",
        "Bearer token",
        "hex string",
        "public IPv4",
        "UAE phone",
        "property portal",
    }


def test_only_the_docs_folder_may_name_a_property_portal():
    sample = "https://www.ba" + "yut.com/to-buy/"
    assert find_problems("src/sources/x.py", sample)
    assert find_problems("README.md", sample)
    assert find_problems("tests/test_x.py", sample)
    assert find_problems("scripts/x.py", sample)
    assert find_problems("docs", sample)  # a FILE called docs is not the folder
    assert find_problems("mydocs/x.md", sample)
    assert not find_problems("docs/research.md", sample)
    assert not find_problems("docs/sub/notes.md", sample)
    # the exemption is for portal names only: a credential in docs/ is still a credential
    assert find_problems("docs/research.md", "Authorization: Bear" + "er " + _id_chars(40))
    assert find_problems("docs/research.md", "call +971" + " 50 123 4567")


def test_a_public_address_is_flagged_but_a_documentation_or_private_one_is_not():
    for public in ("8.8" + ".8.8", "1.1" + ".1.1", "34.120" + ".5.9", "151.101" + ".1.69"):
        assert find_problems("x.txt", f"server {public}"), public
    for private in ("192.0.2.44", "198.51.100.200", "203.0.113.7", "127.0.0.1", "0.0.0.0", "10.1.2.3", "172.31.0.9", "192.168.0.1"):
        assert not find_problems("x.txt", f"server {private}"), private


# A long unbroken token (a minified bundle, a base64 blob) must not make the scan crawl:
# a pattern that restarts at every character of a long run is quadratic, and a 1 MB file
# would take hours. Each case is 60 000 characters; a linear scan takes milliseconds.
@pytest.mark.parametrize(
    "text",
    [
        "x" * 60000,
        "a." * 30000,
        "a+" * 30000,
        "https://" + "a" * 60000,
        "a@" + "b." * 30000,
        "-" * 60000,
        # the shape detectors: a long run of what each one scans for, and many short repeats of its opening
        "docs.google.com/spreadsheets/" * 2000,
        "docs.google.com/" + "a/" * 30000,
        "docs.google.com/spreadsheets/d/" + "a" * 60000,
        "SPREADSHEET_ID=" + "a" * 60000,
        "SPREADSHEET_ID" + " " * 60000,
        "SPREADSHEET_ID=" * 4000,
        "Bearer " + "a" * 60000,
        "Bearer" + " " * 60000,
        "Bearer " * 8000,
        "a" * 60000 + "g",
        "0" * 60000,
        "1." * 30000,
        "1.2.3." * 10000,
        "255." * 15000,
        "+971" + " " * 60000,
        "+971 " + "1 " * 30000,
        "+971" * 15000,
        "00971" * 12000,
        "//" + "a" * 60000,
        "//a" * 20000,
        "bayut" * 12000,
    ],
    ids=[
        "letters", "dotted", "plus", "scheme", "domain", "dashes",
        "gdocs-prefix", "gdocs-slashes", "gdocs-id", "sheet-id", "sheet-spaces", "sheet-repeat",
        "bearer-token", "bearer-spaces", "bearer-repeat", "hex-then-g", "zeros", "dotted-ones",
        "dotted-octets", "dotted-255", "phone-spaces", "phone-digits", "phone-repeat", "phone-00",
        "portal-slashes", "portal-repeat", "portal-word",
    ],
)
def test_the_detectors_are_linear_on_a_long_unbroken_token(text):
    import time

    started = time.perf_counter()
    find_problems("x.txt", text)
    assert time.perf_counter() - started < 2.0


def test_a_url_credential_on_a_real_host_is_flagged_but_the_same_on_a_fake_host_is_not():
    assert find_problems("x.txt", "https://u:p@" + "api.provider.net/x")
    assert not find_problems("x.txt", "https://u:p@api.provider.test/x")


# ---------------------------------------------------------------------------
# The scan of the repository
# ---------------------------------------------------------------------------
def test_the_scan_sees_the_project():
    names = set(candidate_paths())
    assert {"src/config.py", ".env.example", ".gitignore", ".gitattributes", "requirements.txt"} <= names
    assert not any(n.startswith((".git/", ".venv/")) for n in names)


def test_no_file_is_over_one_megabyte():
    big = {f.relative_to(ROOT).as_posix(): f.stat().st_size for f in candidate_files() if f.stat().st_size > MAX_FILE_BYTES}
    assert not big, f"files over 1 MB: {big}"


def test_every_file_is_utf8_text_without_a_byte_order_mark():
    bad = []
    for path in candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        if path.suffix.lower() in BINARY_EXTENSIONS:
            continue
        if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
            bad.append(f"{relative}: byte-order mark")
            continue
        if b"\0" in data:
            bad.append(f"{relative}: binary content")
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{relative}: not UTF-8")
    assert not bad, "; ".join(bad)


# Characters a reader cannot see: bidi controls (they can make code or a name display as something else),
# zero-width spaces and joiners, the word joiner and invisible operators, a soft hyphen and a stray BOM.
# Tests that need one write it as an escape (‮), so the file shows what it holds.
_INVISIBLE = re.compile("[­؜​-‏‪-‮⁠-⁩﻿]")


def test_no_file_holds_a_literal_invisible_or_bidi_character():
    offenders = {}
    for path in candidate_files():
        if path.suffix.lower() in BINARY_EXTENSIONS:
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # reported by the UTF-8 test
        lines = [number for number, line in enumerate(text.splitlines(), start=1) if _INVISIBLE.search(line)]
        if lines:
            offenders[path.relative_to(ROOT).as_posix()] = lines
    assert not offenders, "literal invisible characters (write them as \\uXXXX escapes): " + "; ".join(
        f"{name} line(s) {lines[:5]}" for name, lines in sorted(offenders.items())
    )


def test_the_invisible_character_detector_catches_each_kind():
    for char in ("‮", "⁦", "​", "‍", "‏", "⁠", "﻿", "­", "؜"):
        assert _INVISIBLE.search(f"a{char}b"), hex(ord(char))
    assert not _INVISIBLE.search("plain ASCII, Arabic مشروع and an emoji \U0001F3D9")


def test_no_secret_personal_or_local_data_in_any_candidate_file():
    offenders = {}
    for path in candidate_files():
        if path.suffix.lower() in BINARY_EXTENSIONS:
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # reported by the UTF-8 test
        relative = path.relative_to(ROOT).as_posix()
        problems = find_problems(relative, text)
        if problems:
            offenders[relative] = problems
    assert not offenders, "; ".join(f"{name}: {', '.join(p)}" for name, p in sorted(offenders.items()))


# ---------------------------------------------------------------------------
# Which data files may exist
# ---------------------------------------------------------------------------
# The data licence is not settled, so registry data must never reach the public repository. src/data holds
# only the files below, and a data-file suffix is allowed nowhere else except the test fixtures.
KNOWN_DATA_FILES = frozenset(
    {
        "src/data/area_aliases.json",
        "src/data/emirate_polygons.geojson",
        "src/data/seed_projects.csv",
        "src/data/seed_registry.csv",
    }
)
DATA_SUFFIXES = frozenset({".csv", ".tsv", ".xls", ".xlsx", ".parquet", ".db", ".sqlite", ".sqlite3"})


def data_file_problems(paths: list[str]) -> list[str]:
    """Files that carry data but are not on the allowlist (paths are POSIX, relative to the repository root)."""
    problems = []
    for relative in paths:
        if relative.startswith("src/data/"):
            if relative not in KNOWN_DATA_FILES:
                problems.append(f"{relative}: src/data holds only the known seed, alias and boundary files")
        elif not relative.startswith("tests/fixtures/"):
            suffixes = {s.lower() for s in PurePosixPath(relative).suffixes}
            if suffixes & DATA_SUFFIXES:
                problems.append(f"{relative}: a data file outside tests/fixtures and src/data")
    return problems


def test_only_the_known_data_files_are_in_the_repository():
    assert data_file_problems(candidate_paths()) == []


def test_every_known_data_file_is_still_there():
    # An allowlist entry for a file that is gone would let a different file of that name in unnoticed.
    names = set(candidate_paths())
    assert KNOWN_DATA_FILES <= names, sorted(KNOWN_DATA_FILES - names)


@pytest.mark.parametrize(
    "path",
    [
        "src/data/dld_projects.json",
        "src/data/geocode_cache.json",
        "src/data/snapshot.csv",
        "src/data/sub/seed_projects.csv",
        "src/data/seed_projects.csv.bak",
        "src/data/SEED_PROJECTS.CSV",
        "src/data/README.md",
        "data/export.csv",
        "mysrc/data/x.csv",
        "scraped.xlsx",
        "EXPORT.CSV",
        "docs/table.csv",
        "docs/table.tsv",
        "scripts/out.parquet",
        "scripts/legacy.xls",
        "x.db",
        "backup/a.sqlite",
        "src/sources/cache.sqlite3",
        "tests/data.csv",
        "tests/fixtures_extra/x.csv",
        "site/pins.csv",
        "export.csv.gz",
    ],
)
def test_a_data_file_off_the_allowlist_is_reported(path):
    assert data_file_problems([path]), path


@pytest.mark.parametrize(
    "path",
    sorted(KNOWN_DATA_FILES)
    + [
        "tests/fixtures/sources/dld_page1.json",
        "tests/fixtures/rows.csv",
        "tests/fixtures/sources/rows.xlsx",
        "src/x.py",
        "src/data_loader.py",
        "src/database.py",
        "site/index.html",
        "README.md",
        "docs/research-2026-10-25.md",
        "scripts/csv_tools.py",
        "requirements.txt",
    ],
)
def test_the_known_data_files_and_ordinary_source_files_are_not_reported(path):
    assert data_file_problems([path]) == [], path


# ---------------------------------------------------------------------------
# The test gate: nothing may make `python -m pytest` pass without running the suite
# ---------------------------------------------------------------------------
# The workflow's test step is `python -m pytest`, before any credential is used. pytest takes its settings
# from pytest.ini, and a conftest.py can skip, deselect or rewrite any test, so the gate holds only while
# pytest.ini is exactly what it is today and no other file can change what is collected or how it runs.
PYTEST_INI_VALUES = {"testpaths": "tests", "pythonpath": "."}
PYTEST_ADDOPTS_ALLOWED = frozenset({"-q"})
# Other files pytest reads its configuration from. In pytest 9 a pytest.toml or .pytest.toml (even an empty one)
# takes precedence over pytest.ini, which then goes unread; the rest are ignored while pytest.ini exists, but a
# second place to configure the gate is a place to hide a change.
STRAY_PYTEST_CONFIG = frozenset({"pyproject.toml", "tox.ini", "setup.cfg", "pytest.toml", ".pytest.toml", ".pytest.ini"})


def pytest_gate_problems(root: Path, paths: list[str]) -> list[str]:
    """What could make the test step pass without running the whole suite: stray config files and a changed pytest.ini."""
    problems = []
    for relative in paths:
        name = PurePosixPath(relative).name
        if name == "conftest.py":
            problems.append(f"{relative}: a conftest.py can skip, deselect or rewrite tests")
        elif name in STRAY_PYTEST_CONFIG:
            problems.append(f"{relative}: a second place to configure pytest")
    ini = root / "pytest.ini"
    if not ini.is_file():
        return problems + ["pytest.ini is missing"]
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # pytest reads option names as written, so ADDOPTS is not addopts
    try:
        parser.read_string(ini.read_text(encoding="utf-8"))
    except (configparser.Error, UnicodeDecodeError) as error:
        return problems + [f"pytest.ini cannot be read ({type(error).__name__})"]
    if parser.defaults():
        problems.append("pytest.ini has a [DEFAULT] section")
    if parser.sections() != ["pytest"]:
        problems.append(f"pytest.ini sections are {parser.sections()}, not exactly ['pytest']")
    options = dict(parser.items("pytest")) if parser.has_section("pytest") else {}
    for name in sorted(set(options) - set(PYTEST_INI_VALUES) - {"addopts"}):
        problems.append(f"pytest.ini option {name!r} is not one of testpaths, pythonpath and addopts")
    for name, wanted in PYTEST_INI_VALUES.items():
        if options.get(name, "").strip() != wanted:
            problems.append(f"pytest.ini {name} must be {wanted!r}")
    try:
        tokens = shlex.split(options.get("addopts", ""))
    except ValueError:
        tokens = ["<unparseable>"]
    extra = sorted(set(tokens) - PYTEST_ADDOPTS_ALLOWED)
    if extra:
        problems.append(f"pytest.ini addopts may hold only {sorted(PYTEST_ADDOPTS_ALLOWED)}, not {extra}")
    return problems


def test_the_ci_test_gate_cannot_be_neutered():
    assert pytest_gate_problems(ROOT, candidate_paths()) == []


GOOD_PYTEST_INI = "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -q\n"


def _gate_problems_in(tmp_path: Path, ini: str | None = GOOD_PYTEST_INI, files: tuple[str, ...] = ()) -> list[str]:
    """The gate check on a throwaway project holding this pytest.ini (or none) and these empty files."""
    if ini is not None:
        (tmp_path / "pytest.ini").write_text(ini, encoding="utf-8")
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return pytest_gate_problems(tmp_path, walk_candidates(tmp_path))


def test_the_gate_check_accepts_the_expected_pytest_ini(tmp_path):
    assert _gate_problems_in(tmp_path) == []


def test_the_gate_check_accepts_a_pytest_ini_without_addopts_and_with_comments(tmp_path):
    assert _gate_problems_in(tmp_path, "# the suite\n[pytest]\ntestpaths = tests\npythonpath = .\n") == []
    assert _gate_problems_in(tmp_path, "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -q -q\n") == []


BAD_PYTEST_INIS = {
    "collect only": GOOD_PYTEST_INI.replace("-q", "-q --collect-only"),
    "collect only, short": GOOD_PYTEST_INI.replace("-q", "--co"),
    "keyword filter": GOOD_PYTEST_INI.replace("-q", "-q -k nothing_matches_this"),
    "marker filter": GOOD_PYTEST_INI.replace("-q", "-q -m nothing"),
    "deselect": GOOD_PYTEST_INI.replace("-q", "-q --deselect tests/test_main.py"),
    "ignore": GOOD_PYTEST_INI.replace("-q", "-q --ignore=tests"),
    "plugin switch": GOOD_PYTEST_INI.replace("-q", "-q -p no:cacheprovider"),
    "stop at the first failure": GOOD_PYTEST_INI.replace("-q", "-q -x"),
    "doctest only": GOOD_PYTEST_INI.replace("-q", "-q --doctest-modules"),
    "quieter": GOOD_PYTEST_INI.replace("-q", "-qq"),
    "unbalanced quote": GOOD_PYTEST_INI.replace("-q", "-q 'oops"),
    "testpaths one file": GOOD_PYTEST_INI.replace("testpaths = tests", "testpaths = tests/test_main.py"),
    "testpaths elsewhere": GOOD_PYTEST_INI.replace("testpaths = tests", "testpaths = docs"),
    "testpaths widened": GOOD_PYTEST_INI.replace("testpaths = tests", "testpaths = tests docs"),
    "testpaths missing": GOOD_PYTEST_INI.replace("testpaths = tests\n", ""),
    "pythonpath moved": GOOD_PYTEST_INI.replace("pythonpath = .", "pythonpath = src"),
    "pythonpath missing": GOOD_PYTEST_INI.replace("pythonpath = .\n", ""),
    "norecursedirs": GOOD_PYTEST_INI + "norecursedirs = tests\n",
    "python_files": GOOD_PYTEST_INI + "python_files = nothing_*.py\n",
    "python_functions": GOOD_PYTEST_INI + "python_functions = nothing_*\n",
    "confcutdir": GOOD_PYTEST_INI + "confcutdir = tests\n",
    "filterwarnings": GOOD_PYTEST_INI + "filterwarnings = ignore\n",
    "capitalised option": GOOD_PYTEST_INI + "ADDOPTS = --co\n",
    # pytest reads option names as written, so these are not the options the gate needs
    "upper-case option names": "[pytest]\nTESTPATHS = tests\nPYTHONPATH = .\nADDOPTS = -q\n",
    "duplicate option": GOOD_PYTEST_INI + "addopts = --co\n",
    "second section": GOOD_PYTEST_INI + "[tool:pytest]\naddopts = --co\n",
    "default section": "[DEFAULT]\naddopts = --co\n" + GOOD_PYTEST_INI,
    "wrong section name": GOOD_PYTEST_INI.replace("[pytest]", "[tool:pytest]"),
    "no section": "testpaths = tests\npythonpath = .\n",
    "empty file": "",
    "unparseable": "[pytest\ntestpaths = tests\n",
    "byte-order mark": "﻿" + GOOD_PYTEST_INI,
}


@pytest.mark.parametrize("label", sorted(BAD_PYTEST_INIS))
def test_a_changed_pytest_ini_is_reported(tmp_path, label):
    assert _gate_problems_in(tmp_path, BAD_PYTEST_INIS[label]), label


def test_a_missing_pytest_ini_is_reported(tmp_path):
    assert _gate_problems_in(tmp_path, None) == ["pytest.ini is missing"]


@pytest.mark.parametrize(
    "stray",
    [
        "conftest.py",
        "tests/conftest.py",
        "src/conftest.py",
        "tests/sub/dir/conftest.py",
        "scripts/conftest.py",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
        "pytest.toml",
        ".pytest.toml",
        ".pytest.ini",
        "docs/tox.ini",
        "scripts/pyproject.toml",
    ],
)
def test_a_stray_conftest_or_second_pytest_config_is_reported(tmp_path, stray):
    problems = _gate_problems_in(tmp_path, files=(stray,))
    assert len(problems) == 1 and problems[0].startswith(stray + ":"), problems


@pytest.mark.parametrize(
    "harmless",
    [
        "tests/test_conftest.py",
        "my_conftest.py",
        "tests/conftest_helpers.py",
        "tests/conftest.py.txt",
        "docs/pytest.ini.md",
        # a virtual environment and caches are not repository content (and are ignored by git)
        ".venv/lib/python3.12/site-packages/pkg/conftest.py",
        "venv/lib/site-packages/pkg/tox.ini",
        "node_modules/pkg/setup.cfg",
        ".pytest_cache/conftest.py",
    ],
)
def test_files_that_only_look_like_pytest_config_are_not_reported(tmp_path, harmless):
    assert _gate_problems_in(tmp_path, files=(harmless,)) == []


# ---------------------------------------------------------------------------
# The ignore rules
# ---------------------------------------------------------------------------
GITIGNORE = ROOT / ".gitignore"

MUST_BE_IGNORED = [
    ".env", ".env.local", ".env.prod", "sub/.env", "sub/dir/.env.staging", "prod.env",
    "service_account.json", "service_account_key.json", "sub/service_account.json", "my_service_account.json",
    "service-account.json", "gcp-service-account-prod.json", "x.json.json", "a/b.json.json",
    "credentials.json", "sub/credentials.json", "credentials-prod.json", "client_secret_123.json",
    "token.json", "sub/token.json", "sa.json",
    "key.pem", "sub/key.pem", "k.p12", "k.pfx", "k.key", "id_rsa", ".secrets/anything.txt",
    "backups/x.json", "sub/backups/x.json",
    "site/pins.geojson", "site/pins.geojson.abc123.tmp", "scraped_projects_latest.json",
    "scraped_projects_latest.json.q1w2e3.tmp", "src/data/geocode_cache.json", "src/data/geocode_cache.json.1234.tmp",
    "src/data/emirate_polygons.geojson.tmp", "run.log",
    "src/__pycache__/x.cpython-312.pyc", "pkg/__pycache__/y.pyc", ".venv/lib/x.py", "venv/x", ".pytest_cache/v/x",
    ".coverage", "htmlcov/index.html", ".mypy_cache/x", ".ruff_cache/x",
    ".vscode/settings.json", ".idea/workspace.xml", "x.swp", ".DS_Store", "sub/.DS_Store", "Thumbs.db", "Desktop.ini",
    "dist/pkg.whl", "build/x", "pkg.egg-info/PKG-INFO",
]
MUST_NOT_BE_IGNORED = [
    ".env.example", "src/config.py", "src/data/emirate_polygons.geojson", "src/data/area_aliases.json",
    "src/data/seed_projects.csv", "tests/fixtures/sources/dld_page1.json", "site/index.html", "docs/DESIGN.md",
    "requirements.txt", ".gitattributes", ".gitignore", ".github/workflows/daily_uae_real_estate.yml",
    "README.md", "scripts/export_map_pins.py",
]


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_gitignore_rules_ignore_secrets_and_generated_files(path):
    rules = parse_gitignore(GITIGNORE.read_text(encoding="utf-8"))
    assert is_ignored(path, rules), f"{path} would be committed"


@pytest.mark.parametrize("path", MUST_NOT_BE_IGNORED)
def test_gitignore_rules_keep_real_source_files(path):
    rules = parse_gitignore(GITIGNORE.read_text(encoding="utf-8"))
    assert not is_ignored(path, rules), f"{path} would be ignored"


def _git_available() -> bool:
    return git_candidates() is not None


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_the_ignore_matcher_agrees_with_git_on_every_sample_path():
    samples = MUST_BE_IGNORED + MUST_NOT_BE_IGNORED
    rules = parse_gitignore(GITIGNORE.read_text(encoding="utf-8"))
    for path in samples:
        result = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT, capture_output=True)
        git_says = result.returncode == 0
        assert is_ignored(path, rules) == git_says, f"{path}: matcher differs from git (git ignored={git_says})"


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_the_directory_walk_lists_the_same_files_as_git():
    listed = git_candidates()
    walked = walk_candidates()
    # A file that exists in the index but was deleted from disk is listed by git only.
    listed_on_disk = [p for p in listed if (ROOT / p).is_file()]
    assert walked == sorted(listed_on_disk)


def test_the_walk_fallback_skips_ignored_and_tool_directories(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\n.env.*\n!.env.example\nbackups/\n*.tmp\n", encoding="utf-8")
    for name in (".env", ".env.local", ".env.example", "keep.py", "backups/b.json", "a/out.tmp", ".git/config", ".venv/x.py"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    assert walk_candidates(tmp_path) == [".env.example", ".gitignore", "keep.py"]


# ---------------------------------------------------------------------------
# Line endings
# ---------------------------------------------------------------------------
def test_gitattributes_makes_text_files_lf_and_binary_files_binary():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert "* text=auto eol=lf" in lines
    for extension in ("png", "ico", "jpg", "woff2"):
        assert any(ln.startswith(f"*.{extension}") and "binary" in ln for ln in lines), extension
    for extension in ("geojson", "json", "csv", "py", "yml", "sh"):
        assert any(ln.startswith(f"*.{extension} ") and "text" in ln and "eol=lf" in ln for ln in lines), extension


def test_gitattributes_is_committed_text_with_lf_endings():
    assert b"\r" not in (ROOT / ".gitattributes").read_bytes()


# ---------------------------------------------------------------------------
# .env.example: names only
# ---------------------------------------------------------------------------
def test_env_example_assigns_no_value_to_any_secret():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    uncommented = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert uncommented, "the example should list the secret names"
    for line in uncommented:
        name, _, value = line.partition("=")
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name.strip()), line
        assert value.strip() == "", f"{name.strip()} has a value in .env.example"


def test_env_example_commented_defaults_never_hold_a_secret_shaped_value():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        match = re.match(r"#\s*([A-Z][A-Z0-9_]*)=(\S+)", line)
        if match and re.search(r"KEY|SECRET|TOKEN|PASSWORD|ID$|EMAILS$|CONTACT", match.group(1)):
            pytest.fail(f"{match.group(1)} shows a default value in .env.example")


def test_env_example_tells_the_same_story_as_the_workflow_and_the_code():
    # The workflow passes GEOCODE_CONTACT as a SECRET (a variable's value is printed in a public log),
    # so the example lists it among the secrets; and no run ever deletes rows, so ALLOW_ROW_DROP's
    # comment must describe the shrink guard it really controls.
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    secrets_block, _, variables_block = text.partition("# ---- Variables")
    assert variables_block, "the example should have a variables block"
    assert re.search(r"^GEOCODE_CONTACT=\s*$", secrets_block, re.M), "GEOCODE_CONTACT belongs with the secrets"
    assert not re.search(r"^#?\s*GEOCODE_CONTACT=", variables_block, re.M), "GEOCODE_CONTACT must not be listed as a variable"
    comment = text.split("ALLOW_ROW_DROP", 1)[0].rsplit("\n\n", 1)[-1]
    assert "delet" not in comment.lower().replace("nothing is ever deleted", ""), comment
    assert "80 percent" in comment, comment


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------
REQUIREMENTS = ROOT / "requirements.txt"
# import name -> the requirement that provides it
IMPORT_TO_REQUIREMENT = {
    "dotenv": "python-dotenv",
    "yaml": "pyyaml",
    "gspread": "gspread",
    "pydantic": "pydantic",
    "requests": "requests",
    "shapely": "shapely",
    "pytest": "pytest",
    "google": None,  # google.auth (google-auth) and google.genai (google-genai): checked below
    "numpy": None,  # installed by shapely; the tests import it directly
}
# google-auth 2.49.0 and later require cryptography; the floor test below keeps google-auth at or above that.
TRANSITIVE_OK = {"numpy": "shapely", "cryptography": "google-auth"}


def parse_requirements() -> dict[str, str]:
    """name (lower-case) -> minimum version string, for each line of requirements.txt."""
    out = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)\s*>=\s*([0-9][0-9A-Za-z.]*)", line)
        assert match, f"requirement without a >= lower bound: {line!r}"
        out[match.group(1).lower().replace("_", "-")] = match.group(2)
    return out


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def third_party_imports() -> dict[str, set[str]]:
    local = {"src", "scripts", "tests"}
    found: dict[str, set[str]] = {}
    for folder in ("src", "scripts", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if top in sys.stdlib_module_names or top in local:
                        continue
                    found.setdefault(top, set()).add(path.relative_to(ROOT).as_posix())
    return found


def test_every_third_party_import_is_provided_by_a_requirement():
    requirements = parse_requirements()
    imports = third_party_imports()
    # sibling test helper modules sit in tests/ and are imported by bare name
    helpers = {p.stem for p in (ROOT / "tests").glob("*.py")}
    missing = []
    for top, users in sorted(imports.items()):
        if top in helpers:
            continue
        if top == "google":
            needed = ["google-auth", "google-genai"]
        elif top in TRANSITIVE_OK:
            needed = [TRANSITIVE_OK[top]]
        elif top in IMPORT_TO_REQUIREMENT:
            needed = [IMPORT_TO_REQUIREMENT[top]]
        else:
            missing.append(f"{top} (unknown import, used by {sorted(users)[:2]})")
            continue
        for requirement in needed:
            if requirement not in requirements:
                missing.append(f"{top} needs {requirement}")
    assert not missing, "; ".join(missing)


# Floors checked on 25 Oct 2026 with pip-audit and a clean install of the exact floor
# versions on Python 3.12 (the version CI uses).
MIN_FLOORS = {
    "requests": "2.33.0",        # earlier releases have published advisories (PYSEC-2026-1872, -2275)
    "python-dotenv": "1.2.2",    # PYSEC-2026-2270
    "pytest": "9.0.3",           # PYSEC-2026-1845
    "pyyaml": "6.0.1",           # 6.0 has no Python 3.12 wheel and does not build from source
    "google-auth": "2.49.0",     # google-genai 2.3.0 needs >=2.48.1 (never released); 2.49.0 also requires cryptography, which a test imports
}


@pytest.mark.parametrize("name", sorted(MIN_FLOORS))
def test_requirement_floors_are_not_below_the_known_good_versions(name):
    floors = parse_requirements()
    assert name in floors
    assert _version_tuple(floors[name]) >= _version_tuple(MIN_FLOORS[name]), (
        f"{name}>={floors[name]} allows a version below {MIN_FLOORS[name]}"
    )


def test_every_requirement_has_a_lower_bound():
    assert parse_requirements()


# ---------------------------------------------------------------------------
# Licence and attribution notes
# ---------------------------------------------------------------------------
def test_no_licence_file_was_invented_and_the_readme_flags_the_open_choice():
    assert not [p for p in ROOT.iterdir() if p.is_file() and p.name.upper().startswith(("LICENSE", "LICENCE", "COPYING"))], (
        "the owner has not chosen a licence; do not add one"
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Licence", 1)
    assert len(section) == 2, "README needs a Licence section"
    assert re.search(r"(?i)owner", section[1]) and re.search(r"(?i)none has been chosen|not chosen", section[1])


def test_readme_carries_the_openstreetmap_and_dubai_land_department_notices():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ODbL" in readme and "OpenStreetMap contributors" in readme
    assert re.search(r"(?i)Dubai Land Department data licence", readme)


def test_the_boundary_file_carries_its_openstreetmap_attribution():
    import json

    data = json.loads((ROOT / "src" / "data" / "emirate_polygons.geojson").read_text(encoding="utf-8"))
    assert data["features"]
    for feature in data["features"]:
        properties = feature["properties"]
        assert "ODbL" in properties["osm_license"]
        assert "OpenStreetMap contributors" in properties["attribution"]


# ---------------------------------------------------------------------------
# The research note is committed on purpose: no scratch paths, no other project's audit
# ---------------------------------------------------------------------------
def test_the_research_note_has_no_scratch_paths_or_sibling_project_audit():
    text = (ROOT / "docs" / "research-2026-10-25.md").read_text(encoding="utf-8")
    for fragment in ("<scratch>", "scratchpad", "Port audit", "CRON_SECRET", "verify=False", "secrets_and_identity_to_replace"):
        assert fragment not in text, fragment
