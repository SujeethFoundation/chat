"""Tests for the copy of ontology.json each instance keeps (ontology_copy.py)."""
import json
import os
from datetime import datetime, timezone

import pytest

import ontology_copy as oc

GOOD = json.dumps({"ontology_version": "1.2.3", "classes": [{"id": "resume_resource"}]}).encode()
NEWER = json.dumps({"ontology_version": "1.2.4", "classes": []}).encode()


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(oc, "LOCAL_PATH", str(tmp_path / "ontology.json"))
    monkeypatch.setattr(oc, "_data", None)
    monkeypatch.setattr(oc, "_fetched_at", None)
    monkeypatch.setattr(oc, "_started", False)


def serve(monkeypatch, body):
    def download():
        if isinstance(body, Exception):
            raise body
        return body
    monkeypatch.setattr(oc, "_download", download)


def test_no_copy_yet():
    assert oc.get() is None
    assert oc.status() == {"version": None, "copied_at": None}


def test_refresh_saves_file_and_memory(monkeypatch):
    serve(monkeypatch, GOOD)
    assert oc.refresh() is True
    assert oc.get()["ontology_version"] == "1.2.3"
    with open(oc.LOCAL_PATH, "rb") as saved:
        assert saved.read() == GOOD
    assert oc.status()["version"] == "1.2.3" and oc.status()["copied_at"].endswith("Z")


@pytest.mark.parametrize("bad", [b"<html>sign in</html>", b"[]", b'{"classes": []}', b'{"ontology_version": "1"}',
                                 OSError("network down")])
def test_bad_download_keeps_the_copy_from_before(monkeypatch, bad):
    serve(monkeypatch, GOOD)
    oc.refresh()
    serve(monkeypatch, bad)
    assert oc.refresh() is False
    assert oc.get()["ontology_version"] == "1.2.3"
    with open(oc.LOCAL_PATH, "rb") as saved:
        assert saved.read() == GOOD


def test_refresh_replaces_an_older_copy(monkeypatch):
    serve(monkeypatch, GOOD)
    oc.refresh()
    serve(monkeypatch, NEWER)
    assert oc.refresh() is True
    assert oc.get()["ontology_version"] == "1.2.4"
    assert not [name for name in os.listdir(os.path.dirname(oc.LOCAL_PATH)) if name.endswith(".tmp")]


def test_get_reads_the_saved_file_when_memory_is_empty(monkeypatch):
    with open(oc.LOCAL_PATH, "wb") as saved:
        saved.write(GOOD)
    assert oc.get()["ontology_version"] == "1.2.3"


def test_copy_still_works_when_disk_write_fails(monkeypatch):
    serve(monkeypatch, GOOD)
    monkeypatch.setattr(oc, "_save", lambda body: (_ for _ in ()).throw(OSError("disk full")))
    assert oc.refresh() is True
    assert oc.get()["ontology_version"] == "1.2.3"


def test_key_is_not_sent_to_github(monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return GOOD

    def fake_urlopen(request, timeout):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return Reply()

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(oc, "FUNCTION_KEY", "secret")
    oc._download()
    assert "x-functions-key" not in seen["headers"]
    monkeypatch.setattr(oc, "URL", "https://example.azurewebsites.net/api/ontology")
    oc._download()
    assert seen["headers"]["x-functions-key"] == "secret"


def test_seconds_until_next(monkeypatch):
    monkeypatch.setattr(oc, "REFRESH_HOUR_UTC", 8)
    assert oc.seconds_until_next(datetime(2026, 11, 14, 7, 0, tzinfo=timezone.utc)) == 3600
    assert oc.seconds_until_next(datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)) == 86400
    assert oc.seconds_until_next(datetime(2026, 11, 14, 22, 30, tzinfo=timezone.utc)) == 9.5 * 3600


def test_start_runs_once_and_respects_the_off_switch(monkeypatch):
    started = []
    monkeypatch.setattr(oc.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: started.append(kw["name"])})())
    assert oc.start() is True and oc.start() is False
    assert started == ["ontology-copy"]
    monkeypatch.setattr(oc, "_started", False)
    monkeypatch.setattr(oc, "ENABLED", False)
    assert oc.start() is False and started == ["ontology-copy"]
