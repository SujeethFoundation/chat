"""Tests for the ontology note added under a visitor's question (enrich.py)."""
import pytest

import chat_blueprint as cb
import enrich

ALLOWED = {"https://www.sujeeth.org"}

ONTOLOGY = {
    "ontology_version": "test",
    "classes": [
        {"id": "resume_resource", "name": "Resume Resource",
         "aliases": ["resume writing", "resume tips", "resume review", "cover letter"]},
        {"id": "interviewing", "name": "Interviewing", "aliases": ["job interview", "mock interview"]},
        {"id": "salary_negotiation", "name": "Salary Negotiation", "aliases": ["negotiate salary", "counteroffer"]},
        {"id": "career_development", "name": "Career Development", "aliases": ["job", "careers"]},
        {"id": "donation", "name": "Donation", "aliases": ["donate", "support the Foundation"]},
        {"id": "volunteer", "name": "Volunteer", "aliases": ["volunteering"]},
        {"id": "partnership", "name": "Partnership", "aliases": []},
        {"id": "book", "name": "Book", "aliases": ["books"]},
        {"id": "book_request", "name": "Book Request", "aliases": ["free book", "book order", "request a book"]},
        {"id": "program_statistics", "name": "Program Statistics", "aliases": ["how many students"]},
    ],
    "personas": [
        {"id": "student", "signals": {"language": ["my resume", "my circle"]}},
        {"id": "mentor", "signals": {"language": ["my circle", "my students", "I work at (an employer) and want to help"]}},
        {"id": "donor", "signals": {"language": ["donate", "tax deductible"]}},
    ],
    "ambiguous_terms": [
        {"term": "support the Foundation", "candidates": ["donation", "volunteer", "partnership"], "default": None,
         "by_persona": {"donor": "donation", "mentor": "volunteer"}},
        {"term": "books", "candidates": ["book_request", "book"], "default": "book", "by_persona": {"student": "book_request"}},
    ],
    "source_precedence": [
        {"question_type": "Career skills", "concepts": ["resume_resource", "interviewing", "salary_negotiation"],
         "prefer": ["approved third-party domains"]},
        {"question_type": "Counts", "concepts": ["program_statistics"], "prefer": ["sujeeth.org (live pages)"]},
    ],
}


def turns(*texts):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": text} for i, text in enumerate(texts)]


def ids(found):
    return [cid for cid, _ in found["concepts"]]


def run(*texts, page=None):
    return enrich.enrich(turns(*texts), page, ALLOWED, ontology=ONTOLOGY)


def test_matches_names_aliases_and_plurals():
    found = run("Can you do a mock interview and help with cover letters?")
    assert set(ids(found)) == {"interviewing", "resume_resource"}
    assert found["sources"] == [("Career skills", "approved third-party domains")]
    assert not found["carried"]


def test_whole_words_only():
    assert run("I enjoy jobbing around and bookkeeping") is None


def test_head_word_finds_the_concept_whose_phrases_use_it():
    assert "resume_resource" in ids(run("please look at my resume"))
    assert "book_request" not in ids(run("which book should I read?"))  # "book" is its own concept


def test_name_outranks_alias_and_the_list_is_capped(monkeypatch):
    monkeypatch.setattr(enrich, "MAX_CONCEPTS", 2)
    found = run("job hunting: salary negotiation, a mock interview, and resume tips")
    assert ids(found)[0] == "salary_negotiation" and len(ids(found)) == 2


def test_nothing_matched_means_no_note():
    assert run("What's a good vodka tonic recipe?") is None
    assert run("hi") is None


def test_follow_up_borrows_the_question_before():
    found = run("how do I negotiate salary?", "Here is how.", "tell me more")
    assert ids(found) == ["salary_negotiation"] and found["carried"]


def test_persona_needs_a_clear_lead():
    assert run("can you review my resume?")["persona"] == "student"
    assert run("a question about my circle and the job market")["persona"] is None  # student and mentor tie


def test_ambiguous_term_follows_persona_then_default():
    assert ids(run("is it tax deductible if I support the Foundation?")) == ["donation"]
    assert ids(run("I need books for class")) == ["book"]
    assert "book_request" in ids(run("I'm polishing my resume, and what about books?"))


def test_undecided_ambiguous_term_picks_no_side():
    assert run("how can I support the Foundation?") is None


def test_page_is_kept_only_from_an_allowed_site():
    assert run("donate", page="https://www.sujeeth.org/donate?utm=x#top")["page"] == "https://www.sujeeth.org/donate"
    assert run("donate", page="https://evil.example/donate")["page"] is None
    assert run("donate", page="javascript:alert(1)")["page"] is None
    assert run("donate", page={"not": "a string"})["page"] is None


def test_block_text_and_off_switch(monkeypatch):
    monkeypatch.setattr(enrich.ontology_copy, "get", lambda: ONTOLOGY)
    block = enrich.context_block(turns("help with my resume"), "https://www.sujeeth.org/week3", ALLOWED)
    assert block.startswith(enrich.OPEN) and block.endswith(enrich.CLOSE)
    assert "resume_resource (Resume Resource)" in block and "student" in block and "/week3" in block
    assert 'If the question is about "Career skills", prefer: approved third-party domains' in block
    monkeypatch.setattr(enrich, "ENABLED", False)
    assert enrich.context_block(turns("help with my resume"), None, ALLOWED) is None


def test_no_ontology_copy_means_no_note(monkeypatch):
    monkeypatch.setattr(enrich.ontology_copy, "get", lambda: None)
    assert enrich.context_block(turns("help with my resume"), None, ALLOWED) is None


def test_note_goes_under_the_newest_question_and_ignores_attachment_text(monkeypatch):
    monkeypatch.setattr(enrich.ontology_copy, "get", lambda: ONTOLOGY)
    parsed = [{"role": "user", "content": "thoughts?", "attachments": []}]
    agent = [{"role": "user", "content": "thoughts?\n\n[Attached file: cv.pdf]\nsalary negotiation, mock interview"}]
    assert cb.add_ontology_context(parsed, agent) is False  # the typed words name nothing; the file's text is not matched
    assert enrich.OPEN not in agent[0]["content"]
    parsed[0]["content"] = agent[0]["content"] = "how do I negotiate salary?"
    assert cb.add_ontology_context(parsed, agent) is True
    assert agent[0]["content"].startswith("how do I negotiate salary?\n\n" + enrich.OPEN)


def test_a_failure_in_enrichment_never_fails_the_chat(monkeypatch):
    monkeypatch.setattr(enrich, "context_block", lambda *a: 1 / 0)
    agent = [{"role": "user", "content": "hello"}]
    assert cb.add_ontology_context([{"role": "user", "content": "hello"}], agent) is False
    assert agent[0]["content"] == "hello"
