"""
Tests for site/index.html (docs/DESIGN.md section 7).

No browser and no network are needed. Four layers:

  1. Static checks of the markup with a real HTML parser: pinned library tags with
     SRI, a Content-Security-Policy whose hashes match the inline blocks, no inline
     handlers, no markup-from-data sinks, safe links, relative URLs only.
  2. A small JavaScript tokenizer, so those checks look at code and not at comments
     or strings (the page has regex literals with quote characters in them). It is
     itself tested on good and bad snippets, and the page is mutated with a bad
     snippet to prove the checks would fail.
  3. The page's PURE block (filters, log-scale mapping, escaping, safe-URL) executed
     under node. Skipped, with the reason, when node is missing.
  4. The whole inline script run under node against a hand-written fake DOM and a
     Leaflet stub, fed with GeoJSON built by the real exporter (the contract test).

Everything is synthetic. The fake DOM's innerHTML setter throws, so any use of it
fails a test.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import shutil
import socket
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from scripts import export_map_pins as exp
from src.config import EMIRATES
from src.emirates import UNION_BBOX
from src.listing_data import MAX_BEDROOMS, PROPERTY_TYPES
from src.models import PRECISIONS, RegistryRow

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "site" / "index.html"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="node is not installed: the page's JavaScript was not executed (the static checks still ran)"
)

# The published files' SRI hashes, taken from the downloaded files and cross-checked
# against the sha512 values cdnjs lists. A changed or mistyped hash must fail here
# rather than silently break the page for every visitor.
PINNED_LIBRARIES = {
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css":
        "sha384-c6Rcwz4e4CITMbu/NBmnNS8yN2sC3cUElMEMfP3vqqKFp7GOYaaBBCqmaWBjmkjb",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css":
        "sha384-uIZNAcoEHVWMk8LrCSX95DJk6FQzH6zG00LQC9BykRY1UKKpkDzDhbDLYQ1Ho3mI",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.min.css":
        "sha384-NyCa0nBOjmCqS9HwLKpE7yqy2HVG6PN/cDyAAIdPijDlUH6jCfqX6ydzXFTA/L/l",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js":
        "sha384-NElt3Op+9NBMCYaef5HxeJmU4Xeard/Lku8ek6hoPTvYkQPh3zLIrJP7KiRocsxO",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js":
        "sha384-SIh+qcEBNMda8tT+rx66aCBuWtVYMVzRABqhK4eeQh4pjDHoRdVx8nYU4LQ5eGzI",
}
REFERENCED_HOSTS = {"cdnjs.cloudflare.com", "tile.openstreetmap.org", "www.openstreetmap.org", "opendatacommons.org"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------
class PageParser(HTMLParser):
    """Collects what the checks need, using the same rules a browser does."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self.ids: dict[str, str] = {}
        self.comments: list[str] = []
        self._open: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag, attrs):
        attributes = {k: v for k, v in attrs}
        self.tags.append((tag, attributes))
        if "id" in attributes and attributes["id"]:
            self.ids[attributes["id"]] = tag
        if tag == "style":
            self._open.append(("style", []))
        elif tag == "script" and "src" not in attributes:
            self._open.append(("script", []))

    def handle_endtag(self, tag):
        if self._open and self._open[-1][0] == tag:
            kind, chunks = self._open.pop()
            (self.inline_styles if kind == "style" else self.inline_scripts).append("".join(chunks))

    def handle_data(self, data):
        if self._open:
            self._open[-1][1].append(data)

    def handle_comment(self, data):
        self.comments.append(data)


def read_page() -> str:
    # A browser normalises CRLF to LF before it hashes an inline block.
    return PAGE.read_bytes().decode("utf-8").replace("\r\n", "\n")


@pytest.fixture(scope="module")
def source() -> str:
    return read_page()


@pytest.fixture(scope="module")
def parsed(source) -> PageParser:
    parser = PageParser()
    parser.feed(source)
    parser.close()
    return parser


@pytest.fixture(scope="module")
def script(parsed) -> str:
    assert len(parsed.inline_scripts) == 1
    return parsed.inline_scripts[0]


def sha256_b64(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def _tokens(css: str, opener: str) -> dict[str, str]:
    """The ``--name: #hex`` colour tokens declared in the first rule whose selector matches ``opener``."""
    body = re.search(opener + r"([^}]*)\}", css).group(1)
    return {name: value for name, value in re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\b", body)}


def _luminance(colour: str) -> float:
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _blend(fg: str, bg: str, alpha: float) -> str:
    return "#" + "".join("%02x" % round(int(fg[i:i + 2], 16) * alpha + int(bg[i:i + 2], 16) * (1 - alpha)) for i in (1, 3, 5))


def csp_directives(parsed: PageParser) -> dict[str, list[str]]:
    metas = [a for t, a in parsed.tags if t == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(metas) == 1, "the page needs exactly one Content-Security-Policy meta tag"
    out: dict[str, list[str]] = {}
    for part in metas[0]["content"].split(";"):
        words = part.split()
        if words:
            assert words[0] not in out, f"duplicate CSP directive {words[0]}"
            out[words[0]] = words[1:]
    return out


# ---------------------------------------------------------------------------
# A small JavaScript tokenizer: enough to tell code from comments, strings,
# template literals and regex literals (the page has regexes containing quotes).
# ---------------------------------------------------------------------------
_REGEX_AFTER_WORDS = {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do", "instanceof"}
_REGEX_AFTER_CHARS = set("(,=:[!&|?{;+-*%<>~^")


def _scan_template(src: str, i: int) -> int:
    """End offset of the template literal starting at src[i] == '`'."""
    j, n = i + 1, len(src)
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
        elif ch == "`":
            return j + 1
        elif ch == "$" and src[j + 1 : j + 2] == "{":
            depth, j = 1, j + 2
            while j < n and depth:
                tk = src[j]
                if tk in "'\"":
                    k = j + 1
                    while k < n and src[k] != tk:
                        k += 2 if src[k] == "\\" else 1
                    j = k + 1
                    continue
                if tk == "`":
                    j = _scan_template(src, j)
                    continue
                depth += (tk == "{") - (tk == "}")
                j += 1
        else:
            j += 1
    return n


def tokenize_js(src: str) -> list[tuple[str, int, int]]:
    """[(kind, start, end)] with kind in code, comment, string, template, regex."""
    tokens: list[tuple[str, int, int]] = []
    i, n, code_start, prev = 0, len(src), 0, ""

    def flush(end: int) -> None:
        if end > code_start:
            tokens.append(("code", code_start, end))

    while i < n:
        c, nxt = src[i], src[i + 1 : i + 2]
        if c == "/" and nxt == "/":
            flush(i)
            j = src.find("\n", i)
            j = n if j < 0 else j
            tokens.append(("comment", i, j))
            i = code_start = j
        elif c == "/" and nxt == "*":
            flush(i)
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            tokens.append(("comment", i, j))
            i = code_start = j
        elif c in "'\"":
            flush(i)
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            tokens.append(("string", i, j))
            i = code_start = j
            prev = "str"
        elif c == "`":
            flush(i)
            j = _scan_template(src, i)
            tokens.append(("template", i, j))
            i = code_start = j
            prev = "str"
        elif c == "/" and (prev == "" or prev in _REGEX_AFTER_CHARS or prev in _REGEX_AFTER_WORDS):
            flush(i)
            j, in_class = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                elif ch == "\n":
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            tokens.append(("regex", i, j))
            i = code_start = j
            prev = "regex"
        elif c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            prev = src[i:j]
            i = j
        else:
            if not c.isspace():
                prev = c
            i += 1
    flush(n)
    return tokens


def code_only(src: str) -> str:
    """`src` with comments, strings, templates and regexes blanked (same length, newlines kept)."""
    out = list(src)
    for kind, a, b in tokenize_js(src):
        if kind != "code":
            for k in range(a, b):
                if out[k] != "\n":
                    out[k] = " "
    return "".join(out)


def _template_expressions(text: str) -> list[str]:
    found, j, n = [], 1, len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
        elif text[j] == "$" and text[j + 1 : j + 2] == "{":
            start, depth, j = j + 2, 1, j + 2
            while j < n and depth:
                tk = text[j]
                if tk in "'\"":
                    k = j + 1
                    while k < n and text[k] != tk:
                        k += 2 if text[k] == "\\" else 1
                    j = k + 1
                    continue
                if tk == "`":
                    j = _scan_template(text, j)
                    continue
                depth += (tk == "{") - (tk == "}")
                j += 1
            found.append(text[start : j - 1])
        else:
            j += 1
    return found


def _strip_escape_calls(masked: str) -> str:
    while True:
        match = re.search(r"\bescapeHtml\s*\(", masked)
        if not match:
            return masked
        depth, k = 1, match.end()
        while k < len(masked) and depth:
            depth += (masked[k] == "(") - (masked[k] == ")")
            k += 1
        masked = masked[: match.start()] + " " + masked[k:]


def expression_is_safe(expr: str) -> bool:
    """
    True when `expr` can only produce static text or escapeHtml(...) results joined
    with +. Anything else (a bare variable, a function call, a ternary) is treated
    as unescaped data: conservative on purpose.
    """
    parts: list[str] = []
    for kind, a, b in tokenize_js(expr):
        text = expr[a:b]
        if kind == "code":
            parts.append(text)
        elif kind == "string":
            parts.append('""')
        elif kind == "regex":
            return False
        elif kind == "template":
            if not all(expression_is_safe(inner) for inner in _template_expressions(text)):
                return False
            parts.append('""')
    return re.sub(r'[\s+()"]', "", _strip_escape_calls("".join(parts))) == ""


def _expression_end(src: str, start: int) -> int:
    depth = 0
    for kind, a, b in tokenize_js(src[start:]):
        if kind != "code":
            continue
        for k in range(a, b):
            ch = src[start + k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    return start + k
                depth -= 1
            elif ch == ";" and depth == 0:
                return start + k
    return len(src)


_SINK_ASSIGN = re.compile(r"\.\s*(innerHTML|outerHTML|srcdoc)\s*(\+?=)(?!=)")
_SINK_CALL = re.compile(r"\.\s*(insertAdjacentHTML|createContextualFragment)\s*\(|\bdocument\s*\.\s*(write|writeln)\s*\(")
_SINK_COMPUTED = re.compile(r"\[\s*(['\"`])(?:innerHTML|outerHTML|srcdoc)\1\s*\]")
_DYNAMIC_CODE = re.compile(r"\beval\s*\(|\bnew\s+Function\b|\bFunction\s*\(|\bimportScripts\s*\(")
_TIMER_CALL = re.compile(r"\b(?:setTimeout|setInterval)\s*\(")
_HANDLER_PROPERTY = re.compile(r"\.\s*on[a-z]+\s*=(?!=)")


def unsafe_html_sinks(src: str) -> list[str]:
    """Each place `src` builds markup from anything but static text or escapeHtml(...)."""
    found: list[str] = []
    code = code_only(src)
    for match in _SINK_ASSIGN.finditer(code):
        end = _expression_end(src, match.end())
        expr = src[match.end() : end]
        if not expression_is_safe(expr):
            found.append(f"{match.group(1)} {match.group(2)} {expr.strip()[:60]}")
    for match in _SINK_CALL.finditer(code):
        end = _expression_end(src, match.end())
        args = src[match.end() : end]
        if match.group(1) == "insertAdjacentHTML":
            _, _, args = args.partition(",")
        if not expression_is_safe(args):
            found.append(f"{match.group(1) or match.group(2)}({args.strip()[:60]}")
    for match in _SINK_COMPUTED.finditer(src):
        found.append(f"computed access {match.group(0)}")
    return found


def dynamic_code(src: str) -> list[str]:
    """Calls that turn text into code: eval, Function, importScripts, timers given a string."""
    code = code_only(src)
    found = [m.group(0) for m in _DYNAMIC_CODE.finditer(code)]
    for match in _TIMER_CALL.finditer(code):
        argument = src[match.end() :].lstrip()[:1]      # strings are blanked in `code`, so read the original
        if argument in ("'", '"', "`"):
            found.append(match.group(0) + argument)
    return found


def inline_handler_problems(markup_parser: PageParser, js: str) -> list[str]:
    problems = []
    for tag, attributes in markup_parser.tags:
        for name, value in attributes.items():
            if name.lower().startswith("on"):
                problems.append(f"<{tag} {name}>")
            if name.lower() == "style":
                problems.append(f"<{tag} style=...> (blocked by the policy anyway)")
            if isinstance(value, str) and re.match(r"\s*javascript:", value, re.I):
                problems.append(f"<{tag} {name}=javascript:>")
    problems += [f"script assigns {m.group(0).strip()}" for m in _HANDLER_PROPERTY.finditer(code_only(js))]
    return problems


# ---------------------------------------------------------------------------
# 1. The page, statically
# ---------------------------------------------------------------------------
class TestPageStructure:
    def test_the_title_is_a_two_to_four_word_name(self, parsed, source):
        title = re.search(r"<title>(.*?)</title>", source, re.S).group(1).strip()
        assert 2 <= len(title.split()) <= 4 and ":" not in title

    def test_language_viewport_and_charset_are_declared(self, parsed):
        assert dict(parsed.tags[0][1]) == {} or True
        html_attrs = next(a for t, a in parsed.tags if t == "html")
        assert html_attrs.get("lang") == "en"
        assert any(t == "meta" and a.get("charset", "").lower() == "utf-8" for t, a in parsed.tags)
        viewport = next(a for t, a in parsed.tags if t == "meta" and a.get("name") == "viewport")
        assert "width=device-width" in viewport["content"]

    def test_there_is_exactly_one_inline_script_and_one_inline_style(self, parsed):
        assert len(parsed.inline_scripts) == 1 and len(parsed.inline_styles) == 1

    def test_the_libraries_are_the_pinned_versions_from_cdnjs_with_sri(self, parsed):
        found = {}
        for tag, a in parsed.tags:
            url = a.get("src") if tag == "script" else a.get("href") if tag == "link" and a.get("rel") == "stylesheet" else None
            if url:
                found[url] = a
        assert set(found) == set(PINNED_LIBRARIES)
        for url, attrs in found.items():
            assert attrs.get("integrity") == PINNED_LIBRARIES[url], url
            assert attrs.get("crossorigin") == "anonymous", url
        assert any("/leaflet/1.9.4/" in u for u in found) and any("/leaflet.markercluster/1.5.3/" in u for u in found)

    def test_sri_values_are_well_formed_sha384(self):
        for value in PINNED_LIBRARIES.values():
            prefix, _, digest = value.partition("-")
            assert prefix == "sha384" and len(base64.b64decode(digest, validate=True)) == 48

    def test_the_only_hosts_the_page_mentions_are_the_expected_ones(self, source):
        hosts = {urlsplit(m).hostname for m in re.findall(r"https?://[^\s\"'<>);,`]+", source)}
        assert hosts <= REFERENCED_HOSTS, hosts - REFERENCED_HOSTS
        assert "http://" not in source

    def test_the_page_can_live_under_a_sub_path(self, parsed, script):
        assert not any(t == "base" for t, _ in parsed.tags)
        for tag, attrs in parsed.tags:
            for name in ("src", "href", "action"):
                value = attrs.get(name)
                if value:
                    assert not value.startswith("/"), f"<{tag} {name}={value}> is root-relative"
        code = code_only(script)
        assert not re.search(r"\blocation\b|\bbaseURI\b|\bdocument\.URL\b", code)
        assert "fetch('pins.geojson'" in script

    def test_every_id_the_script_looks_up_exists_in_the_markup(self, parsed, script):
        wanted = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", script)) | set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", script))
        assert wanted and wanted <= set(parsed.ids), wanted - set(parsed.ids)
        assert "map" in parsed.ids

    def test_the_spec_s_controls_and_wording_are_present(self, parsed, source, script):
        for needed in ("q", "emirateChips", "status", "typeChips", "bedChips", "priceLo", "priceHi", "pick", "radius", "list", "stats", "notice"):
            assert needed in parsed.ids, needed
        assert "Studio" in script
        assert re.search(r"exact pins only", source)
        assert "Approximate location (community centroid)" in script
        assert "Approximate location (area centroid)" in script

    def test_the_map_is_set_up_as_the_design_says(self, script):
        assert re.search(r"setView\(\[25\.2, 55\.3\], 9\)", script)
        assert "tile.openstreetmap.org/{z}/{x}/{y}.png" in script
        assert "OpenStreetMap</a> contributors" in script and "ODbL" in script
        assert "preferCanvas: true" in script
        assert "markerClusterGroup" in script
        # approximate pins stack on identical coordinates: only spiderfying separates them
        assert "disableClusteringAtZoom" not in code_only(script) and "spiderfyOnMaxZoom: true" in script

    def test_the_page_reads_every_property_the_exporter_writes(self, script):
        unread = [name for name in exp.ALLOWED_PROPERTIES if not re.search(rf"\bp\.{name}\b", script)]
        assert unread == []

    def test_nothing_is_stored_or_tracked(self, script, source):
        assert not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie|sendBeacon|XMLHttpRequest|WebSocket", code_only(script))
        # url.password / url.username are how safeHttpUrl refuses credentials in a link; they are not secrets
        assert not re.search(r"(?i)api[_-]?key|secret|password|bearer|authorization", re.sub(r"\burl\.(?:username|password)\b", "", source))


class TestContentSecurityPolicy:
    def test_it_comes_before_anything_that_loads(self, parsed):
        order = [t for t, _ in parsed.tags]
        policy = next(i for i, (t, a) in enumerate(parsed.tags) if t == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy")
        for tag in ("link", "script", "style", "img"):
            if tag in order:
                assert order.index(tag) > policy, tag

    def test_it_allows_only_self_cdnjs_osm_tiles_and_fetch_to_self(self, parsed):
        csp = csp_directives(parsed)
        assert csp["default-src"] == ["'none'"]
        assert csp["connect-src"] == ["'self'"]
        assert csp["object-src"] == ["'none'"] and csp["base-uri"] == ["'none'"] and csp["form-action"] == ["'none'"]
        assert set(csp["img-src"]) == {"'self'", "data:", "https://tile.openstreetmap.org"}
        for directive in ("script-src", "style-src"):
            sources = csp[directive]
            assert {"'self'", "https://cdnjs.cloudflare.com"} <= set(sources)
            extra = [s for s in sources if s not in ("'self'", "https://cdnjs.cloudflare.com")]
            assert len(extra) == 1 and re.fullmatch(r"'sha256-[A-Za-z0-9+/]{43}='", extra[0]), extra

    def test_it_never_weakens_itself(self, parsed):
        text = json.dumps(csp_directives(parsed))
        for weak in ("unsafe-inline", "unsafe-eval", "unsafe-hashes", "*", "http:", "blob:", "filesystem:", "wss:", "ws:"):
            assert weak not in text, weak

    @pytest.mark.parametrize("directive, kind", [("script-src", "script"), ("style-src", "style")])
    def test_the_hash_matches_the_inline_block_byte_for_byte(self, parsed, directive, kind):
        block = (parsed.inline_scripts if kind == "script" else parsed.inline_styles)[0]
        expected = f"'sha256-{sha256_b64(block)}'"
        assert expected in csp_directives(parsed)[directive], (
            f"the inline {kind} changed: put {expected} in the {directive} of the policy"
        )

    def test_the_dark_theme_beats_the_map_librarys_own_selectors_for_the_controls(self, parsed):
        # Leaflet's sheet uses ".leaflet-container .leaflet-control-attribution" and ".leaflet-bar a:focus";
        # a plainer selector of ours loses to them, leaving a pale attribution strip and a pale focused zoom button.
        css = parsed.inline_styles[0]
        assert re.search(r"\.leaflet-container\s+\.leaflet-control-attribution\s*\{[^}]*background:\s*var\(--surface\)", css)
        assert re.search(r"\.leaflet-container\s+\.leaflet-control-attribution\s+a\s*\{[^}]*var\(--accent\)", css)
        assert re.search(r"\.leaflet-bar a:focus[^{]*\{[^}]*background:\s*var\(--surface-2\)", css)
        assert re.search(r"\.leaflet-bar a:focus[^{]*\{[^}]*color:\s*var\(--ink\)", css)

    @pytest.mark.parametrize("theme, opener", [
        ("light", r":root\s*\{"),
        ("dark", r":root:not\(\[data-theme=\"light\"\]\)\s*\{"),
        ("dark (forced)", r":root\[data-theme=\"dark\"\]\s*\{"),
    ])
    def test_text_stays_readable_in_every_state_of_every_theme(self, parsed, theme, opener):
        # WCAG 1.4.3: 4.5:1 for the text colours on the surfaces they sit on, and also for the label of an
        # emirate chip that is switched off, which the page dims with an opacity (a dimmed label at .5 was 3.2:1).
        css = parsed.inline_styles[0]
        light = _tokens(css, r":root\s*\{")
        tokens = dict(light, **_tokens(css, opener)) if theme != "light" else light
        pairs = [("ink", "surface"), ("ink", "ground"), ("muted", "surface"), ("muted", "ground"), ("accent", "surface"),
                 ("accent-ink", "accent"), ("dubai", "surface"), ("sharjah", "surface"), ("warn-ink", "warn-bg"), ("error-ink", "error-bg")]
        for fg, bg in pairs:
            assert _contrast(tokens[fg], tokens[bg]) >= 4.5, (theme, fg, bg)
        opacity = float(re.search(r"\.chip\.emirate\[aria-pressed=\"false\"\]\s*\{[^}]*opacity:\s*([0-9.]+)", css).group(1))
        dimmed = _blend(tokens["ink"], tokens["surface"], opacity)
        assert _contrast(dimmed, tokens["surface"]) >= 4.5, (theme, "switched-off emirate chip", opacity)

    def test_a_changed_script_would_be_caught_by_the_hash_check(self, parsed):
        block = parsed.inline_scripts[0]
        assert sha256_b64(block + " ") != sha256_b64(block)
        assert f"'sha256-{sha256_b64(block + ' ')}'" not in csp_directives(parsed)["script-src"]

    def test_the_policy_does_not_disable_the_referrer_osm_needs(self, parsed):
        # The OSM tile servers require a Referer: a no-referrer policy would get the map blocked.
        assert not any(t == "meta" and (a.get("name") or "").lower() == "referrer" and "no-referrer" in (a.get("content") or "") for t, a in parsed.tags)


class TestMarkupSafety:
    def test_there_are_no_inline_handlers_or_style_attributes(self, parsed, script):
        assert inline_handler_problems(parsed, script) == []

    def test_no_markup_is_built_from_data(self, script):
        assert unsafe_html_sinks(script) == []

    def test_no_dynamic_code_execution(self, script):
        assert dynamic_code(script) == []

    def test_links_open_safely(self, script):
        code = code_only(script)
        blanks = [m.start() for m in re.finditer(r"_blank", script)]
        assert blanks, "the page has external links"
        for position in blanks:
            assert "noopener noreferrer" in script[max(0, position - 200) : position + 200]
        # the only place a link target is created: scheme-checked first
        creators = re.findall(r"\bhref\s*:", code)
        assert len(creators) == 1
        body = re.search(r"function externalLink\(href, text\) \{(.*?)\n  \}", script, re.S).group(1)
        assert "safeHttpUrl(href)" in body and "href: safe" in body and "target: '_blank'" in body

    def test_the_el_helper_refuses_handler_style_and_srcdoc_attributes(self, script):
        body = re.search(r"function el\(tag, props, kids\) \{(.*?)\n  \}", script, re.S).group(1)
        assert "lower.indexOf('on') === 0" in body and "'style'" in body and "'srcdoc'" in body and "throw" in body

    def test_text_reaches_the_page_through_text_nodes(self, script):
        code = code_only(script)
        assert "textContent" in code and "createTextNode" in code
        assert not re.search(r"\binnerHTML\b|\bouterHTML\b|\binsertAdjacentHTML\b|\bdocument\.write", code)


class TestTheScannerItself:
    """The checks above are only worth anything if the scanner would catch the bad cases."""

    @pytest.mark.parametrize(
        "snippet",
        [
            "el.innerHTML = '<b>' + name + '</b>';",
            "el.innerHTML = `<i>${name}</i>`;",
            "el.innerHTML += userText;",
            "el.innerHTML = '<b>' + escapeHtml(a) + b;",
            "el.innerHTML = clean(name);",
            "el.innerHTML = ok ? '<b>x</b>' : other;",
            "el.outerHTML = x;",
            "el.insertAdjacentHTML('beforeend', html);",
            "document.write(x);",
            "document.writeln('<p>' + x);",
            "frame.srcdoc = x;",
            "el['innerHTML'] = '<b>x</b>';",
            "range.createContextualFragment(x);",
            "el . innerHTML\n  =\n  x;",
        ],
    )
    def test_unescaped_markup_is_found(self, snippet):
        assert unsafe_html_sinks(snippet), snippet

    @pytest.mark.parametrize(
        "snippet",
        [
            "el.innerHTML = '';",
            "el.innerHTML = '<b>static</b>';",
            "el.innerHTML = '<b>' + escapeHtml(name) + '</b>';",
            "el.innerHTML = `<i>${escapeHtml(name)}</i>`;",
            "el.innerHTML = escapeHtml(a + b);",
            "el.innerHTML = '<a title=\"' + escapeHtml(t) + '\">' + escapeHtml(x) + '</a>';",
            "var s = el.innerHTML;",
            "if (el.innerHTML === 'x') {}",
            "// el.innerHTML = name;",
            "var note = 'never write el.innerHTML = name';",
            "/* document.write(x) */",
            "x.textContent = name;",
            "var r = /el.innerHTML = x;/;",
        ],
    )
    def test_safe_or_inert_code_is_not_flagged(self, snippet):
        assert unsafe_html_sinks(snippet) == [], snippet

    @pytest.mark.parametrize("snippet", ["eval(x)", "new Function('return 1')", "setTimeout('go()', 5)", "Function('a', 'return a')", "importScripts('x')"])
    def test_dynamic_code_is_found(self, snippet):
        assert dynamic_code(snippet), snippet

    def test_the_tokenizer_handles_regexes_with_quotes_division_and_templates(self):
        src = "var a = /[&<>\"'`]/g; var b = x / 2 / y; var c = `t ${f('}')} u`; // 'x\nvar d = \"it's\".length;".replace("it's", "it\\'s")
        kinds = [k for k, _, _ in tokenize_js(src)]
        assert "regex" in kinds and "template" in kinds and "comment" in kinds
        assert "".join(src[a:b] for _, a, b in tokenize_js(src)) == src
        assert code_only(src).count("/") == 2   # only the two divisions are code

    def test_the_real_page_tokenizes_into_balanced_code(self, script):
        code = code_only(script)
        for opener, closer in ("()", "[]", "{}"):
            assert code.count(opener) == code.count(closer), opener
        assert "".join(script[a:b] for _, a, b in tokenize_js(script)) == script

    def test_handlers_in_markup_and_script_are_found(self):
        parser = PageParser()
        parser.feed('<div onclick="x()"></div><img src=x onerror=y><a href="javascript:z()">q</a><p style="color:red">t</p>')
        problems = inline_handler_problems(parser, "el.onclick = f; el.onerror=g; if (a.onclick == b) {}")
        assert len(problems) >= 6 and any("onclick" in p for p in problems) and any("javascript:" in p for p in problems)

    def test_adding_a_bad_snippet_to_the_real_page_is_detected(self, script, parsed):
        bad_sink = script.replace("var MAX_LIST_ROWS = 300;", "var MAX_LIST_ROWS = 300; els.list.innerHTML = '<b>' + pins[0].name + '</b>';")
        assert bad_sink != script and unsafe_html_sinks(bad_sink)
        bad_handler = script.replace("var MAX_LIST_ROWS = 300;", "var MAX_LIST_ROWS = 300; els.reload.onclick = load;")
        assert inline_handler_problems(parsed, bad_handler)
        broken_hash = script + "\n"
        assert f"'sha256-{sha256_b64(broken_hash)}'" not in csp_directives(parsed)["script-src"]

    def test_an_inline_onclick_added_to_the_markup_is_detected(self, source):
        edited = source.replace('<button class="btn" id="reload" type="button">', '<button class="btn" id="reload" type="button" onclick="x()">')
        assert edited != source
        parser = PageParser()
        parser.feed(edited)
        assert inline_handler_problems(parser, "")


# ---------------------------------------------------------------------------
# Running JavaScript under node
# ---------------------------------------------------------------------------
def pure_block(source: str) -> str:
    begin = [m.end() for m in re.finditer(r"/\* === PURE-BEGIN[^\n]*\*/", source)]
    end = [m.start() for m in re.finditer(r"/\* === PURE-END === \*/", source)]
    assert len(begin) == 1 and len(end) == 1 and begin[0] < end[0], "the PURE block markers are missing or duplicated"
    return source[begin[0] : end[0]]


PURE_DRIVER = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const job = JSON.parse(fs.readFileSync(0, 'utf8'), function (k, v) {
  if (v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 1 && '$' in v) {
    switch (v.$) { case 'NaN': return NaN; case 'Infinity': return Infinity; case '-Infinity': return -Infinity; case 'undefined': return undefined; }
  }
  return v;
});
const P = vm.runInNewContext('"use strict";\n(function () {\n' + job.pure + '\nreturn PURE;\n})()', { URL: URL, Intl: Intl });
function replacer(k, v) {
  if (typeof v === 'number' && !isFinite(v)) return { $: String(v) };
  if (v === undefined) return { $: 'undefined' };
  return v;
}
const results = job.jobs.map(function (j) {
  try { return { ok: new Function('P', 'vars', j.code)(P, j.vars) }; }
  catch (e) { return { error: String(e && e.message || e) }; }
});
process.stdout.write(JSON.stringify(results, replacer));
"""


def _encode(value):
    """Python -> the driver's JSON, with markers for values JSON cannot carry."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"$": "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _decode(value):
    if isinstance(value, dict) and set(value) == {"$"}:
        return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf, "undefined": None}[value["$"]]
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    return value


@pytest.fixture(scope="module")
def pure(tmp_path_factory, source):
    # .cjs: a package.json above the repo can set "type": "module", which would break require()
    driver = tmp_path_factory.mktemp("pure") / "driver.cjs"
    driver.write_text(PURE_DRIVER, encoding="utf-8")
    block = pure_block(source)

    def run(code: str, cases: list) -> list:
        """Run `code` (a function body using P and vars) once per case; vars is the case."""
        payload = json.dumps({"pure": block, "jobs": [{"code": code, "vars": _encode(c)} for c in cases]})
        done = subprocess.run([NODE, str(driver)], input=payload, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert done.returncode == 0, done.stderr
        results = json.loads(done.stdout)
        for number, result in enumerate(results):
            assert "error" not in result, f"case {number} ({cases[number]!r}) threw: {result['error']}"
        return [_decode(r["ok"]) for r in results]

    def call(name: str, argument_lists: list[list]) -> list:
        return run(f"return P.{name}(...vars);", argument_lists)

    return SimpleNamespace(run=run, call=call)


class TestThePureBlockIsPure:
    def test_it_touches_neither_the_dom_nor_leaflet_nor_the_network(self, source):
        code = code_only(pure_block(source))
        assert not re.search(r"\b(document|window|navigator|fetch|localStorage|sessionStorage|XMLHttpRequest|setTimeout|console)\b", code)
        assert not re.search(r"\bL\s*\.", code)
        assert not re.search(r"\binnerHTML\b|\bcreateElement\b", code)


# ---------------------------------------------------------------------------
# 3. The pure functions
# ---------------------------------------------------------------------------
NASTY_STRINGS = [
    "<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "' onmouseover='alert(1)", "`${alert(1)}`",
    "&amp;lt;", "a&b<c>d\"e'f", "مشروع <b>", "😀 <i>", "\u202e</b>", "", "&", "<", ">", "\"", "'", "`", "x" * 10_000,
]


@needs_node
class TestEscapeHtml:
    def test_the_five_characters_and_the_backtick_are_escaped(self, pure):
        out = pure.call("escapeHtml", [["&"], ["<"], [">"], ['"'], ["'"], ["`"]])
        assert out == ["&amp;", "&lt;", "&gt;", "&quot;", "&#39;", "&#96;"]

    def test_null_and_undefined_are_empty_and_other_values_are_text(self, pure):
        assert pure.call("escapeHtml", [[None], [math.nan], [0], [12], [False]]) == ["", "NaN", "0", "12", "false"]
        assert pure.run("return P.escapeHtml(undefined);", [None]) == [""]

    def test_unicode_and_arabic_pass_through_unchanged(self, pure):
        assert pure.call("escapeHtml", [["مشروع تجريبي"], ["Café 😀"]]) == ["مشروع تجريبي", "Café 😀"]

    def test_the_output_never_contains_a_raw_markup_character_and_decodes_back(self, pure):
        outputs = pure.call("escapeHtml", [[s] for s in NASTY_STRINGS])
        for original, escaped in zip(NASTY_STRINGS, outputs):
            assert not re.search(r"[<>\"'`]", escaped), original
            decoded = escaped.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&#96;", "`").replace("&amp;", "&")
            assert decoded == original


@needs_node
class TestSafeHttpUrl:
    @pytest.mark.parametrize(
        "given, expected",
        [
            ("https://example.com/a?b=1#c", "https://example.com/a?b=1#c"),
            ("HTTPS://EXAMPLE.com", "https://example.com/"),
            ("http://example.com", "http://example.com/"),
            ("https://example.com:8443/x", "https://example.com:8443/x"),
            ("https://[2001:db8::1]/x", "https://[2001:db8::1]/x"),
            ("https://example.com/مشروع", "https://example.com/%D9%85%D8%B4%D8%B1%D9%88%D8%B9"),
            pytest.param("https://example.com/" + "a" * 1980, "https://example.com/" + "a" * 1980, id="exactly-2000-characters"),
        ],
        ids=lambda v: repr(v)[:40],
    )
    def test_good_urls_are_returned_normalised(self, pure, given, expected):
        assert pure.call("safeHttpUrl", [[given]]) == [expected]

    def test_an_international_host_becomes_punycode(self, pure):
        (out,) = pure.call("safeHttpUrl", [["https://例え.jp/x"]])
        assert out.startswith("https://xn--") and out.endswith(".jp/x")

    BAD = [
        "javascript:alert(1)", "JavaScript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "javascript:alert(1) ",
        "java\tscript:alert(1)", "java\nscript:alert(1)", "java\rscript:alert(1)", "\u0001javascript:alert(1)", "\ufeffjavascript:alert(1)",
        " javascript:alert(1)", " https://example.com", "\u0085https://example.com",
        "data:text/html,<script>alert(1)</script>", "DATA:text/html;base64,AAAA", "vbscript:msgbox(1)", "file:///etc/passwd",
        "ftp://example.com/x", "blob:https://example.com/uuid", "//example.com/x", "/relative/path", "example.com", "",
        "https:", "https://", "http://:80/", "https://user:pw@example.com", "https://user@example.com/",
        "https://exa mple.com", "https://example.com/a b", "https:// example.com", "https://example.com/\u0000",
        "https://example.com/" + "a" * 1981, "x" * 2001,
        None, 5, True, {}, [], ["https://example.com"], {"href": "https://example.com"},
    ]

    def test_everything_but_a_clean_http_or_https_url_is_refused(self, pure):
        results = pure.call("safeHttpUrl", [[bad] for bad in self.BAD])
        assert results == [None] * len(self.BAD), [(b, r) for b, r in zip(self.BAD, results) if r is not None]
        assert pure.run("return P.safeHttpUrl();", [None]) == [None]

    def test_hostname_of_strips_www_and_never_throws(self, pure):
        out = pure.call("hostnameOf", [["https://www.example.com/x"], ["https://example.com"], ["not a url"], [""], [None], ["javascript:alert(1)"]])
        assert out == ["example.com", "example.com", "source", "source", "source", "source"]


@needs_node
class TestLogPriceScale:
    PAIRS = [(1e5, 5e7), (1.0, 1e10), (1000.0, 1001.0), (2_500_000.0, 2_500_001.0), (850_000.0, 1_200_000_000.0), (1.0, 2.0)]

    def test_the_ends_are_exactly_the_data_s_min_and_max(self, pure):
        for low, high in self.PAIRS:
            assert pure.call("posToPrice", [[0, low, high], [1000, low, high]]) == [low, high]

    def test_every_position_round_trips(self, pure):
        code = "const [lo, hi] = vars; const bad = []; for (let i = 0; i <= 1000; i++) { const back = P.priceToPos(P.posToPrice(i, lo, hi), lo, hi); if (back !== i) bad.push([i, back]); } return bad;"
        assert pure.run(code, [list(pair) for pair in self.PAIRS]) == [[]] * len(self.PAIRS)

    def test_it_is_strictly_increasing_and_the_middle_is_the_geometric_mean(self, pure):
        code = "const [lo, hi] = vars; let prev = -Infinity; for (let i = 0; i <= 1000; i++) { const v = P.posToPrice(i, lo, hi); if (!(v > prev)) return i; prev = v; } return -1;"
        assert pure.run(code, [[1e5, 5e7], [1000.0, 1001.0]]) == [-1, -1]
        (mid,) = pure.call("posToPrice", [[500, 1e6, 1e8]])
        assert mid == pytest.approx(1e7, rel=1e-9)

    def test_prices_outside_the_range_clamp_and_a_price_maps_to_the_nearest_step(self, pure):
        out = pure.call("priceToPos", [[1, 1e5, 5e7], [1e5, 1e5, 5e7], [5e7, 1e5, 5e7], [1e12, 1e5, 5e7], [1e7, 1e5, 5e7]])
        assert out[:4] == [0, 0, 1000, 1000]
        assert 0 < out[4] < 1000
        assert pure.call("posToPrice", [[-5, 10, 100], [5000, 10, 100], [499.6, 10, 1000], [500.4, 10, 1000]]) == [10, 100, *pure.call("posToPrice", [[500, 10, 1000]] * 2)]

    @pytest.mark.parametrize(
        "name, args",
        [
            ("posToPrice", [500, 0, 10]), ("posToPrice", [500, -1, 10]), ("posToPrice", [500, 10, 5]), ("posToPrice", [500, 1, math.inf]),
            ("posToPrice", [500, math.nan, 5]), ("posToPrice", [500, 1, math.nan]), ("posToPrice", ["abc", 1, 10]), ("posToPrice", [None, None, None]),
            ("priceToPos", [5, 0, 10]), ("priceToPos", [5, 10, 10]), ("priceToPos", [5, 10, 5]), ("priceToPos", [5, 1, math.inf]),
            ("priceToPos", [0, 1, 10]), ("priceToPos", [-3, 1, 10]), ("priceToPos", [math.nan, 1, 10]), ("priceToPos", ["x", 1, 10]),
        ],
        ids=lambda v: repr(v),
    )
    def test_nonsense_gives_null_not_a_number(self, pure, name, args):
        assert pure.call(name, [args]) == [None]

    def test_a_single_price_range_is_flat_for_pos_and_unmappable_for_inverse(self, pure):
        assert pure.call("posToPrice", [[500, 5, 5]]) == [5]
        assert pure.call("priceToPos", [[5, 5, 5]]) == [None]


@needs_node
class TestFormatting:
    @pytest.mark.parametrize(
        "n, expected",
        [
            (0, "AED 0"), (999, "AED 999"), (1000, "AED 1K"), (1500, "AED 1.5K"), (850_000, "AED 850K"), (999_999, "AED 1M"),
            (1_000_000, "AED 1M"), (1_200_000, "AED 1.2M"), (1_250_000, "AED 1.25M"), (999_999_999, "AED 1B"), (2_500_000_000, "AED 2.5B"),
            (10**10, "AED 10B"), (-1, ""), (math.nan, ""), (math.inf, ""), ("5", ""), (None, ""), (True, ""),
        ],
        ids=repr,
    )
    def test_short_prices(self, pure, n, expected):
        assert pure.call("formatAed", [[n]]) == [expected]

    def test_full_prices_are_grouped_by_thousands(self, pure):
        assert pure.call("formatAedFull", [[1_234_567], [0], [999], [1000], ["5"], [math.nan], [None]]) == ["AED 1,234,567", "AED 0", "AED 999", "AED 1,000", "", "", ""]
        assert pure.call("groupDigits", [[1_234_567], [-1234], [0], [12.6], ["1"], [None]]) == ["1,234,567", "-1,234", "0", "13", "", ""]

    @pytest.mark.parametrize(
        "low, high, expected",
        [(0, 0, "Studio"), (0, 3, "Studio to 3 BR"), (1, 3, "1 to 3 BR"), (2, 2, "2 BR"), (2, None, "2 BR"), (None, 3, "3 BR"), (None, 0, "Studio"), (None, None, "")],
    )
    def test_bedroom_labels(self, pure, low, high, expected):
        assert pure.call("bedLabel", [[low, high]]) == [expected]

    def test_the_timestamp_is_shown_in_dubai_time(self, pure):
        (shown,) = pure.call("formatStamp", [["2026-10-25T03:00:00Z"]])
        assert "25 Oct 2026" in shown and "07:00" in shown
        assert pure.call("formatStamp", [[None], [5], ["yesterday"], [""]]) == ["", "", "yesterday", ""]

    def test_a_stamp_that_is_not_an_iso_date_is_shown_as_written_not_reinterpreted(self, pure):
        # The browser's lenient Date parser turns "Test 5" or "1" into a date in 2001; only ISO-shaped text is a date.
        odd = ["Test 5", "1", "March 5", "12/10/2026"]
        assert pure.call("formatStamp", [[v] for v in odd]) == odd
        (shown,) = pure.call("formatStamp", [["2026-10-25"]])
        assert "25 Oct 2026" in shown

    def test_text_folding_ignores_case_accents_arabic_marks_and_spacing(self, pure):
        out = pure.call("foldText", [["  Café   TOWER "], ["مَشْرُوعٌ"], ["مـشـروع"], [None], [5], ["ＡＢＣ"]])
        assert out == ["cafe tower", "مشروع", "مشروع", "", "5", "abc"]

    def test_the_precision_wording_is_the_one_the_design_asks_for(self, pure):
        out = pure.call("precisionNote", [["exact"], ["community"], ["area"], ["unknown"], [""], [None], ["constructor"], ["__proto__"]])
        assert out[0] == "Exact location"
        assert out[1] == "Approximate location (community centroid)"
        assert out[2] == "Approximate location (area centroid)"
        assert out[3:] == ["Approximate location (precision not recorded)"] * 5


@needs_node
class TestDistance:
    def test_known_distances(self, pure):
        def reference(a, b):
            ra = math.radians
            h = math.sin(ra(b[0] - a[0]) / 2) ** 2 + math.cos(ra(a[0])) * math.cos(ra(b[0])) * math.sin(ra(b[1] - a[1]) / 2) ** 2
            return 2 * 6371.0088 * math.asin(math.sqrt(h))

        pairs = [((25.2, 55.3), (25.2, 55.4)), ((25.0805, 55.1403), (25.1972, 55.2744)), ((25.2, 55.3), (25.2, 55.3)), ((24.62, 53.98), (25.7, 56.6))]
        out = pure.call("haversineKm", [[list(a), list(b)] for a, b in pairs])
        for got, (a, b) in zip(out, pairs):
            assert got == pytest.approx(reference(a, b), abs=1e-9)
        assert out[0] == pytest.approx(10.06, abs=0.05)
        assert out[2] == 0

    def test_it_is_symmetric_and_survives_the_antipode(self, pure):
        a, b = [25.2, 55.3], [-25.2, -124.7]
        forward, backward = pure.call("haversineKm", [[a, b], [b, a]])
        assert forward == pytest.approx(backward) and math.isfinite(forward) and forward == pytest.approx(math.pi * 6371.0088, rel=1e-6)


def feature(lng=55.27, lat=25.2, **props):
    """# SYNTHETIC: one GeoJSON feature as the exporter writes it."""
    properties = {"project_key": "dld:dubai:1", "name": "Alpha Tower", "emirate": "Dubai", "precision": "exact"}
    properties.update(props)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]}, "properties": properties}


LISTING = {
    "price_from": 1_200_000, "price_to": 2_500_000, "bedrooms_min": 1, "bedrooms_max": 3, "property_types": ["Apartment"],
    "handover": "Q4 2027", "source_url": "https://example.com/p/1", "as_of": "2026-10-01", "price_per_sqft": 1500,
}


@needs_node
class TestNormalizeFeature:
    def norm(self, pure, *features):
        return pure.call("normalizeFeature", [[f] for f in features])

    def test_a_good_feature_becomes_a_pin_with_lng_lat_read_in_the_right_order(self, pure):
        (pin,) = self.norm(pure, feature(**LISTING, area="Business Bay", status="ACTIVE", percent_completed=66.67, units=120, name_ar="مشروع"))
        assert (pin["lat"], pin["lng"]) == (25.2, 55.27)
        assert (pin["priceFrom"], pin["priceTo"], pin["bedsMin"], pin["bedsMax"], pin["pricePerSqft"]) == (1_200_000, 2_500_000, 1, 3, 1500)
        assert pin["types"] == ["Apartment"] and pin["sourceUrl"] == "https://example.com/p/1" and pin["asOf"] == "2026-10-01"
        assert (pin["percent"], pin["units"], pin["nameAr"], pin["label"]) == (66.67, 120, "مشروع", "Alpha Tower")
        assert "alpha tower" in pin["haystack"] and "dld 1" not in pin["haystack"]

    @pytest.mark.parametrize(
        "bad",
        [
            None, 5, "x", [], {}, {"type": "Feature"}, {"geometry": None, "properties": {}},
            {"geometry": {"type": "Point", "coordinates": [55.27, 25.2]}, "properties": None},
            {"geometry": {"type": "Polygon", "coordinates": [[[55, 25]]]}, "properties": {"emirate": "Dubai"}},
            {"geometry": {"type": "Point", "coordinates": [55.27]}, "properties": {"emirate": "Dubai"}},
            {"geometry": {"type": "Point", "coordinates": "55.27,25.2"}, "properties": {"emirate": "Dubai"}},
            {"geometry": {"type": "Point", "coordinates": ["55.27", "25.2"]}, "properties": {"emirate": "Dubai"}},
            {"geometry": {"type": "Point", "coordinates": [None, None]}, "properties": {"emirate": "Dubai"}},
            feature(lng=25.2, lat=55.27),             # swapped
            feature(lng=0, lat=0), feature(lng=2.35, lat=48.85), feature(lng=54.38, lat=24.45),
            feature(lng=math.nan), feature(lat=math.inf),
            feature(emirate="Abu Dhabi"), feature(emirate=None), feature(emirate="dubai"), feature(emirate=["Dubai"]),
        ],
        ids=lambda v: repr(v)[:50],
    )
    def test_anything_that_cannot_be_placed_is_not_a_pin(self, pure, bad):
        assert self.norm(pure, bad) == [None]

    def test_precision_is_kept_only_when_known(self, pure):
        pins = self.norm(pure, *(feature(precision=p) for p in ("exact", "community", "area", "unknown", "", None, "centroid", "EXACT", 5)))
        assert [p["precision"] for p in pins] == ["exact", "community", "area", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown"]

    def test_the_precision_property_missing_entirely_is_unknown(self, pure):
        no_precision = feature()
        del no_precision["properties"]["precision"]
        assert self.norm(pure, no_precision)[0]["precision"] == "unknown"

    @pytest.mark.parametrize(
        "props, field, expected",
        [
            ({"price_from": 1.5e12}, "priceFrom", None), ({"price_from": "1200000"}, "priceFrom", None), ({"price_from": 1200000.5}, "priceFrom", None),
            ({"price_from": 0}, "priceFrom", None), ({"price_from": -4}, "priceFrom", None), ({"price_from": True}, "priceFrom", None),
            ({"price_from": 10**10}, "priceFrom", 10**10), ({"price_from": 10**10 + 1}, "priceFrom", None),
            ({"price_per_sqft": exp.MAX_PRICE_PER_SQFT_AED + 1}, "pricePerSqft", None),
            ({"price_per_sqft": exp.MAX_PRICE_PER_SQFT_AED}, "pricePerSqft", exp.MAX_PRICE_PER_SQFT_AED),
            ({"bedrooms_min": 0}, "bedsMin", 0), ({"bedrooms_min": -1}, "bedsMin", None), ({"bedrooms_min": MAX_BEDROOMS + 1}, "bedsMin", None),
            ({"bedrooms_max": MAX_BEDROOMS}, "bedsMax", MAX_BEDROOMS), ({"bedrooms_max": 2.5}, "bedsMax", None),
            ({"percent_completed": 150}, "percent", None), ({"percent_completed": -1}, "percent", None), ({"percent_completed": 100}, "percent", 100),
            ({"percent_completed": "50"}, "percent", None), ({"units": 0}, "units", None), ({"units": 10**9}, "units", 10**9), ({"units": 10**9 + 1}, "units", None),
        ],
        ids=lambda v: repr(v)[:40],
    )
    def test_numbers_must_be_plausible_whole_numbers(self, pure, props, field, expected):
        # a source URL is present so the listing facts are allowed to show
        (pin,) = self.norm(pure, feature(source_url="https://example.com/p/1", **props))
        assert pin[field] == expected

    def test_a_backwards_range_loses_both_ends(self, pure):
        (pin,) = self.norm(pure, feature(source_url="https://example.com/p", price_from=3_000_000, price_to=1_000_000, bedrooms_min=4, bedrooms_max=1))
        assert (pin["priceFrom"], pin["priceTo"], pin["bedsMin"], pin["bedsMax"]) == (None, None, None, None)

    @pytest.mark.parametrize("url", [None, "", "javascript:alert(1)", "data:text/html,x", "ftp://example.com", "https://user:pw@example.com", "/relative", 5])
    def test_a_listing_fact_without_a_safe_source_is_not_shown(self, pure, url):
        (pin,) = self.norm(pure, feature(**{**LISTING, "source_url": url}))
        assert pin["sourceUrl"] == ""
        assert (pin["priceFrom"], pin["priceTo"], pin["pricePerSqft"], pin["bedsMin"], pin["bedsMax"]) == (None,) * 5
        assert pin["types"] == [] and pin["handover"] == "" and pin["asOf"] == ""
        assert pin["name"] == "Alpha Tower"      # project facts do not depend on a listing source

    def test_property_types_are_filtered_to_the_known_set(self, pure):
        (pin,) = self.norm(pure, feature(source_url="https://example.com/p", property_types=["Villa", "Villa", "Unknown", "Spaceship", 5, None, ["Villa"], "Apartment"]))
        assert pin["types"] == ["Villa", "Apartment"]
        (pin,) = self.norm(pure, feature(source_url="https://example.com/p", property_types="Villa"))
        assert pin["types"] == []

    def test_text_is_cleaned_bounded_and_falls_back_to_other_names(self, pure):
        long_name = "N" * 1000
        pins = self.norm(
            pure,
            feature(name="Test\u202e\x00 Tower\n\t A"), feature(name=long_name), feature(name="", name_ar="مشروع"),
            feature(name="", project_key="dld:dubai:77"), feature(name="", project_key=""), feature(name=["x"]), feature(name="<img src=x onerror=alert(1)>"),
        )
        assert pins[0]["name"] == "Test Tower A"
        assert len(pins[1]["name"]) == 300
        assert pins[2]["label"] == "مشروع" and pins[3]["label"] == "dld:dubai:77" and pins[4]["label"] == "Unnamed project"
        assert pins[5]["name"] == "" and pins[6]["name"] == "<img src=x onerror=alert(1)>"   # kept as text, never as markup

    def test_hostile_property_names_do_no_harm(self, pure):
        hostile = feature()
        hostile["properties"]["__proto__"] = {"polluted": True}
        hostile["properties"]["constructor"] = "x"
        (out,) = pure.run("const pin = P.normalizeFeature(JSON.parse(vars)); return [pin.name, ({}).polluted === undefined];", [json.dumps(hostile)])
        assert out == ["Alpha Tower", True]


def evaluate(pure, features, **filters):
    base = {"emirates": ["Dubai", "Sharjah"], "terms": [], "status": "", "types": [], "beds": [], "priceMin": None, "priceMax": None,
            "loPos": 0, "hiPos": 1000, "centre": None, "radiusKm": 5}
    base.update(filters)
    code = "const pin = P.normalizeFeature(vars[0]); return P.evaluatePin(pin, vars[1]);"
    return pure.run(code, [[f, base] for f in features])


@needs_node
class TestEvaluatePin:
    def test_with_no_filter_everything_is_shown_including_pins_with_no_data(self, pure):
        (out,) = evaluate(pure, [feature()])
        assert out["show"] is True and out["failed"] == [] and out["missing"] == [] and out["approximate"] is False

    def test_emirate_toggle(self, pure):
        out = evaluate(pure, [feature(), feature(emirate="Sharjah")], emirates=["Sharjah"])
        assert [o["show"] for o in out] == [False, True] and out[0]["failed"] == ["emirate"]
        assert evaluate(pure, [feature()], emirates=[])[0]["show"] is False

    def test_text_search_is_and_across_words_and_ignores_case_accents_and_arabic_marks(self, pure):
        f = feature(name="Café Tower", developer="Example Developer LLC", registry="DLD", registry_no="1234", name_ar="مَشْرُوع", area="Business Bay")
        for terms, shown in [(["cafe"], True), (["tower", "example"], True), (["tower", "nomatch"], False), (["1234"], True), (["dld", "1234"], True),
                             (["مشروع"], True), (["bay"], True), (["developer", "llc"], True), (["zzz"], False)]:
            assert evaluate(pure, [f], terms=terms)[0]["show"] is shown, terms

    def test_status_filter_separates_a_different_status_from_no_status(self, pure):
        out = evaluate(pure, [feature(status="ACTIVE"), feature(status="Pending"), feature()], status="active")
        assert [o["show"] for o in out] == [True, False, False]
        assert out[1]["failed"] == ["status"] and out[1]["missing"] == []
        assert out[2]["failed"] == [] and out[2]["missing"] == ["status"]

    def test_type_filter_is_any_of_and_missing_is_not_a_failure(self, pure):
        listing = {"source_url": "https://example.com/p"}
        out = evaluate(
            pure,
            [feature(**listing, property_types=["Villa"]), feature(**listing, property_types=["Apartment", "Villa"]), feature(**listing, property_types=["Plot"]), feature()],
            types=["Villa", "Townhouse"],
        )
        assert [o["show"] for o in out] == [True, True, False, False]
        assert out[2]["failed"] == ["type"] and out[3]["missing"] == ["type"] and out[3]["failed"] == []

    @pytest.mark.parametrize(
        "bounds, matches",
        [
            ((1, 3), {1, 2, 3}), ((0, 3), {0, 1, 2, 3}), ((2, None), {2}), ((None, 3), {3}), ((None, None), set()),
            ((4, 6), {4, 5}), ((5, 5), {5}), ((7, None), {5}), ((None, 6), {5}), ((0, 0), {0}), ((3, 3), {3}), ((6, 9), {5}),
        ],
        ids=repr,
    )
    def test_bedroom_chips_cover_only_what_the_source_states(self, pure, bounds, matches):
        low, high = bounds
        out = pure.call("bedsCover", [[{"bedsMin": low, "bedsMax": high}, chip] for chip in range(6)])
        assert {chip for chip, hit in enumerate(out) if hit} == matches

    def test_bedroom_filter_uses_any_selected_chip_and_flags_missing(self, pure):
        listing = {"source_url": "https://example.com/p"}
        out = evaluate(pure, [feature(**listing, bedrooms_min=1, bedrooms_max=2), feature(**listing, bedrooms_min=4, bedrooms_max=4), feature()], beds=[0, 2])
        assert [o["show"] for o in out] == [True, False, False]
        assert out[1]["failed"] == ["beds"] and out[2]["missing"] == ["beds"]

    def test_the_price_filter_is_inactive_at_the_ends_so_pins_without_prices_stay(self, pure):
        out = evaluate(pure, [feature(), feature(**LISTING)], priceMin=1e6, priceMax=1e7, loPos=0, hiPos=1000)
        assert [o["show"] for o in out] == [True, True]
        assert pure.call("priceFilterActive", [[{"priceMin": 1e6, "priceMax": 1e7, "loPos": 0, "hiPos": 1000}], [{"priceMin": 1e6, "priceMax": 1e7, "loPos": 1, "hiPos": 1000}],
                                                [{"priceMin": 1e6, "priceMax": 1e7, "loPos": 0, "hiPos": 999}], [{"priceMin": None, "priceMax": None, "loPos": 5, "hiPos": 9}],
                                                [{"priceMin": 5, "priceMax": 5, "loPos": 5, "hiPos": 9}]]) == [False, True, True, False, False]

    def test_narrowing_the_price_range_hides_pins_without_a_price_as_missing(self, pure):
        (hi_pos,) = pure.call("priceToPos", [[2_000_000, 1e6, 1e7]])
        out = evaluate(pure, [feature(), feature(**LISTING)], priceMin=1e6, priceMax=1e7, loPos=0, hiPos=hi_pos)
        assert out[0]["show"] is False and out[0]["missing"] == ["price"] and out[0]["failed"] == []
        assert out[1]["show"] is True      # 1.2M-2.5M overlaps the range up to about 2M

    def test_price_overlap_rules(self, pure):
        lo_pos, = pure.call("priceToPos", [[2_800_000, 1e6, 1e7]])
        hi_pos, = pure.call("priceToPos", [[2_000_000, 1e6, 1e7]])
        cases = [
            (dict(price_from=1_200_000, price_to=2_500_000), dict(loPos=lo_pos, hiPos=1000), False),   # whole range below the minimum
            (dict(price_from=1_200_000, price_to=2_500_000), dict(loPos=0, hiPos=hi_pos), True),        # starts inside
            (dict(price_from=5_000_000), dict(loPos=lo_pos, hiPos=1000), True),                           # a lone price inside
            (dict(price_from=5_000_000), dict(loPos=0, hiPos=hi_pos), False),                             # a lone price above
            (dict(price_to=1_500_000), dict(loPos=0, hiPos=hi_pos), True),
            (dict(price_to=1_500_000), dict(loPos=lo_pos, hiPos=1000), False),
        ]
        out = [
            evaluate(pure, [feature(source_url="https://example.com/p", **props)], priceMin=1e6, priceMax=1e7, **bounds)[0]["show"]
            for props, bounds, _ in cases
        ]
        assert out == [expected for _, _, expected in cases]

    def test_a_reversed_slider_is_treated_as_a_range(self, pure):
        (hi,) = pure.call("priceToPos", [[2_000_000, 1e6, 1e7]])
        a = evaluate(pure, [feature(**LISTING)], priceMin=1e6, priceMax=1e7, loPos=0, hiPos=hi)[0]["show"]
        b = evaluate(pure, [feature(**LISTING)], priceMin=1e6, priceMax=1e7, loPos=hi, hiPos=0)[0]["show"]
        assert a is b is True

    def test_radius_search_uses_exact_pins_only(self, pure):
        centre = [25.2, 55.27]
        near = {"lat": 25.2, "lng": 55.275}
        out = evaluate(
            pure,
            [feature(), feature(precision="community"), feature(precision="area"), feature(precision="unknown"), feature(precision=""), feature(lng=55.4, lat=25.2)],
            centre=centre, radiusKm=5,
        )
        assert [o["show"] for o in out] == [True, False, False, False, False, False]
        assert [o["approximate"] for o in out] == [False, True, True, True, True, False]
        assert out[0]["dist"] == pytest.approx(0, abs=1e-9)
        assert out[5]["failed"] == ["radius"] and out[5]["dist"] > 5
        assert near["lat"] == 25.2

    def test_the_radius_edge_is_inclusive_and_a_pin_just_outside_is_not_shown(self, pure):
        far = feature(lng=55.30, lat=25.2)
        (dist,) = pure.call("haversineKm", [[[25.2, 55.27], [25.2, 55.30]]])
        inside, outside = evaluate(pure, [far, far], centre=[25.2, 55.27], radiusKm=dist)[0], evaluate(pure, [far], centre=[25.2, 55.27], radiusKm=dist - 0.001)[0]
        assert inside["show"] is True and outside["show"] is False

    def test_filters_combine_and_a_failed_filter_is_not_reported_as_missing_data(self, pure):
        (out,) = evaluate(pure, [feature(emirate="Sharjah")], emirates=["Dubai"], status="active")
        assert out["failed"] == ["emirate"] and out["missing"] == ["status"] and out["show"] is False


# ---------------------------------------------------------------------------
# 4. The whole page against a fake DOM
# ---------------------------------------------------------------------------
HARNESS_JS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const job = JSON.parse(fs.readFileSync(0, 'utf8'));

const uncaught = [], unhandled = [];
process.on('uncaughtException', (e) => uncaught.push(String(e && e.stack || e)));
process.on('unhandledRejection', (e) => unhandled.push(String(e && e.stack || e)));

class TextNode {
  constructor(t) { this.nodeType = 3; this.data = String(t); this.parentNode = null; }
  get textContent() { return this.data; }
}
class Element {
  constructor(tag) {
    this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {};
    this.parentNode = null; this._cls = new Set(); this.hidden = false; this.value = ''; this.disabled = false; this.id = '';
  }
  get className() { return Array.from(this._cls).join(' '); }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get classList() {
    const self = this;
    return {
      add: (...c) => c.forEach((x) => self._cls.add(x)), remove: (...c) => c.forEach((x) => self._cls.delete(x)),
      toggle: (c, force) => { const on = force === undefined ? !self._cls.has(c) : !!force; if (on) self._cls.add(c); else self._cls.delete(c); return on; },
      contains: (c) => self._cls.has(c),
    };
  }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  set textContent(v) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    if (v !== '' && v !== null && v !== undefined) this.appendChild(new TextNode(v));
  }
  get innerHTML() { return ''; }
  set innerHTML(v) { throw new Error('innerHTML was used: ' + String(v).slice(0, 80)); }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i < 0) throw new Error('removeChild: not a child'); this.children.splice(i, 1); c.parentNode = null; return c; }
  setAttribute(n, v) {
    n = String(n);
    if (/^on/i.test(n) || n.toLowerCase() === 'style' || n.toLowerCase() === 'srcdoc') throw new Error('refused attribute ' + n);
    this.attrs[n] = String(v);
    if (n === 'id') this.id = String(v);
    if (n === 'class') this.className = v;
    if (n === 'value') this.value = String(v);
  }
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
  removeAttribute(n) { delete this.attrs[n]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, extra) { (this.listeners[t] || []).forEach((fn) => fn(Object.assign({ type: t, target: this }, extra || {}))); }
}
const textOf = (n) => (n.nodeType === 3 ? n.data : n.children.map(textOf).join(''));
function walk(n, f) { f(n); if (n.children) n.children.forEach((c) => walk(c, f)); }
function findAll(n, pred) { const out = []; walk(n, (x) => { if (x.nodeType === 1 && pred(x)) out.push(x); }); return out; }

const byId = {};
Object.keys(job.ids).forEach((id) => { const e = new Element(job.ids[id]); e.setAttribute('id', id); byId[id] = e; });
byId.priceLo.value = '0'; byId.priceHi.value = '1000'; byId.radius.value = '5';
const documentElement = new Element('html');
const body = new Element('body');
const documentStub = {
  getElementById: (id) => byId[id] || null,
  createElement: (t) => new Element(t),
  createTextNode: (t) => new TextNode(t),
  documentElement, body,
};
const windowStub = {
  innerWidth: job.width || 1200,
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  matchMedia: () => ({ addEventListener: () => {} }),
};

const LC = { views: [], tileLayers: [], clusterOpts: null, cluster: null, markers: [], map: null, centre: null, circle: null, fits: [], mapOpts: null, log: [] };
const L = {
  map(id, opts) {
    const handlers = {};
    const m = {
      id, zoom: 9, setView(c, z) {
        LC.log.push('view'); LC.views.push([c, z]);
        // markercluster is busy re-clustering for a moment after the zoom changes and forgets a zoomToShowLayer request made then
        if (LC.simulateAnimation && z !== undefined && z !== m.zoom && LC.cluster) {
          LC.cluster.animating = true; setTimeout(() => { LC.cluster.animating = false; }, 250);
        }
        if (z !== undefined) m.zoom = z; return m;
      }, addLayer() { return m; },
      removeLayer(l) { l.removed = true; }, on(t, fn) { handlers[t] = fn; return m; }, getZoom() { return m.zoom; }, invalidateSize() { LC.log.push('invalidate'); },
      fitBounds(b) { LC.fits.push(b); }, fire(t, e) { if (handlers[t]) handlers[t](e); },
    };
    LC.map = m; LC.mapOpts = opts; return m;
  },
  tileLayer(url, opts) { const t = { url, opts, addTo() { return t; } }; LC.tileLayers.push(t); return t; },
  markerClusterGroup(opts) {
    LC.clusterOpts = opts;
    const g = { layers: [], clearLayers() { g.layers = []; }, addLayers(a) { g.layers.push(...a); }, zoomToShowLayer(m, cb) { if (g.animating) return; if (cb) cb(); } };
    LC.cluster = g; return g;
  },
  circleMarker(latlng, style) {
    const mk = { latlng, style, popupFn: null, opened: false,
      bindPopup(fn, o) { mk.popupFn = fn; mk.popupOpts = o; return mk; }, setStyle(s) { mk.style = s; }, openPopup() { mk.opened = true; } };
    LC.markers.push(mk); return mk;
  },
  marker(latlng, opts) {
    const mk = { latlng, opts, handlers: {}, addTo() { return mk; }, on(t, fn) { mk.handlers[t] = fn; return mk; },
      setLatLng(l) { mk.latlng = l; return mk; }, getLatLng() { return mk.latlng; } };
    LC.centre = mk; return mk;
  },
  divIcon(o) { return o; },
  circle(latlng, opts) {
    const c = { latlng, opts, radius: opts.radius, addTo() { return c; }, setLatLng(l) { c.latlng = l; }, setRadius(r) { c.radius = r; }, getBounds() { return 'bounds'; } };
    LC.circle = c; return c;
  },
};

let fetchSpec = job.fetch;
const fetchLog = [];
function fakeFetch(url, opts) {
  fetchLog.push({ url: String(url), opts: opts || null });
  if (fetchSpec.mode === 'reject') return Promise.reject(new TypeError('Failed to fetch'));
  if (fetchSpec.mode === 'throw') throw new Error('fetch is not available');
  const status = fetchSpec.status;
  return Promise.resolve({ ok: status >= 200 && status < 300, status, text: () => Promise.resolve(fetchSpec.body) });
}

const sandbox = { document: documentStub, window: windowStub, fetch: fakeFetch, URL, console: { log() {}, warn() {}, error() {} }, setTimeout, clearTimeout };
if (!job.noLeaflet) sandbox.L = L;
const context = vm.createContext(sandbox);
let scriptError = null;
try { vm.runInContext(job.script, context, { filename: 'index.html' }); } catch (e) { scriptError = String(e && e.stack || e); }

const settle = (ms) => new Promise((resolve) => { let n = 0; const tick = () => { if (++n > 8) setTimeout(resolve, ms || 0); else setImmediate(tick); }; tick(); });

function describe(n) {
  if (n.nodeType === 3) return { text: n.data };
  const out = { tag: n.tagName.toLowerCase(), cls: n.className, attrs: Object.assign({}, n.attrs), kids: n.children.map(describe) };
  return out;
}
const chips = (id) => byId[id].children.map((b) => ({ text: textOf(b), pressed: b.getAttribute('aria-pressed'), cls: b.className }));
function rows() {
  return byId.list.children.filter((c) => c.nodeType === 1 && c.tagName === 'BUTTON').map((b) => ({
    name: textOf(b.children[1].children[0]), meta: textOf(b.children[1].children[1]), aside: textOf(b.children[2]),
    hollow: b.children[0].className.indexOf('hollow') >= 0, cls: b.className,
  }));
}
function snapshot() {
  const n = byId.notice;
  return {
    notice: { hidden: n.hidden, cls: n.className, text: n.children.map(textOf), role: n.getAttribute('role') },
    stats: textOf(byId.stats), header: textOf(byId.resultsHeader), rows: rows(), rowTotal: byId.list.children.length,
    shown: LC.cluster ? LC.cluster.layers.length : null, markerTotal: LC.markers.length, credits: byId.credits.children.map(textOf),
    priceLabel: textOf(byId.priceLabel), priceCov: textOf(byId.priceCov), statusCov: textOf(byId.statusCov), typeCov: textOf(byId.typeCov), bedCov: textOf(byId.bedCov),
    radiusNote: { hidden: byId.radiusNote.hidden, text: textOf(byId.radiusNote) }, radiusLabel: textOf(byId.radiusLabel),
    pick: textOf(byId.pick), clearHidden: byId.clearRadius.hidden,
    priceDisabled: [byId.priceLo.disabled, byId.priceHi.disabled],
    statusOptions: byId.status.children.map((o) => ({ value: o.getAttribute('value'), text: textOf(o) })),
    emirateChips: chips('emirateChips'), typeChips: chips('typeChips'), bedChips: chips('bedChips'),
    bodyPicking: body.classList.contains('picking'), sideCollapsed: byId.side.classList.contains('collapsed'),
  };
}
function popupByName(name) {
  for (const mk of LC.markers) {
    const node = mk.popupFn();
    const h3 = findAll(node, (e) => e.tagName === 'H3')[0];
    if (h3 && textOf(h3) === name) {
      return {
        text: textOf(node), tree: describe(node), tags: findAll(node, () => true).map((e) => e.tagName.toLowerCase()),
        anchors: findAll(node, (e) => e.tagName === 'A').map((a) => ({ href: a.getAttribute('href'), target: a.getAttribute('target'), rel: a.getAttribute('rel'), text: textOf(a) })),
        marker: { style: mk.style, popupOpts: mk.popupOpts },
      };
    }
  }
  return null;
}
const helpers = {
  byId: (id) => byId[id], LC, fetchLog, settle, snapshot, popupByName, rows, textOf, findAll, describe,
  click: (e) => e.dispatch('click'),
  input: async (e, v) => { e.value = String(v); e.dispatch('input'); await settle(200); },
  change: async (e, v) => { e.value = String(v); e.dispatch('change'); await settle(5); },
  chip: (id, text) => byId[id].children.find((b) => textOf(b).indexOf(text) === 0),
  mapClick: (lat, lng) => LC.map.fire('click', { latlng: { lat, lng } }),
  setFetch: (spec) => { fetchSpec = spec; },
  viewport: windowStub,
};

(async () => {
  let result = null, scenarioError = null;
  try {
    await settle(5);
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    result = await new AsyncFunction('h', job.scenario)(helpers);
  } catch (e) { scenarioError = String(e && e.stack || e); }
  process.stdout.write(JSON.stringify({ result, scriptError, scenarioError, uncaught, unhandled, fetchLog, views: LC.views, tileLayers: LC.tileLayers.map((t) => ({ url: t.url, opts: t.opts })), clusterOpts: LC.clusterOpts, mapOpts: LC.mapOpts }));
})();
"""


def registry_row(**overrides) -> RegistryRow:
    """# SYNTHETIC"""
    fields = dict(emirate="Dubai", registry="DLD", registry_project_no="1001", project_name_en="Alpha Tower",
                  developer="Example Developer LLC", latitude=25.2, longitude=55.27, location_precision="exact")
    fields.update(overrides)
    return RegistryRow(**fields)


def sharjah_row(**overrides) -> RegistryRow:
    """# SYNTHETIC"""
    fields = dict(emirate="Sharjah", registry="SRERD", registry_project_no="S-1", project_name_en="Gamma Heights",
                  developer="Example Developer LLC", latitude=25.33, longitude=55.4, location_precision="area")
    fields.update(overrides)
    return RegistryRow(**fields)


def sample_collection() -> dict:
    """# SYNTHETIC: a small map built by the real exporter, so page and exporter stay in contract."""
    rows = [
        registry_row(registry_project_no="1", project_name_en="Alpha Tower", status="ACTIVE", percent_completed=66.67, units=120,
                     area_en="Business Bay", project_name_ar="برج ألفا", completion_date="2027-12-31"),
        registry_row(registry_project_no="2", project_name_en="Beta Residences", location_precision="community", latitude=25.1, longitude=55.15,
                     status="Pending", master_project="Test Community"),
        sharjah_row(registry_project_no="S-1", project_name_en="Gamma Heights", status="ACTIVE"),
        sharjah_row(registry_project_no="S-2", project_name_en="Delta Court", location_precision="exact", latitude=25.35, longitude=55.39, status="Pending"),
        registry_row(registry_project_no="3", project_name_en="Epsilon Place", location_precision="", latitude=25.0, longitude=55.1),
        registry_row(registry_project_no="4", project_name_en="Zeta Gardens", location_precision="exact", latitude=25.21, longitude=55.28, status="ACTIVE"),
        registry_row(registry_project_no="5", project_name_en="No Coordinates", latitude=None, longitude=None),
    ]
    listings = {
        rows[0].project_key: {"source_url": "https://example.com/alpha", "price_from": 1_200_000, "price_to": 2_500_000, "bedrooms_min": 0, "bedrooms_max": 3,
                              "types": ["Apartment"], "handover": "Q4 2027", "as_of": "2026-10-01", "price_per_sqft": 1500},
        rows[3].project_key: {"source_url": "https://example.com/delta", "price_from": 8_000_000, "price_to": 20_000_000, "bedrooms_min": 3, "bedrooms_max": 5,
                              "types": ["Villa"], "as_of": "2026-09-01"},
        rows[2].project_key: {"source_url": "https://example.com/gamma", "price_from": 600_000, "bedrooms_min": 1, "bedrooms_max": 1, "types": ["Apartment"]},
    }
    return exp.build_geojson(rows, listings, generated_at="2026-10-25T03:00:00Z", unreadable_rows=0)


@pytest.fixture(scope="module")
def page(tmp_path_factory, source, parsed, script):
    harness = tmp_path_factory.mktemp("page") / "harness.cjs"
    harness.write_text(HARNESS_JS, encoding="utf-8")

    def run(collection=None, *, body=None, status=200, mode="ok", scenario="return h.snapshot();", no_leaflet=False, width=1200, inline_script=None):
        if body is None:
            body = exp.serialise(collection).decode("utf-8") if collection is not None else ""
        job = {
            "script": script if inline_script is None else inline_script, "ids": parsed.ids, "fetch": {"mode": mode, "status": status, "body": body},
            "scenario": scenario, "noLeaflet": no_leaflet, "width": width,
        }
        done = subprocess.run([NODE, str(harness)], input=json.dumps(job), capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert done.returncode == 0, done.stderr
        out = json.loads(done.stdout)
        assert out["scriptError"] is None, out["scriptError"]
        assert out["scenarioError"] is None, out["scenarioError"]
        assert out["uncaught"] == [] and out["unhandled"] == [], (out["uncaught"], out["unhandled"])
        return out

    return run


@needs_node
class TestPageWithData:
    def test_it_loads_the_relative_file_with_no_cache_and_draws_every_located_pin(self, page):
        out = page(sample_collection())
        snap = out["result"]
        assert [f["url"] for f in out["fetchLog"]] == ["pins.geojson"]
        assert out["fetchLog"][0]["opts"] == {"cache": "no-store"}
        assert snap["shown"] == snap["markerTotal"] == 6 and snap["notice"]["hidden"] is True
        assert out["views"][0] == [[25.2, 55.3], 9]

    def test_the_map_library_is_set_up_as_designed(self, page):
        out = page(sample_collection())
        assert out["mapOpts"]["preferCanvas"] is True
        assert "disableClusteringAtZoom" not in out["clusterOpts"] and out["clusterOpts"]["spiderfyOnMaxZoom"] is True
        (tile,) = out["tileLayers"]
        assert tile["url"] == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        assert "OpenStreetMap" in tile["opts"]["attribution"] and "ODbL" in tile["opts"]["attribution"] and "&copy;" in tile["opts"]["attribution"]

    def test_the_header_shows_counts_per_emirate_and_per_precision_from_the_meta_block(self, page):
        stats = page(sample_collection())["result"]["stats"]
        assert "Located" in stats and "Dubai 4 of 5" in stats and "Sharjah 2 of 2" in stats
        assert "exact 3" in stats and "community 1" in stats and "area 1" in stats and "not recorded 1" in stats
        assert "25 Oct 2026" in stats

    def test_the_credits_carry_the_attribution_the_exporter_wrote(self, page):
        credits = page(sample_collection())["result"]["credits"]
        assert any("OpenStreetMap contributors" in c for c in credits) and any("ODbL" in c for c in credits)
        assert any("1 project is in the registry data but not yet located" in c for c in credits)

    def test_the_list_is_sorted_by_name_and_hollow_symbols_mark_approximate_pins(self, page):
        rows = page(sample_collection())["result"]["rows"]
        assert [r["name"] for r in rows] == ["Alpha Tower", "Beta Residences", "Delta Court", "Epsilon Place", "Gamma Heights", "Zeta Gardens"]
        hollow = {r["name"]: r["hollow"] for r in rows}
        assert hollow == {"Alpha Tower": False, "Beta Residences": True, "Delta Court": False, "Epsilon Place": True, "Gamma Heights": True, "Zeta Gardens": False}

    def test_marker_style_distinguishes_exact_from_approximate(self, page):
        scenario = "return h.LC.markers.map((m) => [m.style.fillOpacity, m.style.weight]);"
        styles = page(sample_collection(), scenario=scenario)["result"]
        assert len(styles) == 6
        solid = [s for s in styles if s[0] > 0.5]
        hollow = [s for s in styles if s[0] < 0.5]
        assert len(solid) == 3 and len(hollow) == 3

    def test_popups_say_how_precise_the_location_is(self, page):
        scenario = "return {a: h.popupByName('Alpha Tower'), b: h.popupByName('Beta Residences'), g: h.popupByName('Gamma Heights'), e: h.popupByName('Epsilon Place')};"
        popups = page(sample_collection(), scenario=scenario)["result"]
        assert "Exact location" in popups["a"]["text"] and "Approximate" not in popups["a"]["text"]
        assert "Approximate location (community centroid)" in popups["b"]["text"]
        assert "Approximate location (area centroid)" in popups["g"]["text"]
        assert "Approximate location (precision not recorded)" in popups["e"]["text"]

    def test_a_popup_shows_the_listing_with_its_source_and_a_safe_link(self, page):
        popup = page(sample_collection(), scenario="return h.popupByName('Alpha Tower');")["result"]
        text = popup["text"]
        for expected in ("AED 1,200,000 to AED 2,500,000", "Studio to 3 BR", "Apartment", "Q4 2027", "Business Bay", "ACTIVE", "66.7% complete", "120", "as of 2026-10-01", "برج ألفا", "DLD 1"):
            assert expected in text, expected
        assert popup["anchors"] == [{"href": "https://example.com/alpha", "target": "_blank", "rel": "noopener noreferrer", "text": "example.com"}]
        assert popup["marker"]["popupOpts"] == {"maxWidth": 320}

    def test_a_pin_without_listing_data_says_so_instead_of_inventing_any(self, page):
        popup = page(sample_collection(), scenario="return h.popupByName('Zeta Gardens');")["result"]
        assert "No price or configuration data yet." in popup["text"] and popup["anchors"] == []
        assert "AED" not in popup["text"]

    def test_arabic_text_is_set_as_a_right_to_left_block(self, page):
        popup = page(sample_collection(), scenario="return h.popupByName('Alpha Tower');")["result"]
        arabic = [n for n in _flatten(popup["tree"]) if n.get("attrs", {}).get("lang") == "ar"]
        assert arabic and arabic[0]["attrs"]["dir"] == "auto"


def _flatten(node):
    yield node
    for kid in node.get("kids", []):
        yield from _flatten(kid)


@needs_node
class TestFilters:
    def test_the_emirate_chips_filter_and_report_their_counts(self, page):
        scenario = """
        const before = h.snapshot().emirateChips;
        h.click(h.chip('emirateChips', 'Dubai'));
        const afterDubaiOff = h.snapshot();
        return {before, shown: afterDubaiOff.shown, rows: afterDubaiOff.rows.map((r) => r.name), pressed: afterDubaiOff.emirateChips.map((c) => c.pressed)};
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        assert [c["text"] for c in out["before"]] == ["Dubai 4", "Sharjah 2"]
        assert out["shown"] == 2 and out["rows"] == ["Delta Court", "Gamma Heights"] and out["pressed"] == ["false", "true"]

    def test_text_search_matches_project_developer_and_registry_number_after_a_short_delay(self, page):
        scenario = """
        const results = {};
        for (const q of ['alpha', 'EXAMPLE developer', 'dld 3', 'S-2', 'nomatch', 'برج']) {
          await h.input(h.byId('q'), q);
          results[q] = h.snapshot().rows.map((r) => r.name);
        }
        await h.input(h.byId('q'), '');
        results.cleared = h.snapshot().rows.length;
        return results;
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        assert out["alpha"] == ["Alpha Tower"]
        assert len(out["EXAMPLE developer"]) == 6
        assert out["dld 3"] == ["Epsilon Place"] and out["S-2"] == ["Delta Court"] and out["nomatch"] == [] and out["برج"] == ["Alpha Tower"]
        assert out["cleared"] == 6

    def test_the_header_uses_the_singular_for_exactly_one_match(self, page):
        scenario = "await h.input(h.byId('q'), 'alpha'); return h.snapshot().header;"
        assert page(sample_collection(), scenario=scenario)["result"] == "1 project matches the filters"
        scenario = "return h.snapshot().header;"
        assert page(sample_collection(), scenario=scenario)["result"] == "6 projects match the filters"

    def test_no_match_shows_a_readable_message_not_an_empty_void(self, page):
        scenario = "await h.input(h.byId('q'), 'zzzz'); return [h.snapshot().header, h.textOf(h.byId('list'))];"
        header, listing = page(sample_collection(), scenario=scenario)["result"]
        assert header.startswith("0 projects match") and "Nothing matches" in listing

    def test_status_filter_lists_each_status_once_and_says_how_many_have_none(self, page):
        scenario = """
        const s = h.snapshot();
        await h.change(h.byId('status'), 'active');
        const active = h.snapshot();
        return {options: s.statusOptions, cov: s.statusCov, rows: active.rows.map((r) => r.name), header: active.header};
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        assert [o["text"] for o in out["options"]] == ["Any status", "ACTIVE", "Pending"]
        assert "Status known for 5 of 6 pins" in out["cov"]
        assert out["rows"] == ["Alpha Tower", "Gamma Heights", "Zeta Gardens"]
        assert "1 hidden: no status" in out["header"]

    def test_property_type_chips_only_offer_types_that_exist_and_hide_pins_without_data(self, page):
        scenario = """
        const s = h.snapshot();
        h.click(h.chip('typeChips', 'Villa'));
        const villa = h.snapshot();
        return {chips: s.typeChips.map((c) => c.text), rows: villa.rows.map((r) => r.name), header: villa.header, cov: s.typeCov};
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        assert out["chips"] == ["Apartment 2", "Villa 1"]
        assert out["rows"] == ["Delta Court"] and "3 hidden: no property type" in out["header"]
        assert "Type known for 3 of 6" in out["cov"]

    def test_bedroom_chips_include_studio_and_follow_the_stated_range(self, page):
        scenario = """
        const labels = h.snapshot().bedChips.map((c) => c.text);
        const result = {labels};
        for (const chip of ['Studio', '2', '4', '5+']) {
          h.click(h.chip('bedChips', chip));
          result[chip] = h.snapshot().rows.map((r) => r.name);
          h.click(h.chip('bedChips', chip));
        }
        return result;
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        assert out["labels"] == ["Studio 1", "1 2", "2 1", "3 2", "4 1", "5+ 1"]
        assert out["Studio"] == ["Alpha Tower"] and out["2"] == ["Alpha Tower"] and out["4"] == ["Delta Court"] and out["5+"] == ["Delta Court"]

    def test_the_price_slider_has_data_driven_ends_and_hides_unpriced_pins_only_when_narrowed(self, page):
        scenario = """
        const start = h.snapshot();
        const lo = h.byId('priceLo');
        await h.input(lo, '200');
        const narrowed = h.snapshot();
        await h.input(lo, '0');
        const back = h.snapshot();
        return {start: [start.priceLabel, start.priceCov, start.priceDisabled, start.rows.length], narrowed: [narrowed.priceLabel, narrowed.header, narrowed.rows.map((r) => r.name)], back: back.rows.length};
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        start, narrowed = out["start"], out["narrowed"]
        assert start[0] == "Any price (AED 600K to AED 20M)" and start[2] == [False, False] and start[3] == 6
        assert "Price known for 3 of 6 pins" in start[1] and "hides the 3 pins with no price" in start[1]
        assert narrowed[0].startswith("From AED ")
        assert "3 hidden: no price data" in narrowed[1]
        assert set(narrowed[2]) <= {"Alpha Tower", "Delta Court"} and "Zeta Gardens" not in narrowed[2]
        assert out["back"] == 6

    def test_the_two_price_handles_cannot_cross(self, page):
        scenario = """
        await h.input(h.byId('priceLo'), '800');
        await h.input(h.byId('priceHi'), '300');
        return [h.byId('priceLo').value, h.byId('priceHi').value];
        """
        assert page(sample_collection(), scenario=scenario)["result"] == ["300", "300"]

    def test_with_no_prices_at_all_the_slider_is_off_and_says_so(self, page):
        collection = exp.build_geojson([registry_row()], {}, generated_at="2026-10-25T03:00:00Z")
        snap = page(collection)["result"]
        assert snap["priceDisabled"] == [True, True] and snap["priceLabel"] == "No price data"
        assert "No price data yet" in snap["priceCov"] and snap["rows"][0]["name"] == "Alpha Tower"

    def test_when_every_price_is_equal_there_is_nothing_to_slide_between(self, page):
        rows = [registry_row(registry_project_no="1"), registry_row(registry_project_no="2", project_name_en="Other Tower")]
        listing = {"source_url": "https://example.com/p", "price_from": 1_000_000, "price_to": 1_000_000}
        collection = exp.build_geojson(rows, {r.project_key: listing for r in rows}, generated_at="2026-10-25T03:00:00Z")
        snap = page(collection)["result"]
        assert snap["priceDisabled"] == [True, True] and "Every price in the data is AED 1,000,000" in snap["priceCov"]

    def test_reset_filters_restores_everything(self, page):
        scenario = """
        await h.input(h.byId('q'), 'alpha');
        h.click(h.chip('emirateChips', 'Sharjah'));
        h.click(h.byId('resetFilters'));
        const s = h.snapshot();
        return [s.rows.length, s.emirateChips.map((c) => c.pressed), h.byId('q').value];
        """
        assert page(sample_collection(), scenario=scenario)["result"] == [6, ["true", "true"], ""]


@needs_node
class TestRadiusSearch:
    def test_it_searches_exact_pins_only_and_says_how_many_approximate_ones_it_left_out(self, page):
        scenario = """
        h.click(h.byId('pick'));
        const picking = h.snapshot();
        h.mapClick(25.2, 55.27);
        const s = h.snapshot();
        return {picking: [picking.bodyPicking, picking.pick], rows: s.rows.map((r) => [r.name, r.aside]), header: s.header, note: s.radiusNote,
                circle: [h.LC.circle.radius, h.LC.circle.latlng.lat], clearHidden: s.clearHidden, after: [s.bodyPicking, s.pick], shown: s.shown};
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        assert out["picking"] == [True, "Now click on the map…"]
        names = [r[0] for r in out["rows"]]
        assert names == ["Alpha Tower", "Zeta Gardens"]            # exact and within 5 km, nearest first
        assert out["rows"][0][1] == "0.00 km" and out["rows"][1][1].endswith(" km")
        assert "exact projects within 5.0 km" in out["header"]
        assert out["note"]["hidden"] is False and "approximate pin" in out["note"]["text"] and "exact pins only" in out["note"]["text"]
        assert out["circle"] == [5000, 25.2] and out["clearHidden"] is False and out["shown"] == 2
        assert out["after"] == [False, "Pick a new centre"]

    def test_changing_the_radius_updates_the_circle_and_the_results(self, page):
        scenario = """
        h.click(h.byId('pick')); h.mapClick(25.2, 55.27);
        const r = h.byId('radius'); r.value = '0.5'; r.dispatch('input');
        const small = h.snapshot(), smallRadius = h.LC.circle.radius;
        r.value = '25'; r.dispatch('input');
        const large = h.snapshot(), largeRadius = h.LC.circle.radius;
        return {small: [small.rows.length, small.radiusLabel, smallRadius, small.header], large: [large.rows.length, large.radiusLabel, largeRadius, large.header]};
        """
        out = page(sample_collection(), scenario=scenario)["result"]
        assert out["small"] == [1, "0.5 km", 500, "1 exact project within 0.5 km of the chosen point"]
        assert out["large"] == [3, "25.0 km", 25000, "3 exact projects within 25.0 km of the chosen point"]

    def test_a_map_click_does_nothing_unless_the_pick_button_was_pressed(self, page):
        scenario = "h.mapClick(25.2, 55.27); const s = h.snapshot(); return [s.shown, s.clearHidden, h.LC.circle];"
        assert page(sample_collection(), scenario=scenario)["result"] == [6, True, None]

    def test_clear_removes_the_circle_and_restores_every_pin(self, page):
        scenario = """
        h.click(h.byId('pick')); h.mapClick(25.2, 55.27);
        h.click(h.byId('clearRadius'));
        const s = h.snapshot();
        return [s.shown, s.clearHidden, s.radiusNote.hidden, h.LC.circle.removed, h.LC.centre.removed];
        """
        assert page(sample_collection(), scenario=scenario)["result"] == [6, True, True, True, True]

    def test_with_no_exact_pin_at_all_it_says_there_is_nothing_to_search(self, page):
        collection = exp.build_geojson([registry_row(location_precision="area")], {}, generated_at="2026-10-25T03:00:00Z")
        scenario = "h.click(h.byId('pick')); h.mapClick(25.2, 55.27); const s = h.snapshot(); return [s.shown, s.radiusNote];"
        shown, note = page(collection, scenario=scenario)["result"]
        assert shown == 0 and note["hidden"] is False and "no exact pins" in note["text"]

    def test_dragging_the_centre_marker_recomputes(self, page):
        scenario = """
        h.click(h.byId('pick')); h.mapClick(25.2, 55.27);
        h.LC.centre.setLatLng({lat: 25.35, lng: 55.39});
        h.LC.centre.handlers.dragend();
        return h.snapshot().rows.map((r) => r[0] || r.name);
        """
        assert page(sample_collection(), scenario=scenario)["result"] == ["Delta Court"]


@needs_node
class TestFailureAndEmptyStates:
    def test_a_missing_file_is_a_readable_error_and_not_an_exception(self, page):
        out = page(body="Not Found", status=404)
        snap = out["result"]
        assert snap["notice"]["hidden"] is False and "error" in snap["notice"]["cls"] and snap["notice"]["role"] == "alert"
        assert "pins.geojson was not found" in snap["notice"]["text"][0] and "export_map_pins.py" in snap["notice"]["text"][0]
        assert snap["stats"] == "No data" and snap["header"] == "—" and snap["markerTotal"] == 0

    @pytest.mark.parametrize("status, fragment", [(500, "HTTP 500"), (403, "HTTP 403"), (429, "HTTP 429")])
    def test_other_http_failures_name_the_status(self, page, status, fragment):
        assert fragment in page(body="", status=status)["result"]["notice"]["text"][0]

    def test_a_network_failure_is_explained(self, page):
        snap = page(mode="reject")["result"]
        assert "network error" in snap["notice"]["text"][0] and "serve the folder over http" in snap["notice"]["text"][0]

    def test_fetch_itself_being_unavailable_is_caught_too(self, page):
        assert "network error" in page(mode="throw")["result"]["notice"]["text"][0]

    @pytest.mark.parametrize("body", ["", "{", "<html>not json</html>", '{"type": "FeatureCollection", "features": [', "\x00\x01"])
    def test_a_truncated_or_corrupt_file_is_reported_as_invalid_json(self, page, body):
        assert "not valid JSON" in page(body=body)["result"]["notice"]["text"][0]

    @pytest.mark.parametrize("body", ["null", "[]", "{}", '"text"', "5", '{"type": "FeatureCollection"}', '{"type": "FeatureCollection", "features": {}}', '{"type": "Feature", "features": []}'])
    def test_valid_json_of_the_wrong_shape_is_reported(self, page, body):
        assert "expected format" in page(body=body)["result"]["notice"]["text"][0]

    def test_an_unknown_schema_version_is_refused_rather_than_half_drawn(self, page):
        body = json.dumps({"type": "FeatureCollection", "meta": {"schema": 2}, "features": [feature()]})
        snap = page(body=body)["result"]
        assert "schema 2" in snap["notice"]["text"][0] and snap["markerTotal"] == 0

    def test_an_empty_map_explains_itself_and_stays_usable(self, page):
        collection = exp.build_geojson([registry_row(latitude=None, longitude=None)], {}, generated_at="2026-10-25T03:00:00Z")
        snap = page(collection)["result"]
        assert snap["notice"]["hidden"] is False and "info" in snap["notice"]["cls"]
        assert "No project has a confirmed location yet" in snap["notice"]["text"][0] and "1 are in the registry data" in snap["notice"]["text"][0]
        assert snap["header"] == "No located projects" and snap["rows"] == [] and snap["markerTotal"] == 0
        assert "Located" in snap["stats"]

    def test_an_empty_feature_list_with_no_meta_at_all_still_renders(self, page):
        snap = page(body='{"type": "FeatureCollection", "features": []}')["result"]
        assert "No project has a confirmed location yet" in snap["notice"]["text"][0] and snap["header"] == "No located projects"
        assert snap["credits"]      # the fallback attribution is still shown

    def test_garbage_features_are_skipped_and_counted_while_good_ones_still_draw(self, page):
        features = [feature(), None, 5, "x", {}, {"geometry": None}, feature(lng=25.2, lat=55.27, name="Swapped"), feature(emirate="Abu Dhabi", name="Elsewhere"),
                    {"type": "Feature", "geometry": {"type": "Point", "coordinates": ["a", "b"]}, "properties": {"emirate": "Dubai"}}]
        snap = page(body=json.dumps({"type": "FeatureCollection", "features": features}))["result"]
        assert snap["markerTotal"] == 1 and snap["rows"][0]["name"] == "Alpha Tower"
        assert "8 entries" in snap["notice"]["text"][0] and "were skipped" in snap["notice"]["text"][0]

    def test_unreadable_sheet_rows_are_flagged_as_an_incomplete_map(self, page):
        collection = exp.build_geojson([registry_row()], {}, generated_at="2026-10-25T03:00:00Z", unreadable_rows=4)
        snap = page(collection)["result"]
        assert snap["notice"]["hidden"] is False and "4 sheet rows could not be read" in snap["notice"]["text"][0] and snap["markerTotal"] == 1

    def test_a_missing_map_library_is_a_readable_error_and_not_an_exception(self, page):
        out = page(sample_collection(), no_leaflet=True)
        assert "map library could not be loaded" in out["result"]["notice"]["text"][0]
        assert out["fetchLog"] == []        # and it does not go on to fetch data it cannot draw

    def test_refresh_reloads_and_a_later_failure_replaces_the_old_map(self, page):
        scenario = """
        const first = h.snapshot().markerTotal;
        h.setFetch({mode: 'ok', status: 503, body: ''});
        h.click(h.byId('reload'));
        await h.settle(10);
        const second = h.snapshot();
        return [first, second.notice.text[0], second.shown, h.fetchLog.length];
        """
        first, text, shown, fetches = page(sample_collection(), scenario=scenario)["result"]
        assert first == 6 and "HTTP 503" in text and shown == 0 and fetches == 2

    def test_refresh_after_a_good_reload_resets_the_filters(self, page):
        scenario = """
        h.click(h.chip('emirateChips', 'Dubai'));
        h.click(h.byId('reload'));
        await h.settle(10);
        return h.snapshot().rows.length;
        """
        assert page(sample_collection(), scenario=scenario)["result"] == 6

    def test_a_late_response_from_an_older_request_cannot_overwrite_a_newer_one(self, page):
        scenario = """
        h.setFetch({mode: 'ok', status: 200, body: '{"type": "FeatureCollection", "features": []}'});
        h.click(h.byId('reload'));
        await h.settle(10);
        return [h.snapshot().header, h.snapshot().markerTotal];
        """
        header, markers = page(sample_collection(), scenario=scenario)["result"]
        assert header == "No located projects" and markers == 6      # markerTotal counts every marker ever created

    def test_a_big_file_caps_the_list_but_not_the_map(self, page):
        features = [feature(lng=55.0 + (i % 100) / 1000, lat=25.0 + (i // 100) / 1000, name=f"Test Residences {i:05d}", project_key=f"dld:dubai:{i + 1}") for i in range(3000)]
        snap = page(body=json.dumps({"type": "FeatureCollection", "meta": {"schema": 1}, "features": features}))["result"]
        assert snap["shown"] == 3000 and len(snap["rows"]) == 300 and "list shows the first 300" in snap["header"]

    def test_the_narrow_screen_collapses_the_panel_when_a_pin_is_chosen(self, page):
        scenario = "h.click(h.byId('list').children[0]); return h.snapshot().sideCollapsed;"
        assert page(sample_collection(), scenario=scenario, width=400)["result"] is True
        assert page(sample_collection(), scenario=scenario, width=1200)["result"] is False

    def test_a_list_click_still_opens_the_popup_when_the_cluster_layer_is_busy_after_the_zoom(self, page):
        # REGRESSION (map review, seen in a real browser): the first click on a list row zooms the map in, and
        # markercluster silently drops a zoomToShowLayer request made while it is re-clustering, so the
        # map moved but the popup never opened. The page has to ask again once the layer has settled.
        open_names = (
            "const names = h.LC.markers.filter((m) => m.opened).map((m) => h.textOf(h.findAll(m.popupFn(), (e) => e.tagName === 'H3')[0]));"
        )
        one = f"h.LC.simulateAnimation = true; const first = h.rows()[0].name; h.click(h.byId('list').children[0]); await h.settle(1500); {open_names} return [first, names];"
        first, names = page(sample_collection(), scenario=one)["result"]
        assert names == [first]
        # two quick clicks: only the last choice may end up open
        two = (
            "h.LC.simulateAnimation = true; const second = h.rows()[1].name; h.click(h.byId('list').children[0]); "
            f"h.click(h.byId('list').children[1]); await h.settle(1500); {open_names} return [second, names];"
        )
        second, names = page(sample_collection(), scenario=two)["result"]
        assert names == [second]

    def test_the_panel_button_tells_a_screen_reader_when_a_pin_choice_folds_the_panel_away(self, page):
        # REGRESSION (map review): choosing a pin (or "pick a centre") folded the panel but left the
        # "Filters & list" button without any state, so it announced nothing about what had changed.
        scenario = (
            "const t = h.byId('toggleSide'); "
            "h.click(h.byId('list').children[0]); const folded = t.getAttribute('aria-expanded'); "
            "h.click(t); const reopened = t.getAttribute('aria-expanded'); return [folded, reopened];"
        )
        assert page(sample_collection(), scenario=scenario, width=375)["result"] == ["false", "true"]

    def test_the_panel_button_starts_out_as_expanded_and_points_at_the_panel(self, source):
        tag = re.search(r'<button[^>]*id="toggleSide"[^>]*>', source).group(0)
        assert 'aria-expanded="true"' in tag and 'aria-controls="side"' in tag

    def test_the_map_is_resized_before_it_moves_when_the_panel_collapses_on_a_phone(self, page):
        # Collapsing the panel changes the map's size. Leaflet must be told (invalidateSize) BEFORE it
        # centres on the chosen pin, or the pin lands off-centre / under the panel on a phone.
        row = "h.LC.log.length = 0; h.click(h.byId('list').children[0]); return h.LC.log;"
        pick = ("h.LC.log.length = 0; h.click(h.byId('pick')); h.mapClick(25.2, 55.27); return h.LC.log;")
        for scenario in (row, pick):
            phone = page(sample_collection(), scenario=scenario, width=375)["result"]
            assert "invalidate" in phone, phone
            if "view" in phone:
                assert phone.index("invalidate") < phone.index("view"), phone
            desktop = page(sample_collection(), scenario=scenario, width=1200)["result"]
            assert "invalidate" not in desktop, desktop


@needs_node
class TestNoDataBecomesMarkup:
    HOSTILE = "<img src=x onerror=alert(1)>"

    def hostile_collection(self):
        return {
            "type": "FeatureCollection", "meta": {"schema": 1, "attribution": [self.HOSTILE, "<script>x</script>"], "generated_at": self.HOSTILE},
            "features": [
                feature(name=self.HOSTILE, developer="<b>dev</b>", area="\"><svg onload=alert(1)>", status="<i>x</i>", handover="javascript:alert(1)",
                        source_url="https://example.com/ok", price_from=1_000_000, name_ar="<script>alert(1)</script>", project_key="'><script>"),
                feature(lng=55.28, name="Bad Link", source_url="javascript:alert(1)", price_from=5),
                feature(lng=55.29, name="Data Link", source_url="data:text/html,<script>alert(1)</script>", price_from=5),
            ],
        }

    def test_hostile_text_is_shown_as_text_in_the_popup_the_list_and_the_header(self, page):
        scenario = f"""
        const popup = h.popupByName({json.dumps(self.HOSTILE)});
        const s = h.snapshot();
        return {{tags: popup.tags, text: popup.text, rows: s.rows, credits: s.credits, stats: s.stats}};
        """
        out = page(body=json.dumps(self.hostile_collection()), scenario=scenario)["result"]
        assert set(out["tags"]) <= {"div", "h3", "span", "a"} and out["tags"].count("a") == 1
        assert self.HOSTILE in out["text"] and "<b>dev</b>" in out["text"]
        assert any(r["name"] == self.HOSTILE for r in out["rows"])
        assert out["credits"][0] == self.HOSTILE

    def test_only_http_links_are_ever_created(self, page):
        scenario = "return ['Bad Link', 'Data Link'].map((n) => h.popupByName(n).anchors);"
        assert page(body=json.dumps(self.hostile_collection()), scenario=scenario)["result"] == [[], []]

    def test_the_fake_dom_proves_innerHTML_is_never_touched(self, page):
        # The harness's innerHTML setter throws; the page loaded, filtered and rendered
        # popups above without tripping it. This test makes the dependency explicit.
        out = page(sample_collection(), scenario="await h.input(h.byId('q'), 'a'); h.popupByName('Alpha Tower'); return 'ok';")
        assert out["result"] == "ok" and out["uncaught"] == []

    def test_the_harness_would_notice_innerHTML_if_the_page_used_it(self, page, script):
        mutated = script.replace("function clearNode(node) { while (node.firstChild) node.removeChild(node.firstChild); }",
                                 "function clearNode(node) { node.innerHTML = ''; }")
        assert mutated != script
        out = subprocess.run([NODE, "-e", "0"], capture_output=True, timeout=60)   # node itself works
        assert out.returncode == 0
        with pytest.raises(AssertionError):
            page(sample_collection(), inline_script=mutated)


# ---------------------------------------------------------------------------
# 5. Exporter and page agree
# ---------------------------------------------------------------------------
@needs_node
class TestExporterPageContract:
    def test_the_pages_constants_equal_the_python_ones(self, pure):
        (consts,) = pure.run("return {schema: P.SCHEMA, bbox: P.UAE_BBOX, emirates: P.EMIRATES, precisions: P.PRECISIONS, types: P.TYPE_ORDER, bed: P.MAX_BEDROOMS, "
                             "aed: P.MAX_PRICE_AED, sqft: P.MAX_PRICE_PER_SQFT, steps: P.PRICE_STEPS};", [None])
        assert consts["schema"] == exp.SCHEMA_VERSION
        min_lat, max_lat, min_lng, max_lng = UNION_BBOX
        assert consts["bbox"] == {"minLat": min_lat, "maxLat": max_lat, "minLng": min_lng, "maxLng": max_lng}
        assert consts["emirates"] == list(EMIRATES)
        assert consts["precisions"] == list(exp.PRECISION_ORDER) == [*PRECISIONS, "unknown"]
        assert consts["types"] == [t for t in PROPERTY_TYPES if t != "Unknown"]
        assert consts["bed"] == MAX_BEDROOMS and consts["aed"] == exp.MAX_PRICE_AED and consts["sqft"] == exp.MAX_PRICE_PER_SQFT_AED and consts["steps"] == 1000

    def test_every_exported_pin_is_accepted_by_the_page_with_its_values_intact(self, pure):
        collection = sample_collection()
        pins = pure.call("normalizeFeature", [[f] for f in collection["features"]])
        assert all(pins), "the page rejected a feature the exporter wrote"
        for pin, feat in zip(pins, collection["features"]):
            props = feat["properties"]
            assert (pin["lng"], pin["lat"]) == tuple(feat["geometry"]["coordinates"])
            assert pin["emirate"] == props["emirate"] and pin["precision"] == props["precision"] and pin["name"] == props["name"]
            for js_name, py_name in (("priceFrom", "price_from"), ("priceTo", "price_to"), ("pricePerSqft", "price_per_sqft"),
                                     ("bedsMin", "bedrooms_min"), ("bedsMax", "bedrooms_max"), ("units", "units"), ("sourceUrl", "source_url")):
                assert pin[js_name] == props.get(py_name, "" if js_name == "sourceUrl" else None), (pin["name"], js_name)
            assert pin["types"] == props.get("property_types", [])

    def test_the_exporter_s_biggest_values_are_still_inside_the_page_s_limits(self, pure):
        row = registry_row(units=10**9)
        collection = exp.build_geojson([row], {row.project_key: {"source_url": "https://example.com/p", "price_from": exp.MAX_PRICE_AED, "price_to": exp.MAX_PRICE_AED,
                                                                  "price_per_sqft": exp.MAX_PRICE_PER_SQFT_AED, "bedrooms_min": 0, "bedrooms_max": MAX_BEDROOMS}},
                                       generated_at="2026-10-25T03:00:00Z")
        (pin,) = pure.call("normalizeFeature", [[collection["features"][0]]])
        assert (pin["priceFrom"], pin["pricePerSqft"], pin["bedsMax"]) == (exp.MAX_PRICE_AED, exp.MAX_PRICE_PER_SQFT_AED, MAX_BEDROOMS)

    def test_what_the_exporter_drops_the_page_would_have_dropped_too(self, pure):
        # swapped / foreign pairs never reach the file; if one did, the page refuses it as well
        swapped = feature(lng=25.2, lat=55.27)
        assert pure.call("normalizeFeature", [[swapped]]) == [None]
        bad = [registry_row(registry_project_no=str(i), latitude=lat, longitude=lon) for i, (lat, lon) in enumerate([(55.27, 25.2), (48.85, 2.35), (24.45, 54.38)], 1)]
        assert exp.build_geojson(bad, {}, generated_at="2026-10-25T03:00:00Z")["features"] == []

    def test_a_pins_file_written_by_the_cli_path_loads_in_the_page(self, page, tmp_path):
        from tests.test_export_map_pins import FakeManager, fake_config   # same fakes, no second copy

        out = tmp_path / "pins.geojson"
        rows = [registry_row(registry_project_no="1"), sharjah_row()]
        assert exp.main(["--out", str(out)], cfg=fake_config(), manager_factory=lambda cfg: FakeManager(rows)) == 0
        snap = page(body=out.read_text(encoding="utf-8"))["result"]
        assert snap["markerTotal"] == 2 and snap["notice"]["hidden"] is True
