"""Tests for the streaming chat route (chat_stream.py): what the page receives, and what happens when a run goes wrong."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import chat_blueprint as cb
import chat_stream as cs


# ---- fakes ----

def ev(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def message_item():
    return {"type": "message"}


def done_response(text="The answer.", status="completed", code=None):
    return SimpleNamespace(
        status=status,
        output=[{"type": "message", "content": [{"type": "output_text", "text": text}]}] if text else [],
        output_text=text,
        error=SimpleNamespace(code=code, message="m") if code else None,
        incomplete_details=None,
    )


def completed(text="The answer."):
    return ev("response.completed", response=done_response(text))


def delta(text):
    return ev("response.output_text.delta", delta=text, item_id="m1")


def added(item):
    return ev("response.output_item.added", item=item)


class FakeStream:
    def __init__(self, events):
        self.events, self.closed = list(events), False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class RateLimitError(Exception):
    pass


class FakeClient:
    """Each call to responses.create returns the next queued stream (or raises it, if it is an exception)."""

    def __init__(self, *runs):
        self.queue, self.calls, self.last = list(runs), 0, None
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, input=None, stream=False):
        assert stream is True
        self.calls += 1
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        self.last = FakeStream(item)
        return self.last


class FakeRequest:
    def __init__(self, body=None, method="POST", origin="https://www.sujeeth.org"):
        self.method = method
        self.headers = {"origin": origin} if origin else {}
        self._body = body if body is not None else json.dumps(
            {"messages": [{"role": "user", "content": "What is a Career Circle?"}]}).encode()

    async def body(self):
        return self._body


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)
    monkeypatch.setattr(cb, "_cooldown_until", 0.0)
    monkeypatch.setattr(cb, "BASE_URL", "https://agent.example.test")
    monkeypatch.setattr(cb.limiter, "allow", lambda ip: (True, 0))


def use(monkeypatch, client):
    monkeypatch.setattr(cb, "get_client", lambda: client)
    return client


def stream_events(request=None):
    """Run the route and return (response, the events it sent)."""
    async def go():
        response = await cs.handle(request or FakeRequest())
        if not hasattr(response, "body_iterator"):
            return response, []
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            if text.startswith("data: "):
                events.append(json.loads(text[6:].strip()))
        return response, events
    return asyncio.run(go())


def types(events):
    return [e["type"] for e in events]


# ---- what the page receives ----

def test_text_arrives_in_pieces_then_the_final_answer(monkeypatch):
    use(monkeypatch, FakeClient([added(message_item()), delta("Career "), delta("Circles."), completed("Career Circles.")]))
    response, events = stream_events()
    assert response.media_type == "text/event-stream"
    assert types(events) == ["status", "delta", "delta", "done"]
    assert [e["text"] for e in events if e["type"] == "delta"] == ["Career ", "Circles."]
    assert events[-1]["reply"] == "Career Circles."
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "https://www.sujeeth.org"


def test_a_tool_call_shows_a_status_and_clears_text_spoken_before_it(monkeypatch):
    use(monkeypatch, FakeClient([
        added(message_item()), delta("Let me check."),
        added({"type": "mcp_call", "name": "search_sujeeth_foundation"}),
        added({"type": "mcp_call", "name": "get_documents"}),
        added(message_item()), delta("Week 3 is resumes."), completed("Week 3 is resumes."),
    ]))
    _, events = stream_events()
    assert types(events) == ["status", "delta", "reset", "status", "status", "delta", "done"]
    assert [e["text"] for e in events if e["type"] == "status"] == [
        "Thinking…", "Searching the Foundation…", "Reading a page…"]
    assert events[-1]["reply"] == "Week 3 is resumes."


def test_no_reset_when_a_tool_call_comes_before_any_text(monkeypatch):
    use(monkeypatch, FakeClient([added({"type": "mcp_call", "name": "read_query"}), delta("Hi."), completed("Hi.")]))
    _, events = stream_events()
    assert "reset" not in types(events)
    assert events[1]["text"] == "Checking the Foundation's records…"


def test_the_final_answer_is_the_last_message_even_if_no_pieces_came(monkeypatch):
    use(monkeypatch, FakeClient([completed("All at once.")]))
    _, events = stream_events()
    assert types(events) == ["status", "done"] and events[-1]["reply"] == "All at once."


def test_a_long_conversation_tells_the_page_to_fold(monkeypatch):
    use(monkeypatch, FakeClient([completed("Ok.")]))
    monkeypatch.setattr(cb, "plan_fold", lambda *args: 4)
    _, events = stream_events()
    assert events[-1]["fold"] == {"keep_last": 4}


def test_the_slot_is_free_again_after_a_stream(monkeypatch):
    use(monkeypatch, FakeClient([completed("Ok.")]))
    stream_events()
    assert cb._in_flight == 0


# ---- when a run goes wrong ----

def test_a_failed_run_ends_with_an_error_event(monkeypatch):
    use(monkeypatch, FakeClient([ev("response.failed", response=done_response("", "failed", "tool_user_error"))]))
    _, events = stream_events()
    assert types(events) == ["status", "error"]
    assert events[-1] == {"type": "error", "error": cb.FRIENDLY["unavailable"], "status": 502}


def test_an_empty_answer_is_an_error(monkeypatch):
    use(monkeypatch, FakeClient([completed("")]))
    _, events = stream_events()
    assert events[-1]["type"] == "error" and events[-1]["status"] == 502


def test_a_stream_that_ends_without_finishing_is_an_error(monkeypatch):
    use(monkeypatch, FakeClient([delta("Half an ans")]))
    _, events = stream_events()
    assert events[-1]["type"] == "error"


def test_an_error_event_from_the_service_is_reported(monkeypatch):
    use(monkeypatch, FakeClient([ev("error", code="server_error", message="boom")]))
    _, events = stream_events()
    assert events[-1] == {"type": "error", "error": cb.FRIENDLY["unavailable"], "status": 502}


def test_a_rate_limit_before_any_text_is_retried(monkeypatch):
    client = use(monkeypatch, FakeClient(RateLimitError("429"), [completed("Here you go.")]))
    _, events = stream_events()
    assert client.calls == 2 and events[-1]["reply"] == "Here you go."


def test_a_rate_limit_after_text_is_not_retried(monkeypatch):
    client = use(monkeypatch, FakeClient(
        [delta("Part of an answer"), ev("response.failed", response=done_response("", "failed", "rate_limit_exceeded"))]))
    _, events = stream_events()
    assert client.calls == 1
    assert events[-1] == {"type": "error", "error": cb.FRIENDLY["busy"], "status": 503}


def test_the_stream_stops_when_the_time_limit_passes(monkeypatch):
    use(monkeypatch, FakeClient([delta("a"), delta("b"), completed("ab")]))
    monkeypatch.setattr(cb, "TIMEOUT_SECONDS", -1)
    _, events = stream_events()
    assert events[-1] == {"type": "error", "error": cb.FRIENDLY["slow"], "status": 504}


def test_a_cancelled_run_closes_the_agents_stream():
    import threading
    stream = FakeStream([delta("a"), completed("a")])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(cs.Cancelled):
        cs.consume(stream, cs.Run(lambda e: None), cancel, deadline=1e12)
    assert stream.closed


# ---- before the stream begins: ordinary JSON answers ----

def test_a_page_that_is_not_allowed_gets_403(monkeypatch):
    use(monkeypatch, FakeClient([completed()]))
    response, events = stream_events(FakeRequest(origin="https://evil.example"))
    assert response.status_code == 403 and events == []
    assert "access-control-allow-origin" not in response.headers


def test_bad_json_gets_400(monkeypatch):
    use(monkeypatch, FakeClient([completed()]))
    response, _ = stream_events(FakeRequest(body=b"not json"))
    assert response.status_code == 400


def test_a_missing_conversation_gets_400(monkeypatch):
    use(monkeypatch, FakeClient([completed()]))
    response, _ = stream_events(FakeRequest(body=b"{}"))
    assert response.status_code == 400


def test_the_request_limit_gets_429(monkeypatch):
    use(monkeypatch, FakeClient([completed()]))
    monkeypatch.setattr(cb.limiter, "allow", lambda ip: (False, 30))
    response, _ = stream_events()
    assert response.status_code == 429 and response.headers["retry-after"] == "30"


def test_no_free_slot_gets_503(monkeypatch):
    use(monkeypatch, FakeClient([completed()]))
    monkeypatch.setattr(cb, "QUEUE_SECONDS", 0.05)
    monkeypatch.setattr(cb, "_get_slots", lambda: asyncio.Semaphore(0))
    response, _ = stream_events()
    assert response.status_code == 503 and json.loads(response.body)["error"] == cb.FRIENDLY["busy"]


def test_the_preflight_answers_204():
    response, _ = stream_events(FakeRequest(method="OPTIONS"))
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://www.sujeeth.org"


def test_status_text_follows_the_tool_name():
    assert cs.status_for({"name": "mcp_Sujeeth-Foundation-Graph.search_sujeeth_foundation"}) == "Searching the Foundation…"
    assert cs.status_for({"name": "get_documents"}) == "Reading a page…"
    assert cs.status_for({"name": "describe"}) == "Checking the Foundation's records…"
    assert cs.status_for({"name": "something_new"}) == cs.DEFAULT_STATUS
