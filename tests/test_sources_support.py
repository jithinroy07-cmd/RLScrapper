"""
Shared fakes and helpers for the source adapter tests (no network, no credentials).

Everything here is SYNTHETIC: invented names, obviously fake secrets. The fakes
are strict on purpose. A FakeSession or FakeGeminiClient whose script is used up
raises AssertionError on the next call, so a test fails if the adapter makes a
request the test did not expect (an unwanted retry, an extra page).

Sibling test modules import this one by bare name (tests/ has no __init__.py).
"""

from __future__ import annotations

import json
import traceback
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Optional

import pytest

from src.config import Config, load_config

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"
TODAY = date(2026, 10, 25)
TODAY_ISO = TODAY.isoformat()

# Obviously fake. They are long enough to be recognisable in an output and unlike
# anything a real service issues.
FAKE_KEY = "fake-key-0123456789"
FAKE_SECRET = "fake-secret-9876543210"
FAKE_TOKEN = "fake-token-abcdefghij"
ALL_FAKE_SECRETS = (FAKE_KEY, FAKE_SECRET, FAKE_TOKEN)

_UNSET = object()


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_cfg(**env: str) -> Config:
    """A Config built from exactly this mapping (never the real environment)."""
    return load_config(env=env)


def dld_cfg(**extra: str) -> Config:
    return make_cfg(DATA_DUBAI_API_KEY=FAKE_KEY, DATA_DUBAI_API_SECRET=FAKE_SECRET, **extra)


def gemini_cfg(**extra: str) -> Config:
    env = {"ENABLE_GEMINI_NEWS": "true", "GEMINI_API_KEY": FAKE_KEY, "GEMINI_MODEL": "fake-model-1"}
    env.update(extra)
    return make_cfg(**env)


def full_text(exc: BaseException) -> str:
    """Everything a traceback would print for `exc`, including chained exceptions."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def assert_no_secret(text: str, *secrets: str) -> None:
    for secret in secrets or ALL_FAKE_SECRETS:
        assert secret not in text, f"a secret leaked into: {text[:300]!r}"


class FakeResponse:
    """Enough of requests.Response for the adapters: status_code, text, headers, json()."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = _UNSET,
        *,
        text: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._text = text
        self.headers = headers or {}
        self._json_error = json_error

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        return "" if self._payload is _UNSET else json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("not JSON")
        if self._payload is not _UNSET:
            return self._payload
        return json.loads(self.text)  # ValueError for non-JSON text, like requests


class FakeSession:
    """
    A scripted requests.Session. Each item of `script` answers one call, in order:
    a FakeResponse (returned), an exception instance (raised) or a callable
    (method, url, kwargs) -> response.
    """

    def __init__(self, script: Iterable[Any]) -> None:
        self.script = list(script)
        self.calls: list[SimpleNamespace] = []
        self.closed = False

    def _next(self, method: str, url: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append(SimpleNamespace(method=method, url=url, kwargs=kwargs))
        if not self.script:
            raise AssertionError(f"unexpected {method} {url}: the script is used up")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(method, url, kwargs)
        return item

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._next("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._next("POST", url, kwargs)

    def close(self) -> None:
        self.closed = True


class Sleeps(list):
    """An injectable sleep that records instead of waiting."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def token_ok(token: str = FAKE_TOKEN) -> FakeResponse:
    return FakeResponse(200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600})


def page(rows: list[dict[str, Any]], **envelope: Any) -> FakeResponse:
    """A 200 answer whose rows sit under "data", with optional envelope keys (total, last_updated)."""
    return FakeResponse(200, {"data": rows, **envelope})


# -- Gemini fakes ---------------------------------------------------------------
def grounding_response(
    text: Optional[str],
    *,
    chunks: Iterable[tuple[Optional[str], Optional[str], Optional[str]]] = (),
    queries: Iterable[str] = ("test query",),
    finish: str = "STOP",
    grounded: bool = True,
) -> SimpleNamespace:
    """
    A stand-in for the SDK's response. `chunks` are (uri, title, domain) triples of
    grounding sources; grounded=False leaves the grounding metadata off entirely.
    """
    web_chunks = [
        SimpleNamespace(web=SimpleNamespace(uri=uri, title=title, domain=domain)) for uri, title, domain in chunks
    ]
    meta = SimpleNamespace(grounding_chunks=web_chunks, web_search_queries=list(queries)) if grounded else None
    candidate = SimpleNamespace(grounding_metadata=meta, finish_reason=SimpleNamespace(name=finish))
    return SimpleNamespace(text=text, candidates=[candidate], prompt_feedback=None)


class FakeGeminiClient:
    """A scripted stand-in for genai.Client: only client.models.generate_content(...) exists."""

    def __init__(self, script: Iterable[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("unexpected generate_content call: the script is used up")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# ---------------------------------------------------------------------------
# The fakes themselves are code, so they get a few tests
# ---------------------------------------------------------------------------
def test_fake_session_replays_in_order_and_records_calls():
    session = FakeSession([FakeResponse(200, {"a": 1}), FakeResponse(500, text="boom")])
    assert session.get("http://x/1", params={"p": 1}).json() == {"a": 1}
    assert session.post("http://x/2", json={}).status_code == 500
    assert [(c.method, c.url) for c in session.calls] == [("GET", "http://x/1"), ("POST", "http://x/2")]


def test_fake_session_fails_loudly_when_the_script_is_used_up():
    with pytest.raises(AssertionError, match="used up"):
        FakeSession([]).get("http://x")


def test_fake_session_raises_scripted_exceptions_and_calls_callables():
    session = FakeSession([ConnectionError("down"), lambda method, url, kwargs: FakeResponse(200, {"m": method})])
    with pytest.raises(ConnectionError):
        session.get("http://x")
    assert session.post("http://x").json() == {"m": "POST"}


def test_fake_response_json_follows_requests_for_bad_bodies():
    assert FakeResponse(200, text='{"a": 1}').json() == {"a": 1}
    with pytest.raises(ValueError):
        FakeResponse(200, text="<html>").json()
    with pytest.raises(ValueError):
        FakeResponse(200, {"a": 1}, json_error=True).json()


def test_fake_gemini_client_is_strict():
    client = FakeGeminiClient([grounding_response("[]")])
    assert client.models.generate_content(model="m", contents="c", config=None).text == "[]"
    with pytest.raises(AssertionError, match="used up"):
        client.models.generate_content(model="m", contents="c", config=None)
    assert client.calls[0]["model"] == "m"


def test_grounding_response_can_omit_grounding_entirely():
    assert grounding_response("x", grounded=False).candidates[0].grounding_metadata is None
    chunk = grounding_response("x", chunks=[("https://a.example/1", "a.example", None)]).candidates[0]
    assert chunk.grounding_metadata.grounding_chunks[0].web.title == "a.example"
