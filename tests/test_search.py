"""Search provider abstraction + inline web_search/fetch_page tool loop."""
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_ops import search
from ai_ops.app import create_app

ADMIN = "a" * 40


class FakeProvider:
    name = "fake"

    def __init__(self, results=None, error=None):
        self.results = results if results is not None else [
            {"title": "Docs", "url": "https://example.test/doc", "snippet": "hello"}]
        self.error = error
        self.queries = []

    def search(self, query, count):
        self.queries.append((query, count))
        if self.error:
            raise search.SearchFailure(self.error)
        return self.results[:count]


class FakeModel:
    """Returns queued responses; records each request body it receives."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, body):
        self.requests.append(json.loads(json.dumps(body)))
        if not self.responses:
            raise AssertionError("model called more times than scripted")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def tool_call(name, arguments, call_id="call_1"):
    return {"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        "usage": {}, "model": "test"}


def final(text):
    return {"message": {"role": "assistant", "content": text}, "usage": {}, "model": "test"}


def make_app(provider, tmp=None):
    path = str((Path(tmp) if tmp else Path(tempfile.mkdtemp())) / "control.db")
    return create_app(path, ADMIN, "test-model", search_provider=provider)


def enable_and_send(client, headers):
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    client.put("/api/v1/roles/ops/model", headers=headers,
               json={"enabled": True, "model": "", "max_model_steps": 6})
    r = client.post("/api/v1/roles/ops/messages", headers=headers,
                    json={"text": "what changed in nginx 1.27?", "execution_users": [],
                          "mode": "direct", "idempotency_key": "msg-00000001"})
    return r.json()["turn_id"]


def test_provider_from_env_prefers_searxng_then_brave(monkeypatch):
    assert search.provider_from_env({}) is None
    p = search.provider_from_env({"AI_OPS_SEARXNG_URL": "https://sx.example"})
    assert isinstance(p, search.SearxngProvider)
    p = search.provider_from_env({"AI_OPS_BRAVE_KEY_FILE": "/run/secrets/brave"})
    assert isinstance(p, search.BraveProvider)


def test_search_rejects_insecure_base_url():
    with pytest.raises(search.SearchFailure):
        search.SearxngProvider("ftp://x")
    with pytest.raises(search.SearchFailure):
        search.SearxngProvider("https://user:pw@x.example")


def test_web_search_tool_runs_inline_without_assets():
    provider = FakeProvider()
    app = make_app(provider)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + ADMIN}
    turn_id = enable_and_send(client, headers)

    model = FakeModel([tool_call("web_search", {"query": "nginx 1.27"}), final("done")])
    engine = app.state.role_engine
    assert engine.advance(model) is True  # step 1 -> tool call
    assert provider.queries == [("nginx 1.27", 5)]
    # The tool result was appended and the turn is ready for the next step.
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "ready"
    tool_msg = [m for m in turn["messages"] if m.get("role") == "tool"][-1]
    assert "example.test" in tool_msg["content"]

    assert engine.advance(model) is True  # step 2 -> final
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "completed"
    assert turn["final_text"] == "done"


def test_web_search_tool_spec_exposed_only_when_provider_configured():
    from ai_ops.turns import tool_spec
    names = [t["function"]["name"] for t in tool_spec([], FakeProvider())]
    assert names == ["web_search", "fetch_page"]
    assert tool_spec([], None) == []
    # With an account selected, the execution tool comes first, then search.
    with_user = [t["function"]["name"] for t in tool_spec(["ops_read"], FakeProvider())]
    assert with_user == ["execute_command", "web_search", "fetch_page"]


def test_search_failure_is_returned_as_tool_error_not_a_crash():
    provider = FakeProvider(error="SEARCH_HTTP_429")
    app = make_app(provider)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + ADMIN}
    turn_id = enable_and_send(client, headers)
    model = FakeModel([tool_call("web_search", {"query": "x"}), final("ok")])
    engine = app.state.role_engine
    engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    tool_msg = [m for m in turn["messages"] if m.get("role") == "tool"][-1]
    assert "SEARCH_HTTP_429" in tool_msg["content"]
    assert turn["state"] == "ready"


def test_fetch_page_extracts_text(monkeypatch):
    html = b"<html><head><style>x{}</style></head><body><h1>Title</h1><p>Body text</p><script>bad()</script></body></html>"

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return html

    class Opener:
        def open(self, *a, **k): return Resp()

    monkeypatch.setattr(search, "_opener", lambda: Opener())
    text = search.fetch_page("https://example.test/")
    assert "Title" in text and "Body text" in text
    assert "bad()" not in text and "x{}" not in text


def test_fetch_page_rejects_non_http():
    with pytest.raises(search.SearchFailure):
        search.fetch_page("file:///etc/passwd")
