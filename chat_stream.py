"""Streaming chat route: the reply arrives as it is written instead of all at once.

  POST /api/chat/stream   same request body as /api/chat  ->  a stream of server-sent events (text/event-stream)

Each event is one line, "data: {json}", followed by a blank line. The json has a "type":
  status  {"text": "Searching the Foundation..."}   the agent is using a tool; show it while the visitor waits
  delta   {"text": "..."}                           the next piece of the answer; append it
  reset   {}                                        the text shown so far was not the answer (the agent spoke before
                                                    a tool call); clear it
  done    {"reply": "...", "fold": {"keep_last": N}}  the final answer, which replaces whatever was shown. "fold" is
                                                    present when the page should summarize older turns (see /api/chat/fold)
  error   {"error": "...", "status": 503}           the chat failed after the stream began, so the HTTP status could not
                                                    carry it. The text is safe to show.
A line that starts with ":" is a keep-alive comment and is ignored.

Problems found before the stream begins (bad origin, bad JSON, rate limit, no free slot) are ordinary JSON responses
with the usual status codes, exactly as on /api/chat. /api/chat stays as it was, so a page that has not been updated
keeps working.

The route shares everything else with /api/chat: the origin check, request limits, attachment reading, the history
shortening, the chat slots and the model client (all in chat_blueprint.py). It is the only route that uses the FastAPI
request and response types, which Azure Functions needs in order to stream. It needs the application setting
PYTHON_ENABLE_INIT_INDEXING=1.

How it runs: the model client is synchronous, so the stream is read on a chat-pool thread, one event at a time, and each
event is handed to the async response through a queue. The thread holds the chat slot until the run ends. When the visitor
closes the page the response is cancelled, the thread notices at its next event, and the slot is freed.
"""
import asyncio
import json
import logging
import random
import threading
import time
import uuid
from types import SimpleNamespace

import azure.functions as func
from azurefunctions.extensions.http.fastapi import JSONResponse, Response, StreamingResponse

import chat_blueprint as cb

log = logging.getLogger("sujeeth-chat")

HEARTBEAT_SECONDS = 15  # a quiet stretch (a slow tool call) still sends a keep-alive so nothing in between closes the connection

# What to tell the visitor while the agent uses a tool. Matched on the tool's name, first match wins.
STATUS_TEXT = (
    ("get_document", "Reading a page…"),
    ("query", "Checking the Foundation's records…"),
    ("describe", "Checking the Foundation's records…"),
    ("search", "Searching the Foundation…"),
)
DEFAULT_STATUS = "Working on it…"
QUIET_ITEMS = ("message", "reasoning", "mcp_list_tools")  # run items that are not a tool call


class Cancelled(Exception):
    """The visitor closed the page; stop reading the agent's stream."""


def sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")


def status_for(item):
    name = str(cb._field(item, "name") or "").lower()
    for needle, text in STATUS_TEXT:
        if needle in name:
            return text
    return DEFAULT_STATUS


class Run:
    """What one pass over the agent's stream has shown the visitor so far."""

    def __init__(self, emit):
        self.emit = emit
        self.shown = False        # text of a message is on the visitor's screen right now
        self.sent_text = False    # any text was sent in this pass, so a retry would repeat it
        self.first_text_at = None
        self.final = None         # the finished response

    def text(self, delta):
        if not delta:
            return
        self.shown = self.sent_text = True
        if self.first_text_at is None:
            self.first_text_at = time.perf_counter()
        self.emit({"type": "delta", "text": delta})

    def clear(self):
        if self.shown:
            self.shown = False
            self.emit({"type": "reset"})


def handle_event(run, event):
    """Turn one event from the agent's run into events for the page."""
    kind = str(getattr(event, "type", "") or "")
    if kind == "response.output_text.delta":
        run.text(getattr(event, "delta", "") or "")
    elif kind == "response.output_item.added":
        item = getattr(event, "item", None)
        item_type = str(cb._field(item, "type") or "")
        if item_type == "message":
            run.clear()   # a second message after text already shown: only the newest is the answer
        elif item_type not in QUIET_ITEMS:
            run.clear()   # the agent spoke before this tool call, so that text was not the answer
            run.emit({"type": "status", "text": status_for(item)})
    elif kind in ("response.completed", "response.failed", "response.incomplete"):
        response = getattr(event, "response", None)
        problem = cb._run_problem(response)
        if problem:
            raise cb.AgentRunFailed(*problem)
        run.final = response
    elif kind == "error":
        raise cb.AgentRunFailed("failed", getattr(event, "code", None), getattr(event, "message", None))


def consume(stream, run, cancel, deadline):
    """Read the agent's stream to its end. Raises Cancelled, TimeoutError, or whatever the run failed with."""
    try:
        for event in stream:
            if cancel.is_set():
                raise Cancelled()
            if time.monotonic() > deadline:
                raise TimeoutError("chat stream passed its time limit")
            handle_event(run, event)
    finally:
        close = getattr(stream, "close", None)
        if close:
            try:
                close()
            except Exception:  # a stream that will not close must not hide the real result
                pass
    if run.final is None:
        raise cb.AgentRunFailed("incomplete", "stream_ended")
    return cb.final_answer_text(run.final)


def stream_agent(messages, emit, cancel):
    """Ask the agent and stream the answer. Like ask_agent, a 429 makes this instance wait as the model asks and try
    again, but only while no answer text has gone to the visitor, because a retry would repeat it.
    Returns (text, Run)."""
    started = time.monotonic()
    rate_deadline = started + cb.RATE_LIMIT_WAIT_SECONDS
    deadline = started + cb.TIMEOUT_SECONDS
    attempt = 0
    while True:
        pause = cb._cooldown_until - time.monotonic()
        if pause > 0:
            time.sleep(max(0.0, min(pause, rate_deadline - time.monotonic())) + random.uniform(0, 0.5))
        run = Run(emit)
        try:
            stream = cb.get_client().responses.create(input=messages, stream=True)
            return consume(stream, run, cancel, deadline), run
        except Exception as exc:
            if (run.sent_text or cancel.is_set() or isinstance(exc, Cancelled)
                    or not cb._is_rate_limited(exc) or time.monotonic() >= rate_deadline):
                raise
            attempt += 1
            wait = cb._retry_after_seconds(exc)
            if wait is None:
                wait = min(2 ** attempt, 15)
            wait = min(max(wait, 1.0), 20.0)
            with cb._cooldown_lock:
                cb._cooldown_until = max(cb._cooldown_until, time.monotonic() + wait)
            log.warning("chat model over its token limit (429); waiting %.1fs (attempt %d)", wait, attempt)


def work(request_id, agent_messages, prior_summary, emit, cancel, began):
    """Runs on a chat-pool thread, in the chat's slot. Always ends by emitting None."""
    try:
        text, run = stream_agent(cb.summary_messages(prior_summary) + agent_messages, emit, cancel)
        keep = cb.plan_fold(prior_summary, agent_messages, text) if text else None
        if not text:
            log.error("chat %s failed: agent returned no text", request_id)
            emit({"type": "error", "error": cb.FRIENDLY["unavailable"], "status": 502})
        else:
            emit({"type": "done", "reply": text, **({"fold": {"keep_last": keep}} if keep else {})})
            first = f"{run.first_text_at - began:.1f}" if run.first_text_at else "none"
            log.info("chat %s stream ok turns=%d chars_in=%d chars_out=%d first_text=%s seconds=%.1f in_flight=%d fold_due=%s",
                     request_id, len(agent_messages), sum(len(m["content"]) for m in agent_messages), len(text), first,
                     time.perf_counter() - began, cb._in_flight, bool(keep))
    except Cancelled:
        log.info("chat %s stream closed by the visitor", request_id)
    except Exception as exc:  # the visitor sees a friendly line; the log has the cause
        status, key, note = cb.explain(exc)
        detail = f" | {type(exc).__name__}: {str(exc)[:300]}" if cb.LOG_ERROR_DETAIL else ""
        log.error("chat %s failed: %s%s", request_id, note, detail)
        emit({"type": "error", "error": cb.FRIENDLY[key], "status": status})
    finally:
        emit(None)


def json_reply(status, body, origin=None, extra=None):
    headers = {"Cache-Control": "no-store", **cb.cors_headers(origin), **(extra or {})}
    return JSONResponse(body, status_code=status, headers=headers)


async def handle(req):
    """The route. req is a FastAPI/Starlette request."""
    origin = req.headers.get("origin")
    if req.method == "OPTIONS":
        preflight = ({"Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type",
                      "Access-Control-Max-Age": "86400"} if cb.SEND_CORS_HEADERS else {})
        return Response(status_code=204, headers={**cb.cors_headers(origin), **preflight})

    payload, error = cb.check_payload(origin, await req.body())
    if error:
        status, body, send_origin = error
        return json_reply(status, body, origin if send_origin else None)
    messages, problem = cb.parse_messages(payload.get("messages"))
    if problem:
        return json_reply(400, {"error": problem}, origin)
    prior_summary = cb.clean_summary(payload.get("summary"))

    allowed, wait = cb.limiter.allow(cb.client_ip(SimpleNamespace(headers=req.headers)))
    if not allowed:
        return json_reply(429, {"error": "You are sending messages quickly. Please wait a moment.", "retry_after": wait},
                          origin, {"Retry-After": str(wait)})

    request_id, began = uuid.uuid4().hex[:8], time.perf_counter()
    agent_messages, file_error = await cb.read_files(request_id, messages)
    if file_error:
        return json_reply(400, {"error": file_error}, origin)

    # Wait for a free slot rather than turning the visitor away. While waiting, the request counts as in flight,
    # which is what makes the platform start another instance.
    slots = cb._get_slots()
    try:
        await asyncio.wait_for(slots.acquire(), cb.QUEUE_SECONDS)
    except asyncio.TimeoutError:
        log.warning("chat %s busy: no free slot after %ds (in_flight=%d of %d)", request_id, cb.QUEUE_SECONDS,
                    cb._in_flight, cb.MAX_CONCURRENT)
        return json_reply(503, {"error": cb.FRIENDLY["busy"]}, origin, {"Retry-After": "10"})

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    cancel = threading.Event()

    def emit(event):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    cb._in_flight += 1
    future = loop.run_in_executor(cb._pool, work, request_id, agent_messages, prior_summary, emit, cancel, began)

    def finished(done):
        cb._in_flight -= 1
        slots.release()
        if not done.cancelled():
            done.exception()  # mark any failure as seen

    future.add_done_callback(finished)

    async def events():
        try:
            yield sse({"type": "status", "text": "Thinking…"})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                if event is None:
                    return
                yield sse(event)
        finally:
            cancel.set()  # the visitor left, or the stream ended: either way the thread should stop

    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", **cb.cors_headers(origin)}
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


def register(app):
    """Add the streaming route to the Function App."""
    from azurefunctions.extensions.http.fastapi import Request

    @app.route(route="chat/stream", methods=["POST", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
    async def chat_stream(req: Request) -> StreamingResponse:
        return await handle(req)
