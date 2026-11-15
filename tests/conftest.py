"""Every test starts with no ontology copy on the instance, so chat tests do not depend on a file left on the machine."""
import pytest

import ontology_copy


@pytest.fixture(autouse=True)
def no_ontology_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(ontology_copy, "LOCAL_PATH", str(tmp_path / "ontology.json"))
    monkeypatch.setattr(ontology_copy, "_data", None)
    monkeypatch.setattr(ontology_copy, "_fetched_at", None)
