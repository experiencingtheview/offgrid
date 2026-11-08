"""Matching meaning without a model.

A fully offline stand-in for embeddings, built from three cheap deterministic
signals:

1. **stemming** - "deploying", "deployed" and "deploy" are the same idea.
2. **concepts** - a small, inspectable lexicon groups words that mean the same
   thing in this domain (verify/test/check, deploy/release/ship, water/purify).
3. **character n-grams hashed into a sparse vector** - catches morphology and
   typos ("verifcation" ~ "verification") with no training and no download.

This is lexical semantics with an explicit vocabulary, not a neural embedding,
and the docstrings say so. That is the honest trade: it cannot understand a
sentence it has never seen the words for, but every match can explain itself,
it needs no GPU, and it works on a USB stick with the network unplugged.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

DIMS = 256
_WORD = re.compile(r"[a-z0-9_]{2,}")

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from get got had has have how i if in
    into is it its just like me more most my no not of on or our out over so some than that the
    their them then there these they this to too up use used using very was we were what when where which
    who why will with would you your""".split()
)

# Each group is one idea. Words inside a group are interchangeable for matching.
# Deliberately small and readable: every entry can be argued with.
CONCEPT_GROUPS: Dict[str, Tuple[str, ...]] = {
    "verify": (
        "verify", "verifies", "verified", "verification", "validate", "validates", "validation",
        "validator", "check", "checks", "checking", "test", "tests", "testing", "tested",
        "assert", "assertion", "pytest", "unittest", "lint", "linter", "prove", "proves",
        "proof", "confirm", "confirms", "ensure",
    ),
    "build": (
        "build", "builds", "building", "compile", "compiles", "compiling", "make", "makefile",
        "bundle", "bundles", "package", "packages", "assemble", "artifact",
    ),
    "deploy": (
        "deploy", "deploys", "deployed", "deploying", "deployment", "release", "releases",
        "released", "ship", "ships", "shipped", "publish", "publishes", "rollout", "launch",
        "launches", "push", "pushes", "install", "installs",
    ),
    "fix": (
        "fix", "fixes", "fixed", "fixing", "bug", "bugs", "repair", "patch", "patches",
        "correct", "correction", "broken", "failure", "failures", "fail", "fails", "failed",
        "error", "errors", "crash", "crashes", "regression", "regressions", "wrong",
    ),
    "memory": (
        "remember", "remembers", "remembered", "memory", "recall", "note", "notes", "lesson",
        "lessons", "learn", "learned", "learning", "knowledge", "know", "knows", "known",
    ),
    "code": (
        "code", "codes", "program", "programs", "script", "scripts", "function", "functions",
        "implementation", "implements", "module", "modules", "source", "class", "method",
    ),
    "file": (
        "file", "files", "path", "paths", "directory", "directories", "folder", "folders",
        "repository", "repo", "project",
    ),
    "auth": (
        "login", "logins", "authentication", "authenticate", "auth", "signin", "session",
        "sessions", "credential", "credentials", "password", "passwords", "token", "tokens",
        "permission", "permissions", "access",
    ),
    "database": (
        "database", "databases", "db", "sql", "migration", "migrations", "schema", "schemas",
        "postgres", "postgresql", "sqlite", "mysql", "query", "queries", "table", "tables",
    ),
    "network": (
        "network", "networking", "http", "https", "request", "requests", "endpoint", "endpoints",
        "api", "apis", "url", "urls", "fetch", "socket", "server", "client", "connection",
    ),
    "deploy_plan": (
        "plan", "plans", "planning", "step", "steps", "task", "tasks", "sequence", "schedule",
        "schedule_step", "order", "priority", "priorities",
    ),
    "review": (
        "review", "reviews", "reviewer", "reviewed", "critique", "inspect", "inspects", "audit",
        "judge", "verdict",
    ),
    "docs": (
        "documentation", "docs", "readme", "guide", "guides", "manual", "manuals", "reference",
        "references", "tutorial", "explain", "explains", "example", "examples",
    ),
    "water": (
        "water", "purify", "purifies", "purification", "disinfect", "disinfects",
        "disinfection", "boil", "boils", "filter", "filters", "filtration", "potable",
        "drink", "drinks", "drinking", "sanitation", "chlorinate", "chlorine", "well",
    ),
    "medicine": (
        "medicine", "medical", "health", "injury", "injuries", "wound", "wounds", "infection",
        "infections", "antibiotic", "antibiotics", "fever", "bandage", "bandages", "care",
        "patient", "patients", "symptom", "symptoms",
    ),
    "power": (
        "power", "electricity", "electric", "battery", "batteries", "solar", "energy",
        "generator", "generators", "charge", "charging", "voltage", "inverter", "watt",
    ),
    "food": (
        "food", "foods", "eat", "eating", "cook", "cooking", "preserve", "preserving",
        "preservation", "storage", "ration", "rations", "seed", "seeds", "crop", "crops",
    ),
    "shelter": (
        "shelter", "shelters", "buildings", "house", "housing", "roof", "tent",
        "construction", "wall", "walls", "foundation",
    ),
    "danger": (
        "danger", "dangerous", "risk", "risks", "hazard", "hazards", "safety", "emergency",
        "emergencies", "warning", "warnings", "storm", "flood", "floods", "fire", "fires",
    ),
    "decision": (
        "decision", "decisions", "decide", "decides", "choice", "choices", "choose", "prefer",
        "preference", "preferences", "want", "wants", "policy",
    ),
}

CONCEPT_OF: Dict[str, str] = {}
for _name, _words in CONCEPT_GROUPS.items():
    for _word in _words:
        CONCEPT_OF.setdefault(_word, _name)


@dataclass(frozen=True)
class Lexicon:
    """A merged concept vocabulary. The built-in one is used by default."""

    groups: Dict[str, Tuple[str, ...]]
    concept_of: Dict[str, str]
    source: str = ""
    warnings: Tuple[str, ...] = ()

    def concept_for(self, word: str) -> str:
        word = (word or "").lower()
        return self.concept_of.get(word) or self.concept_of.get(stem(word), "")


BUILTIN_LEXICON = Lexicon(
    groups={name: tuple(members) for name, members in CONCEPT_GROUPS.items()},
    concept_of=dict(CONCEPT_OF),
)

_GROUP_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_LEXICON_WORD = re.compile(r"[a-z0-9_]{2,}\Z")


def load_lexicon(
    path: Union[str, Path], base: Optional[Lexicon] = None
) -> Lexicon:
    """Merge `.offgrid/lexicon.json` into the built-in vocabulary.

    Accepted shape:

        {"groups": {"billing": ["invoice", "receipt"], "verify": ["typecheck"]}}

    A top-level mapping of group names is accepted too. A missing file is not
    an error. Invalid entries and words that already belong to another concept
    are skipped with a warning rather than silently changing their meaning.
    """
    base = base or BUILTIN_LEXICON
    path = Path(path)
    if not path.is_file():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Lexicon(
            base.groups,
            base.concept_of,
            str(path),
            (f"could not read {path}: {exc}",),
        )
    if not isinstance(data, dict):
        return Lexicon(
            base.groups,
            base.concept_of,
            str(path),
            (f"{path}: expected an object of synonym groups",),
        )

    raw_groups = data.get("groups") if "groups" in data else data
    if not isinstance(raw_groups, dict):
        return Lexicon(
            base.groups,
            base.concept_of,
            str(path),
            (f"{path}: 'groups' must be an object",),
        )

    groups: Dict[str, List[str]] = {
        name: list(members) for name, members in base.groups.items()
    }
    concept_of = dict(base.concept_of)
    warnings: List[str] = []
    for raw_name, raw_members in raw_groups.items():
        name = str(raw_name).strip().lower()
        if not _GROUP_NAME.match(name):
            warnings.append(
                f"{path}: skipped group {raw_name!r}; use lower-case letters, digits and _"
            )
            continue
        if isinstance(raw_members, str):
            members = [raw_members]
        elif isinstance(raw_members, (list, tuple)):
            members = list(raw_members)
        else:
            warnings.append(f"{path}: group {name!r} must be a list of words")
            continue

        added = 0
        for raw_word in members:
            word = str(raw_word).strip().lower()
            if not _LEXICON_WORD.match(word):
                warnings.append(f"{path}: ignored invalid word {raw_word!r} in {name!r}")
                continue
            existing = concept_of.get(word)
            if existing and existing != name:
                warnings.append(
                    f"{path}: {word!r} already belongs to {existing!r}; "
                    f"not moved to {name!r}"
                )
                continue
            if word not in groups.setdefault(name, []):
                groups[name].append(word)
            # A project file normally lists base words, so let inflections
            # (invoice/invoices, receipt/receipts) reach the same concept.
            # The built-in vocabulary lists its inflections explicitly; custom
            # files should not have to repeat every plural by hand.
            aliases = {word, stem(word)}
            if word.endswith("s"):
                aliases.add(word[:-1])
                if word.endswith("es"):
                    aliases.add(word[:-2])
            else:
                aliases.add(word + "s")
                if word.endswith(("s", "x", "z", "ch", "sh")):
                    aliases.add(word + "es")
                if word.endswith("y"):
                    aliases.add(word[:-1] + "ies")
            for alias in aliases:
                if not alias:
                    continue
                existing_alias = concept_of.get(alias)
                if existing_alias and existing_alias != name:
                    continue
                concept_of[alias] = name
            added += 1
        if not added and name not in groups:
            warnings.append(f"{path}: group {name!r} has no usable words")

    return Lexicon(
        groups={name: tuple(members) for name, members in groups.items()},
        concept_of=concept_of,
        source=str(path),
        warnings=tuple(warnings),
    )

# A bare n-gram overlap is not meaning. Below this, only shared words/concepts count.
_NGRAM_FLOOR = 0.42
# Anything scoring under this is noise, not a match.
_SCORE_FLOOR = 0.16

_SUFFIXES = ("ingly", "edly", "ation", "ions", "ing", "ers", "er", "ies", "ied", "es", "ed", "ly", "s")


def stem(word: str) -> str:
    """A deliberately small stemmer: enough for English inflections, no surprises."""
    word = (word or "").lower()
    if len(word) <= 3:
        return word
    for suffix in _SUFFIXES:
        if not word.endswith(suffix):
            continue
        base = word[: -len(suffix)]
        if len(base) < 4:
            continue
        if suffix in ("ies", "ied"):
            return base + "y"
        return base
    return word


def words(text: str) -> List[str]:
    return [word for word in _WORD.findall((text or "").lower()) if word not in STOPWORDS]


def concepts(text: str, lexicon: Optional[Lexicon] = None) -> Tuple[str, ...]:
    """Every distinct idea the text touches, via the lexicon (stem fallback included)."""
    mapping = (lexicon or BUILTIN_LEXICON).concept_of
    found = set()
    for word in words(text):
        name = mapping.get(word) or mapping.get(stem(word))
        if name:
            found.add(name)
    return tuple(sorted(found))


def explain_concepts(
    text: str, lexicon: Optional[Lexicon] = None
) -> Dict[str, Tuple[str, ...]]:
    """Which words produced which concepts - used when a match needs defending."""
    mapping = (lexicon or BUILTIN_LEXICON).concept_of
    out: Dict[str, List[str]] = {}
    for word in words(text):
        name = mapping.get(word) or mapping.get(stem(word))
        if name:
            out.setdefault(name, []).append(word)
    return {name: tuple(sorted(set(items))) for name, items in out.items()}


def _hash(fragment: str) -> int:
    return int.from_bytes(hashlib.blake2b(fragment.encode("utf-8"), digest_size=4).digest(), "big") % DIMS


def vector(text: str) -> Dict[int, float]:
    """Sparse hashed bag of word and character trigrams, L2-normalised."""
    buckets: Dict[int, float] = {}
    for word in words(text):
        buckets[_hash("w:" + word)] = buckets.get(_hash("w:" + word), 0.0) + 1.0
        padded = f"^{word}$"
        for index in range(len(padded) - 2):
            gram = padded[index : index + 3]
            key = _hash("g:" + gram)
            buckets[key] = buckets.get(key, 0.0) + 0.5
    norm = math.sqrt(sum(value * value for value in buckets.values()))
    if not norm:
        return {}
    return {key: value / norm for key, value in buckets.items()}


def cosine(left: Dict[int, float], right: Dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


@dataclass(frozen=True)
class Match:
    """How well a document answers a query, with the reason kept for the user."""

    score: float = 0.0
    lexical: float = 0.0
    semantic: float = 0.0
    shared: Tuple[str, ...] = ()
    shared_words: Tuple[str, ...] = ()

    @property
    def useful(self) -> bool:
        if not (self.shared or self.shared_words or self.semantic >= _NGRAM_FLOOR):
            return False
        return self.score >= _SCORE_FLOOR

    def reason(self) -> str:
        if self.shared:
            return "same idea: " + ", ".join(self.shared)
        if self.shared_words:
            return "same wording: " + ", ".join(self.shared_words[:4])
        return f"similar wording ({self.semantic:.2f})"

    def as_dict(self) -> Dict[str, object]:
        return {
            "score": round(self.score, 3),
            "lexical": round(self.lexical, 3),
            "semantic": round(self.semantic, 3),
            "concepts": list(self.shared),
            "words": list(self.shared_words),
        }


_NO_MATCH = Match()


def match(
    query: str, document: str, lexicon: Optional[Lexicon] = None
) -> Match:
    """Compare a query against one document, by meaning first and wording second."""
    query_words = words(query)
    if not query_words:
        return _NO_MATCH
    doc_words = words(document)
    if not doc_words:
        return _NO_MATCH

    query_stems = {stem(word) for word in query_words}
    doc_stems = {stem(word) for word in doc_words}
    shared_stems = tuple(sorted(query_stems & doc_stems))
    lexical = len(shared_stems) / max(1, len(query_stems))

    query_concepts = set(concepts(query, lexicon))
    shared_concepts = tuple(sorted(query_concepts & set(concepts(document, lexicon))))
    concept_score = len(shared_concepts) / max(1, len(query_concepts))

    semantic = cosine(vector(query), vector(document))
    score = 0.45 * concept_score + 0.30 * semantic + 0.25 * lexical
    return Match(
        score=round(score, 4),
        lexical=round(lexical, 4),
        semantic=round(semantic, 4),
        shared=shared_concepts,
        shared_words=shared_stems,
    )


def rank(
    query: str, documents: Iterable[str], lexicon: Optional[Lexicon] = None
) -> List[Tuple[int, Match]]:
    """Every document that is genuinely related, best first."""
    scored: List[Tuple[int, Match]] = []
    for index, document in enumerate(documents):
        found = match(query, document, lexicon=lexicon)
        if found.useful:
            scored.append((index, found))
    scored.sort(key=lambda item: item[1].score, reverse=True)
    return scored
