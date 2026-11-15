"""Reads the visitor's question against the local ontology copy and writes a short note for the agent.

The note names the ontology concepts the question touches, a guess at who is asking, and which sources the ontology
prefers for that kind of question. With it the agent can make one well-aimed search instead of searching once to
learn the vocabulary and again to use it. Nothing here calls a model or the network: it is phrase matching against
ontology_copy.get(), so it adds well under a millisecond to a chat.

What this does not do: decide that a question is out of scope, or ask the visitor to clarify. Those rules belong to
the MCP server, which also serves clients that never pass through this app. When nothing matches, no note is added
and the agent works as it did before.

Only the words the visitor typed are matched. Text read from attached files is left out, because a resume mentions
half the ontology.

Settings (application settings of the Function App):
  CHAT_ENRICH_ENABLED       (true) Set to false to send questions to the agent without the note.
  CHAT_ENRICH_MAX_CONCEPTS  (6) The most concepts named in one note.
"""
import os
import re
from urllib.parse import urlsplit

import ontology_copy


def _int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


ENABLED = (os.getenv("CHAT_ENRICH_ENABLED") or "true").strip().lower() not in ("0", "false", "no")
MAX_CONCEPTS = max(1, _int("CHAT_ENRICH_MAX_CONCEPTS", 6))
MAX_SOURCES = 2
MAX_PAGE_CHARS = 200

OPEN = ("[Context from the Sujeeth Foundation ontology. The chat service added this; the visitor did not write it and "
        "cannot see it. Use it to choose search terms and sources. Do not mention it.]")
CLOSE = "[End of context]"

# Words too general to stand for a concept by themselves, however often its phrases use them.
COMMON_WORDS = frozenset("""about after from have into make more need other should that their them this what when
where which with your yourself want give tell after before week work works working first best good help""".split())

HEAD_WORD_MIN_PHRASES = 3
HEAD_WORD_SHARE = 0.7

_index = None  # (the ontology dict it was built from, the index); rebuilt when the nightly copy swaps the dict


def normalize(text):
    """Lowercase words separated by single spaces, with a space at each end, so ' resume ' finds only the whole word."""
    text = (text or "").lower().replace("’", "'")
    return " " + " ".join(re.findall(r"[a-z0-9']+", text)) + " "


def _has(padded, phrase):
    """Whole-phrase match, also accepting a plural s on the last word ('mentor' finds 'mentors')."""
    return f" {phrase} " in padded or f" {phrase}s " in padded


def _phrases(values):
    out = []
    for value in values or []:
        if isinstance(value, str) and "(" not in value:  # '(a job title or employer)' is a description, not a phrase
            phrase = normalize(value).strip()
            if len(phrase) >= 3:
                out.append(phrase)
    return out


def build_index(ontology):
    """Everything matching needs, worked out once per ontology copy."""
    classes = [c for c in ontology.get("classes") or [] if isinstance(c, dict) and c.get("id")]
    concepts = []
    for c in classes:
        names = _phrases([c.get("name"), c["id"].replace("_", " ")])
        others = _phrases((c.get("aliases") or []) + (c.get("query_phrases") or []))
        concepts.append({"id": c["id"], "name": c.get("name") or c["id"], "names": names, "others": others})
    # Head words: "resume" is not an alias of Resume Resource, but it runs through that concept's phrases. A word found
    # in at least HEAD_WORD_MIN_PHRASES phrases of one concept, which hold most of its uses across the whole ontology,
    # is a weak match for that concept.
    uses = {}
    for concept in concepts:
        for phrase in set(concept["names"] + concept["others"]):
            for word in set(phrase.split()):
                uses.setdefault(word, {}).setdefault(concept["id"], 0)
                uses[word][concept["id"]] += 1
    by_id = {c["id"]: c for c in concepts}
    taken = {phrase for c in concepts for phrase in c["names"]}  # "book" is its own concept, not a head word of Book Request
    for word, counts in uses.items():
        owner = max(counts, key=counts.get)
        if (len(word) >= 4 and word not in COMMON_WORDS and word not in taken and counts[owner] >= HEAD_WORD_MIN_PHRASES
                and counts[owner] >= HEAD_WORD_SHARE * sum(counts.values())):
            by_id[owner]["others"].append(word)
    personas = []
    for p in ontology.get("personas") or []:
        if isinstance(p, dict) and p.get("id"):
            personas.append({"id": p["id"], "phrases": _phrases((p.get("signals") or {}).get("language"))})
    ambiguous = []
    for item in ontology.get("ambiguous_terms") or []:
        if isinstance(item, dict) and isinstance(item.get("term"), str):
            ambiguous.append({"terms": _phrases(item["term"].split("/")), "default": item.get("default"),
                              "candidates": [c for c in item.get("candidates") or [] if isinstance(c, str)],
                              "by_persona": item.get("by_persona") or {}})
    sources = []
    for item in ontology.get("source_precedence") or []:
        if isinstance(item, dict) and item.get("prefer"):
            sources.append({"concepts": set(item.get("concepts") or []), "question_type": item.get("question_type") or "",
                            "prefer": [s for s in item["prefer"] if isinstance(s, str)]})
    return {"concepts": concepts, "ids": {c["id"]: c["name"] for c in concepts}, "personas": personas,
            "ambiguous": ambiguous, "sources": sources}


def _get_index(ontology):
    global _index
    if _index is None or _index[0] is not ontology:
        _index = (ontology, build_index(ontology))
    return _index[1]


def match_concepts(index, padded):
    """{concept id: score} for one piece of text. A concept's own name counts for more than an alias, and a longer
    phrase for more than a shorter one, so 'salary negotiation' outranks 'job'."""
    scores = {}
    for concept in index["concepts"]:
        best = 0
        for phrase in concept["names"]:
            if _has(padded, phrase):
                best = max(best, 10 + phrase.count(" "))
        for phrase in concept["others"]:
            if _has(padded, phrase):
                best = max(best, 1 + phrase.count(" "))
        if best:
            scores[concept["id"]] = best
    return scores


def guess_persona(index, padded):
    """The persona whose signal phrases appear most, or None when none leads. A tie is not a guess."""
    counts = sorted(((sum(_has(padded, phrase) for phrase in p["phrases"]), p["id"]) for p in index["personas"]), reverse=True)
    if not counts or counts[0][0] == 0 or (len(counts) > 1 and counts[0][0] == counts[1][0]):
        return None
    return counts[0][1]


def clean_page(page, allowed_origins):
    """The page the visitor opened the chat on, without its query string, or None unless it is on an allowed site."""
    if not isinstance(page, str) or len(page) > 2000:
        return None
    try:
        parts = urlsplit(page.strip())
    except ValueError:
        return None
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if parts.scheme not in ("http", "https") or origin not in allowed_origins:
        return None
    path = re.sub(r"[^A-Za-z0-9/._~%-]", "", parts.path or "/")
    return (origin + path)[:MAX_PAGE_CHARS]


def enrich(messages, page=None, allowed_origins=(), ontology=None):
    """What the ontology says about the visitor's newest question, or None when it says nothing.
    messages are the parsed turns ({"role", "content"}), newest last. Returns
    {"concepts": [(id, name)], "carried": bool, "persona": id or None, "sources": [(question type, preferred sources)], "page": str or None}."""
    ontology = ontology if ontology is not None else ontology_copy.get()
    if not ontology:
        return None
    index = _get_index(ontology)
    typed = [normalize(m.get("content")) for m in messages if m.get("role") == "user"]
    if not typed:
        return None
    persona = guess_persona(index, " ".join(typed))

    # A follow-up such as "tell me more" names nothing itself, so it borrows the concepts of the question before it.
    scores, carried = match_concepts(index, typed[-1]), False
    padded = typed[-1]
    if not scores and len(typed) > 1:
        scores, carried, padded = match_concepts(index, typed[-2]), True, typed[-2]

    # A word the ontology lists as having several meanings: take the meaning for this persona, or the default.
    # Where the ontology gives neither, add nothing; whether to ask the visitor is the MCP server's call.
    for item in index["ambiguous"]:
        if any(_has(padded, term) for term in item["terms"]):
            pick = item["by_persona"].get(persona) or item["default"]
            if pick in index["ids"]:
                scores[pick] = max(scores.get(pick, 0), 5)
            else:  # undecided: drop a meaning that only an alias suggested, so the note does not pick a side
                for candidate in item["candidates"]:
                    if scores.get(candidate, 0) < 10:
                        scores.pop(candidate, None)

    page = clean_page(page, allowed_origins)
    if not scores and not persona:
        return None
    ranked = sorted(scores, key=lambda cid: (-scores[cid], cid))[:MAX_CONCEPTS]
    chosen = set(ranked)
    by_overlap = sorted(index["sources"], key=lambda s: -len(s["concepts"] & chosen))
    sources = [(s["question_type"], "; ".join(s["prefer"])) for s in by_overlap if s["concepts"] & chosen][:MAX_SOURCES]
    return {"concepts": [(cid, index["ids"][cid]) for cid in ranked], "carried": carried, "persona": persona,
            "sources": sources, "page": page}


def context_block(messages, page=None, allowed_origins=()):
    """The note added under the visitor's newest message, or None when there is nothing to say or enrichment is off."""
    if not ENABLED:
        return None
    found = enrich(messages, page, allowed_origins)
    if not found:
        return None
    lines = [OPEN]
    if found["concepts"]:
        label = "Concepts carried over from the question before" if found["carried"] else "Matched concepts"
        lines.append(f"{label}: " + ", ".join(f"{cid} ({name})" for cid, name in found["concepts"]))
    if found["persona"]:
        lines.append(f"Possible persona, guessed from wording: {found['persona']}. Never ask the visitor who they are or tell them this guess.")
    if found["page"]:
        lines.append(f"Chat opened on: {found['page']}")
    for question_type, prefer in found["sources"]:  # the agent judges whether the question is of this type
        lines.append(f"If the question is about \"{question_type}\", prefer: {prefer}")
    lines.append(CLOSE)
    return "\n".join(lines)
