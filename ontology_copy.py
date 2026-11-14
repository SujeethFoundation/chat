"""Keeps a copy of the Foundation's ontology.json on this instance, so chat code can read the ontology without a network call.

Each instance copies the file when it starts and again once a night. The copy is saved to local disk and kept in
memory; get() returns it.

Why this is a background thread and not a timer-triggered function: on Flex Consumption a timer function runs on its
own instance, apart from the instances that answer chats, and local disk is not shared between instances. A timer
function would refresh a copy that no chat ever reads. A thread started with the app runs on every instance that
serves chats, including the ones the platform adds under load.

A failed copy never replaces a good one: the instance keeps what it has and tries again the next night (and a few
times sooner if it has no copy at all).

Settings (application settings of the Function App):
  ONTOLOGY_URL               Where to copy from. Default: ontology/ontology.json on the main branch of the public
                             SujeethFoundation/ontology repo. To copy what the ontology app is serving instead, set it to
                             https://<ontology-app>/api/ontology and set ONTOLOGY_FUNCTION_KEY.
  ONTOLOGY_FUNCTION_KEY      Optional. Sent as x-functions-key. Never sent to the default GitHub address.
  ONTOLOGY_REFRESH_HOUR_UTC  (8) Hour of the nightly copy, 0-23 UTC. 8 is 2 a.m. Central in winter, 3 a.m. in summer.
  ONTOLOGY_LOCAL_PATH        Where the copy is saved. Default: ontology.json in the instance's temp folder.
  ONTOLOGY_TIMEOUT_SECONDS   (20) How long one download may take.
  ONTOLOGY_COPY_ENABLED      (true) Set to false to turn the copy off.
"""
import json
import logging
import os
import random
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("sujeeth-chat")

DEFAULT_URL = "https://raw.githubusercontent.com/SujeethFoundation/ontology/main/ontology/ontology.json"
MAX_BYTES = 5 * 1024 * 1024  # the file is about 130 KB; anything near this size is not the ontology
RETRY_SECONDS = (60, 300, 900)  # waits between tries while the instance has no copy at all
JITTER_SECONDS = 600  # instances spread their nightly copy over ten minutes


def _int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


URL = (os.getenv("ONTOLOGY_URL") or "").strip() or DEFAULT_URL
FUNCTION_KEY = (os.getenv("ONTOLOGY_FUNCTION_KEY") or "").strip()
REFRESH_HOUR_UTC = min(23, max(0, _int("ONTOLOGY_REFRESH_HOUR_UTC", 8)))
LOCAL_PATH = (os.getenv("ONTOLOGY_LOCAL_PATH") or "").strip() or os.path.join(tempfile.gettempdir(), "ontology.json")
TIMEOUT_SECONDS = max(1, _int("ONTOLOGY_TIMEOUT_SECONDS", 20))
ENABLED = (os.getenv("ONTOLOGY_COPY_ENABLED") or "true").strip().lower() not in ("0", "false", "no")

_lock = threading.Lock()
_data = None
_fetched_at = None  # time.time() of the last good copy
_started = False
_stop = threading.Event()


def _download():
    """Returns the ontology file's bytes. Raises on any network or HTTP problem."""
    headers = {"Accept": "application/json", "User-Agent": "sujeeth-chat"}
    if FUNCTION_KEY and URL != DEFAULT_URL:
        headers["x-functions-key"] = FUNCTION_KEY
    with urllib.request.urlopen(urllib.request.Request(URL, headers=headers), timeout=TIMEOUT_SECONDS) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("ontology file is larger than expected")
    return body


def _parse(body):
    """The ontology as a dict. Raises ValueError when the bytes are not an ontology file."""
    data = json.loads(body)
    if not isinstance(data, dict) or not data.get("ontology_version") or not isinstance(data.get("classes"), (list, dict)):
        raise ValueError("not an ontology file")
    return data


def _save(body):
    """Writes the file beside its final name, then swaps it in, so a reader never sees half a file."""
    folder = os.path.dirname(LOCAL_PATH) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=folder, prefix="ontology-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(body)
        os.replace(temp_path, LOCAL_PATH)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def refresh():
    """Copies the ontology now. True when a new copy is in place; False (after a warning) when the old one stays."""
    global _data, _fetched_at
    try:
        body = _download()
        data = _parse(body)
    except Exception as exc:
        log.warning("ontology copy failed (%s); keeping the copy from before", type(exc).__name__)
        return False
    try:
        _save(body)
    except Exception as exc:  # the copy in memory is still good without the file
        log.warning("ontology copy could not be saved to disk (%s)", type(exc).__name__)
    with _lock:
        _data, _fetched_at = data, time.time()
    log.info("ontology copy ok: version %s", data.get("ontology_version"))
    return True


def get():
    """The ontology as a dict, or None when this instance has no copy yet. Never makes a network call. Treat it as read-only."""
    global _data
    if _data is None:
        try:
            with open(LOCAL_PATH, "rb") as saved:
                data = _parse(saved.read())
        except (OSError, ValueError):
            return None
        with _lock:
            if _data is None:
                _data = data
    return _data


def status():
    """What the health route reports: the version held and when it was copied."""
    data = get()
    copied = datetime.fromtimestamp(_fetched_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if _fetched_at else None
    return {"version": data.get("ontology_version") if data else None, "copied_at": copied}


def seconds_until_next(now=None):
    """Seconds from now until the next nightly copy hour (UTC)."""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=REFRESH_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _run():
    # At startup: copy now, and while there is no copy at all, try again a few times before waiting for the night.
    ok = refresh()
    for wait in RETRY_SECONDS:
        if ok or _stop.wait(wait):
            break
        ok = refresh()
    while not _stop.wait(seconds_until_next() + random.uniform(0, JITTER_SECONDS)):
        refresh()


def start():
    """Starts the copy thread once per instance. Safe to call more than once."""
    global _started
    if not ENABLED:
        return False
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_run, name="ontology-copy", daemon=True).start()
    return True
