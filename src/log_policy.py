"""
What may reach the log of this PUBLIC repository (docs/DESIGN.md section 8), in one place so
the daily run (src/main.py) and the map exporter (scripts/export_map_pins.py) keep the same
rules.

    * The handler lets through ONLY records from this project's own loggers (OWN_LOGGERS).
      Every other logger is dropped at every level, with its traceback text: urllib3 logs
      "Failed to parse headers" with the request URL (a search query that holds a project
      name) and the raw header bytes, google-auth's background lookup logs retries with the
      service-account e-mail in the URL, an SDK can repeat a response body. A list of
      library names would lag behind the next dependency; a policy about WHO is speaking
      does not. The root logger sits above CRITICAL, so a foreign record is not even
      created (a handler that sees nobody else's records cannot be bypassed by a handler
      somebody else adds, and test fixtures cannot capture them either).
    * Python warnings are routed into logging (logging.captureWarnings) so they meet the
      same rule instead of going straight to stderr: google-genai warns with the value
      the server sent. Nothing in this project calls warnings.warn.
    * Whatever an own record says is scrubbed once more on the way out (SecretFilter): the
      known secret values and credential-shaped text. The code never relies on it.
    * secret_values() lists what must never appear: every hidden Config field, the secret
      members of a JSON-object secret, and the identifiers a service-account key carries
      (its e-mail, the parts of it, the project id, the key id, the client id).
    * An exception nobody catches (a cancelled run raises KeyboardInterrupt) is printed by the
      interpreter's default hook with its whole chain, message text included, and a keyed URL
      can ride along in a chained message. install_excepthook() replaces the hook with one that
      prints the class name and the place only, `ClassName (at file:line in function)`; the two
      entry points call it from main(), never at import time.

Importing this module has no side effects: no logging configuration, no change to
sys.excepthook, and no import of the sheet code (the exporter must not load gspread just to
configure its log).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, Optional

# Not __name__: under `python -m src.main` that is "__main__", outside the "src" tree.
OWN_LOGGERS = ("src", "export_map_pins")
LOG_LINE_LIMIT = 300
LOG_HANDLER_MARK = "_uae_scraper_handler"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FOREIGN_SILENCE_LEVEL = logging.CRITICAL + 1  # above every record: a logger with no level of its own says nothing

_SECRET_MEMBER_NAMES = ("key", "token", "secret", "password", "private", "credential")
# Members that are not secret by name but identify the account: the key file's owner.
_IDENTIFIER_MEMBER_NAMES = ("client_email", "client_id", "project_id")
_MIN_PEM_LINE = 20  # a PEM body is 64-character lines; shorter lines are not worth hunting for
# Google's own minimum for a project id and for the account name before the @. A shorter
# value is not an identifier and, replaced in a log line, would mangle ordinary words.
_MIN_IDENTIFIER_LENGTH = 6


def _identifier_forms(name: str, member: str) -> list[str]:
    """An identifier member as the strings to scrub: itself, and for an e-mail its name and domain."""
    forms = [member]
    if name.lower() == "client_email" and "@" in member:
        local, _, domain = member.partition("@")
        forms.extend((local, domain))
    return [form for form in forms if len(form.strip()) >= _MIN_IDENTIFIER_LENGTH]


def json_secret_members(value: str) -> list[str]:
    """
    The secret-looking string members of a JSON-object secret (a service-account key), the
    long lines of a multi-line one (a PEM body) and the identifiers of the account. An auth
    library that fails can echo just the private key, or the account e-mail in a URL, which a
    whole-document match would miss.
    """
    text = value.strip()
    if not text.startswith("{"):
        return []
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # a deeply nested document is not a key file, and must not stop the run
        return []
    found: list[str] = []
    if isinstance(data, dict):
        for name, member in data.items():
            if not isinstance(name, str) or not isinstance(member, str):
                continue
            if any(w in name.lower() for w in _SECRET_MEMBER_NAMES):
                found.append(member)
                found.extend(
                    line for line in member.splitlines() if len(line) >= _MIN_PEM_LINE and not line.startswith("-----")
                )
            elif name.lower() in _IDENTIFIER_MEMBER_NAMES:
                found.extend(_identifier_forms(name, member))
    return found


def secret_values(cfg: Any) -> list[str]:
    """
    Everything that must never reach a log: every Config field that hides from repr()
    (keys, ids, the e-mail list), the secret members and account identifiers inside a JSON
    value, and the geocoding contact address (not a credential, but personal data in a
    public log).
    """
    values: list[str] = []
    for item in dataclasses.fields(cfg):
        if item.repr:
            continue
        value = getattr(cfg, item.name, "")
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, (tuple, list)):
            values.extend(v for v in value if isinstance(v, str))
    contact = getattr(cfg, "geocode_contact", "")
    if isinstance(contact, str):
        values.append(contact)
    values.extend(member for value in list(values) for member in json_secret_members(value))
    return [v for v in dict.fromkeys(v.strip() for v in values) if v]


def scrub_for_log(value: object, secrets: Iterable[str] = (), limit: int = LOG_LINE_LIMIT) -> str:
    """
    `value` as one short line that is safe to log: the known secret values and anything
    credential-shaped replaced (the sheet layer's rules, then the source layer's, which
    also collapses whitespace and truncates last so a cut cannot leave half a secret).
    """
    # lazy: the sheet layer imports gspread, which the exporter loads only when it opens the sheet
    from src.gsheet_manager import scrub_text as sheet_scrub  # noqa: PLC0415
    from src.sources.base import scrub_text as source_scrub  # noqa: PLC0415

    known = [s for s in secrets if isinstance(s, str) and s]
    return source_scrub(sheet_scrub(value, known), known, limit=limit)


class OwnLoggersOnly(logging.Filter):
    """The policy: a record passes only when this project's code wrote it, whatever its level."""

    def filter(self, record: logging.LogRecord) -> bool:
        return any(record.name == own or record.name.startswith(own + ".") for own in OWN_LOGGERS)


class SecretFilter(logging.Filter):
    """
    Last line of defence on the handler: whatever an own logger emits is scrubbed of known
    secrets and credential-shaped text, and an attached traceback is reduced to scrubbed
    text. The code never relies on it; it exists because a debug line can carry a URL with
    an API key.
    """

    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self.secrets: tuple[str, ...] = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must still be scrubbed
            message = str(record.msg)
        record.msg, record.args = scrub_for_log(message, self.secrets, 2000), None
        if record.exc_info:
            record.exc_text = scrub_for_log("".join(traceback.format_exception(*record.exc_info)), self.secrets, 4000)
            record.exc_info = None
        return True


def configure_logging(verbose: bool = False, secrets: Iterable[str] = ()) -> logging.Handler:
    """
    stderr handler with the policy above. Only the project's own loggers speak: they follow
    -v, and every other logger (with its debug lines, where request URLs live) is silent
    even at ERROR. Replaces a handler an earlier call installed, so calling it twice does
    not duplicate lines.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(OwnLoggersOnly())
    handler.addFilter(SecretFilter(secrets))
    setattr(handler, LOG_HANDLER_MARK, True)
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, LOG_HANDLER_MARK, False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(FOREIGN_SILENCE_LEVEL)
    for name in OWN_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.captureWarnings(False)  # a second call must install its own hook, not keep a stale one
    logging.captureWarnings(True)
    return handler


def set_secrets(handler: logging.Handler, secrets: Iterable[str]) -> None:
    """Give the handler's SecretFilter the secrets of the configuration, once it is loaded."""
    for flt in handler.filters:
        if isinstance(flt, SecretFilter):
            flt.secrets = tuple(secrets)


# ---------------------------------------------------------------------------
# An exception nobody catches
# ---------------------------------------------------------------------------
def traceback_location(tb: Optional[TracebackType]) -> str:
    """file:line in function of the innermost frame: where it happened, never any variable value."""
    frames = traceback.extract_tb(tb)
    if not frames:
        return "unknown"
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"


def error_location(exc: BaseException) -> str:
    """The place an exception was raised, for a log line (see traceback_location)."""
    return traceback_location(exc.__traceback__)


def excepthook(
    exc_type: type[BaseException], exc: Optional[BaseException], tb: Optional[TracebackType]
) -> None:
    """
    sys.excepthook for the two entry points: `ClassName (at file:line in function)` on stderr and
    nothing else. The default hook prints the traceback and the whole __cause__ / __context__
    chain with every message, and a message can carry a URL with a key or a project name. Never
    raises: a hook that fails would print its own traceback.
    """
    try:
        name = getattr(exc_type, "__name__", "BaseException")
        try:
            where = traceback_location(tb)
        except Exception:  # noqa: BLE001 - something that is not a traceback still names its class
            where = "unknown"
        sys.stderr.write(f"{name} (at {where})\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - a closed or broken stderr must not turn one failure into two
        pass


def install_excepthook() -> None:
    """
    Make `excepthook` the interpreter's hook for an uncaught exception. Called from main() of
    `python -m src.main` and `python -m scripts.export_map_pins`, never at import time, so that
    importing this module (or running the tests) changes nothing. It sets sys.excepthook and
    nothing else: not the thread hook, not the unraisable hook, not the logging setup.
    """
    sys.excepthook = excepthook
