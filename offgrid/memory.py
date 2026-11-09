"""Long-term memory: what this agent learned, kept on disk, kept honest.

A tiny SQLite database (stdlib) under `.offgrid/memory.db`. No embeddings, no
vector service - just deterministic keyword scoring, because it has to work on a
laptop with no network and it has to be explainable.

Every entry records where it came from and how confident it is, so recall can
say "you wrote this down after a run that failed verification" instead of
presenting a guess as a fact.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .private import ensure_private_dir, harden_file
from .semantics import Lexicon, load_lexicon, match

KINDS = ("fact", "repo", "lesson", "warning", "preference", "decision", "correction", "source")
REJECTION_SIMILARITY = 0.90  # near-identical wording counts as the same rejected lesson
# Meaning overlap that counts as the same lesson in different words. Kept above
# mere topical similarity, so sharing a subject does not block a new lesson.
REJECTION_SEMANTIC_SCORE = 0.62
MAX_EVIDENCE_CHARS = 2000  # verification output kept with a lesson, for review
REVIEW_EVIDENCE_CHARS = 600  # how much of it the review prompt prints

# Higher weight means the entry surfaces more readily for a given term overlap.
KIND_WEIGHT = {
    "repo": 1.35,
    "fact": 1.15,
    "correction": 1.5,
    "warning": 1.4,
    "lesson": 1.25,
    "decision": 1.0,
    "preference": 1.0,
    "source": 0.9,
}

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from get got had has have how i if in
    into is it its just like make me more most my no not of on or our out over so some than that the
    their them then there these they this to too up use used using very was we were what when where which
    who why will with would you your""".split()
)

_TOKEN = re.compile(r"[a-z0-9_]{2,}")


@dataclass
class Memory:
    id: int
    kind: str
    text: str
    key: str
    source: str
    confidence: float
    created: float
    uses: int
    status: str = "accepted"  # accepted | pending. Rejected text lives in the ledger.
    score: float = 0.0
    reason: str = ""  # why recall picked it, for the human reading the list
    verify_command: str = ""
    verify_output: str = ""

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.created) / 86400.0)

    def line(self, show_age: bool = True, show_source: bool = True) -> str:
        bits = [f"#{self.id}", self.kind, f"conf {self.confidence:.1f}"]
        if self.status != "accepted":
            bits.append(self.status)
        if show_age:
            age = self.age_days
            bits.append("today" if age < 1 else f"{age:.0f}d ago")
        head = "[" + ", ".join(bits) + "]"
        tail = ""
        if show_source and self.source:
            tail = f" (from {self.source})"
        return f"{head} {self.text}{tail}"

    def evidence_lines(self, max_chars: int = REVIEW_EVIDENCE_CHARS) -> List[str]:
        """The verification behind a council lesson, ready to print beneath it.

        Only council lessons carry evidence: accepting one means trusting an
        outside opinion, so the human reviewing it should see what was actually
        run and what it printed. Answers with no evidence print nothing extra.
        """
        if not self.source.startswith("council"):
            return []
        command = (self.verify_command or "").strip()
        output = (self.verify_output or "").strip()
        if not command and not output:
            return []
        lines: List[str] = []
        if command:
            lines.append(f"    verified with `{command}` (it passed)")
        if output:
            lines.append("    output:")
            lines.extend(
                "      " + part for part in clip_evidence(output, max_chars).splitlines()
            )
        return lines


@dataclass
class Rejection:
    """A lesson the human refused, kept so the archivist cannot quietly re-propose it."""

    id: int
    kind: str
    text: str
    key: str
    source: str
    confidence: float
    rejected: float
    count: int

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.rejected) / 86400.0)

    def line(self) -> str:
        age = self.age_days
        bits = [
            f"#{self.id}",
            self.kind,
            f"conf {self.confidence:.1f}",
            f"rejected {self.count}x",
            "today" if age < 1 else f"{age:.0f}d ago",
        ]
        head = "[" + ", ".join(bits) + "]"
        tail = f" (from {self.source})" if self.source else ""
        return f"{head} {self.text}{tail}"


def tokens(text: str) -> List[str]:
    return [word for word in _TOKEN.findall((text or "").lower()) if word not in STOPWORDS]


def normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip().lower())
    return re.sub(r"[^\w\s]", "", text)


def make_key(kind: str, text: str) -> str:
    return hashlib.sha1(f"{kind}|{normalize(text)}".encode("utf-8")).hexdigest()[:16]


def clip_evidence(text: str, max_chars: int = MAX_EVIDENCE_CHARS) -> str:
    """Keep verification output short enough to store and read at a review prompt.

    The ends matter most - the command line and the failure summary - so long
    output keeps both instead of trailing off into a wall of warnings.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if len(text) <= max_chars:
        return text
    marker = "\n... (truncated) ...\n"
    half = max(1, (max_chars - len(marker)) // 2)
    return f"{text[:half]}{marker}{text[-half:]}"


class MemoryStore:
    """Durable notes with keyword recall. Safe to share between agent threads."""

    # Kinds that stay in context for every task, keyword match or not: how a
    # project is verified matters even when the task shares no words with it.
    ALWAYS_KINDS = ("repo", "preference", "correction")

    def __init__(
        self,
        path: Path,
        lexicon: Optional[Lexicon] = None,
        lexicon_file: Optional[Path] = None,
    ) -> None:
        self.path = Path(path)
        ensure_private_dir(self.path.parent)
        # A project file next to memory.db is the default extension point.
        self.lexicon = lexicon or load_lexicon(
            lexicon_file if lexicon_file is not None else self.path.parent / "lexicon.json"
        )
        self.lexicon_warnings = list(self.lexicon.warnings)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        harden_file(self.path)
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    key TEXT UNIQUE,
                    source TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.6,
                    created REAL NOT NULL,
                    uses INTEGER DEFAULT 0,
                    verify_command TEXT DEFAULT '',
                    verify_output TEXT DEFAULT ''
                )
                """
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_kind ON memories(kind)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created)")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS lesson_rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    lesson_key TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.6,
                    rejected REAL NOT NULL,
                    count INTEGER DEFAULT 1
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rejected ON lesson_rejections(rejected)"
            )
            # Older databases predate the review gate. Whatever is already in
            # there has been influencing runs, so it stays accepted.
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(memories)").fetchall()}
            if "status" not in columns:
                self._db.execute(
                    "ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'accepted'"
                )
            # Evidence columns arrived with council lesson review; older rows
            # simply have none to show.
            if "verify_command" not in columns:
                self._db.execute(
                    "ALTER TABLE memories ADD COLUMN verify_command TEXT DEFAULT ''"
                )
            if "verify_output" not in columns:
                self._db.execute(
                    "ALTER TABLE memories ADD COLUMN verify_output TEXT DEFAULT ''"
                )

    # -- writing ---------------------------------------------------------
    def remember(
        self,
        text: str,
        kind: str = "fact",
        source: str = "",
        confidence: float = 0.6,
        key: str = "",
        status: str = "accepted",
        verify_command: str = "",
        verify_output: str = "",
    ) -> Optional[int]:
        """Store a note. Returns the row id, or None if it was a duplicate.

        `status="pending"` keeps the note out of every prompt and recall until a
        human accepts it. A duplicate bumps the use count and keeps the higher
        confidence instead of piling up near-identical rows.
        """
        text = (text or "").strip()
        if len(text) < 4:
            return None
        kind = kind if kind in KINDS else "fact"
        confidence = max(0.0, min(1.0, float(confidence)))
        status = "pending" if status == "pending" else "accepted"
        command = (verify_command or "").strip()[:300]
        output = clip_evidence(verify_output)
        row_key = key or make_key(kind, text)
        # Automatic lessons are checked against the ledger. An explicit accepted
        # `remember` is a human/agent override and is still allowed.
        if status == "pending" and self.was_rejected(text):
            return None
        now = time.time()
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT id, confidence, status FROM memories WHERE key = ?", (row_key,)
            ).fetchone()
            if existing:
                # An explicit `remember` accepts; a repeated pending lesson stays pending.
                new_status = "accepted" if status == "accepted" else existing["status"]
                self._db.execute(
                    "UPDATE memories SET uses = uses + 1, confidence = MAX(confidence, ?), "
                    "text = ?, status = ?, "
                    "verify_command = CASE WHEN ? <> '' THEN ? ELSE verify_command END, "
                    "verify_output = CASE WHEN ? <> '' THEN ? ELSE verify_output END "
                    "WHERE id = ?",
                    (
                        confidence,
                        text,
                        new_status,
                        command,
                        command,
                        output,
                        output,
                        existing["id"],
                    ),
                )
                return None
            cursor = self._db.execute(
                "INSERT INTO memories "
                "(kind, text, key, source, confidence, created, uses, status, "
                "verify_command, verify_output) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (kind, text, row_key, source[:200], confidence, now, status, command, output),
            )
            return int(cursor.lastrowid)

    def forget(self, ident: int) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM memories WHERE id = ?", (int(ident),))
            return cursor.rowcount > 0

    # -- the review gate -------------------------------------------------
    def pending(self, limit: int = 20) -> List[Memory]:
        """New lessons waiting for a human. Not used anywhere until accepted."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE status = 'pending' ORDER BY created ASC, id ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get_by_key(self, key: str) -> Optional[Memory]:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE key = ?", (key,)).fetchone()
        return self._row(row) if row else None

    def accept(self, ids: Iterable[int]) -> int:
        changed = 0
        with self._lock, self._db:
            for ident in ids:
                cursor = self._db.execute(
                    "UPDATE memories SET status = 'accepted' WHERE id = ? AND status = 'pending'",
                    (int(ident),),
                )
                changed += max(0, cursor.rowcount)
        return changed

    def reject(self, ids: Iterable[int]) -> int:
        """Remove notes from active memory and keep their text in the rejection ledger."""
        changed = 0
        now = time.time()
        with self._lock, self._db:
            for ident in ids:
                row = self._db.execute(
                    "SELECT kind, text, key, source, confidence FROM memories WHERE id = ?",
                    (int(ident),),
                ).fetchone()
                if row is None:
                    continue
                text = str(row["text"] or "")
                text_key = make_key("rejected", text)
                existing = self._db.execute(
                    "SELECT id FROM lesson_rejections WHERE text_key = ?", (text_key,)
                ).fetchone()
                if existing:
                    self._db.execute(
                        "UPDATE lesson_rejections SET kind = ?, text = ?, lesson_key = ?, "
                        "source = ?, confidence = MAX(confidence, ?), rejected = ?, "
                        "count = count + 1 WHERE id = ?",
                        (
                            str(row["kind"] or "lesson"),
                            text,
                            str(row["key"] or ""),
                            str(row["source"] or ""),
                            float(row["confidence"] or 0.0),
                            now,
                            int(existing["id"]),
                        ),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO lesson_rejections "
                        "(text_key, kind, text, lesson_key, source, confidence, rejected, count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                        (
                            text_key,
                            str(row["kind"] or "lesson"),
                            text,
                            str(row["key"] or ""),
                            str(row["source"] or ""),
                            float(row["confidence"] or 0.0),
                            now,
                        ),
                    )
                cursor = self._db.execute("DELETE FROM memories WHERE id = ?", (int(ident),))
                changed += max(0, cursor.rowcount)
        return changed

    def forgive(self, ids: Iterable[int]) -> int:
        """Remove entries from the rejection ledger, never restore a lesson.

        Forgiving lifts the negative record so an idea may be proposed again. It
        deliberately does not insert anything into active or pending memory.
        """
        changed = 0
        with self._lock, self._db:
            for ident in ids:
                cursor = self._db.execute(
                    "DELETE FROM lesson_rejections WHERE id = ?", (int(ident),)
                )
                changed += max(0, cursor.rowcount)
        return changed

    def rejections_by_id(self, ids: Iterable[int]) -> List[Rejection]:
        """The ledger entries for specific IDs, in the order requested."""
        wanted = list(dict.fromkeys(int(ident) for ident in ids))
        if not wanted:
            return []
        placeholders = ", ".join("?" for _ in wanted)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM lesson_rejections WHERE id IN ({placeholders})",
                wanted,
            ).fetchall()
        found = {int(row["id"]): self._rejection(row) for row in rows}
        return [found[ident] for ident in wanted if ident in found]

    def matching_rejections(self, text: str, limit: int = 500) -> List[Rejection]:
        """The ledger entries an exact text or meaning search would forgive."""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) >= 4:
            with self._lock:
                row = self._db.execute(
                    "SELECT * FROM lesson_rejections WHERE text_key = ?",
                    (make_key("rejected", text),),
                ).fetchone()
            if row is not None:
                return [self._rejection(row)]
        return self.rejected(limit=limit, query=text)

    def forgive_matching(self, text: str, limit: int = 500) -> int:
        """Forgive ledger entries by exact text, or else by a meaning search.

        An exact text match (case and punctuation normalised) wins over the
        broader search, so forgiving one specific lesson cannot accidentally
        lift its neighbours' bans. Returns how many records were removed.
        """
        return self.forgive([item.id for item in self.matching_rejections(text, limit)])

    def rejected(self, limit: int = 50, query: str = "") -> List[Rejection]:
        """The rejection ledger, newest first. These never enter prompts."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM lesson_rejections ORDER BY rejected DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        items = [self._rejection(row) for row in rows]
        query = (query or "").strip()
        if not query:
            return items[:limit]
        return [item for item in items if match(query, item.text, lexicon=self.lexicon).useful][:limit]

    def was_rejected(self, text: str) -> bool:
        """True for a rejected lesson, however it is reworded.

        Exact text, near-identical wording and semantic matches through the
        project lexicon all count. The threshold sits above mere topical
        similarity, so a genuinely new lesson still gets proposed. Matching runs
        both ways because the scorer weighs shared words against its query side,
        and a reworded lesson may be the longer or the shorter text.
        """
        text = (text or "").strip()
        if len(text) < 4:
            return False
        normalized = normalize(text)
        with self._lock:
            exact = self._db.execute(
                "SELECT 1 FROM lesson_rejections WHERE text_key = ?",
                (make_key("rejected", text),),
            ).fetchone()
            if exact:
                return True
            rows = self._db.execute(
                "SELECT text FROM lesson_rejections ORDER BY rejected DESC LIMIT 200"
            ).fetchall()
        for row in rows:
            other = normalize(row["text"])
            if other and SequenceMatcher(None, normalized, other).ratio() >= REJECTION_SIMILARITY:
                return True
            found = match(text, row["text"], lexicon=self.lexicon)
            reverse = match(row["text"], text, lexicon=self.lexicon)
            if max(found.score, reverse.score) >= REJECTION_SEMANTIC_SCORE:
                return True
        return False

    def edit(self, ident: int, text: str) -> Optional[Memory]:
        """Rewrite a note before accepting it. The key follows the new text."""
        text = (text or "").strip()
        if len(text) < 4:
            return None
        with self._lock, self._db:
            row = self._db.execute("SELECT kind FROM memories WHERE id = ?", (int(ident),)).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE memories SET text = ?, key = ?, status = 'accepted' WHERE id = ?",
                (text, make_key(row["kind"], text), int(ident)),
            )
        return self.get(int(ident))

    # -- reading ---------------------------------------------------------
    def get(self, ident: int) -> Optional[Memory]:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (int(ident),)).fetchone()
        return self._row(row) if row else None

    def all(
        self,
        limit: int = 50,
        kinds: Optional[Sequence[str]] = None,
        include_pending: bool = False,
    ) -> List[Memory]:
        sql = "SELECT * FROM memories"
        clauses: List[str] = []
        params: List[object] = []
        if not include_pending:
            clauses.append("status = 'accepted'")
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def recall(
        self,
        query: str = "",
        limit: int = 6,
        kinds: Optional[Sequence[str]] = None,
    ) -> List[Memory]:
        """Keyword-overlap ranking. Deterministic and inspectable."""
        rows = self.all(limit=500, kinds=kinds)
        if not rows:
            return []
        if not (query or "").strip():
            return rows[:limit]
        scored: List[Memory] = []
        for row in rows:
            # Meaning first: stemming, the concept lexicon, then character
            # n-grams. "how do I check the build" finds a note about `make test`.
            found = match(query, f"{row.kind} {row.text}", lexicon=self.lexicon)
            if not found.useful:
                continue
            weight = KIND_WEIGHT.get(row.kind, 1.0)
            recency = 1.0 / (1.0 + row.age_days / 30.0)
            row.score = found.score * weight + 0.20 * row.confidence + 0.10 * recency
            row.reason = found.reason()
            scored.append(row)
        scored.sort(key=lambda item: (item.score, item.created), reverse=True)
        return scored[:limit]

    def prompt_block(self, query: str, limit: int = 6) -> str:
        """Formatted memory for an agent prompt, with provenance intact."""
        items = self.recall(query, limit=limit)
        pinned = self.all(limit=40, kinds=self.ALWAYS_KINDS)[:3]
        merged: List[Memory] = []
        seen = set()
        for item in pinned + items:
            if item.id in seen:
                continue
            seen.add(item.id)
            merged.append(item)
        items = merged[: max(limit, 4)]
        if not items:
            return ""
        lines = [
            "WHAT YOU ALREADY KNOW (your own notes from earlier sessions)",
            "These are yours, not ground truth: they can be stale or wrong. Verify before relying,",
            "and correct them with `remember` when you learn better.",
        ]
        for item in items:
            lines.append("- " + item.line())
        return "\n".join(lines)

    def touch(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock, self._db:
            self._db.executemany("UPDATE memories SET uses = uses + 1 WHERE id = ?", [(i,) for i in ids])

    def stats(self) -> Dict[str, object]:
        with self._lock:
            total = self._db.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE status = 'accepted'"
            ).fetchone()["c"]
            pending = self._db.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE status = 'pending'"
            ).fetchone()["c"]
            rejected = self._db.execute(
                "SELECT COUNT(*) AS c FROM lesson_rejections"
            ).fetchone()["c"]
            by_kind = {
                row["kind"]: row["c"]
                for row in self._db.execute(
                    "SELECT kind, COUNT(*) AS c FROM memories WHERE status = 'accepted' GROUP BY kind"
                ).fetchall()
            }
        return {
            "total": total,
            "pending": pending,
            "rejected": rejected,
            "by_kind": by_kind,
            "path": str(self.path),
        }

    def export(self) -> str:
        return json.dumps([self._as_dict(row) for row in self.all(limit=10000)], indent=2)

    def export_sync(self, limit: int = 5000) -> List[Dict[str, object]]:
        """Accepted notes in a stable, shareable shape. Never includes pending notes."""
        items: List[Dict[str, object]] = []
        for row in self.all(limit=limit):
            data = dict(self._as_dict(row))
            data["key"] = row.key
            items.append(data)
        return items

    def import_sync(self, entries: Iterable[Any], source: str = "sync") -> Dict[str, int]:
        """Quarantine incoming notes until a human accepts them.

        Remote keys are deliberately not trusted: deduplication uses the local
        kind+text key, so a peer cannot overwrite an unrelated accepted note by
        guessing its key. Rejected wording stays rejected.
        """
        counts = {"received": 0, "new": 0, "duplicates": 0, "skipped": 0, "rejected": 0}
        source = (source or "sync").strip()[:180] or "sync"
        for raw in entries:
            counts["received"] += 1
            if not isinstance(raw, dict):
                counts["skipped"] += 1
                continue
            text = str(raw.get("text") or "").strip()
            if len(text) < 8 or len(text) > 4000:
                counts["skipped"] += 1
                continue
            kind = str(raw.get("kind") or "fact")
            if kind not in KINDS:
                kind = "fact"
            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.6)))
            except (TypeError, ValueError):
                confidence = 0.6
            if self.was_rejected(text):
                counts["rejected"] += 1
                continue
            new_id = self.remember(
                text,
                kind=kind,
                source=source,
                confidence=confidence,
                key=make_key(kind, text),
                status="pending",
                verify_command=str(raw.get("verify_command") or "")[:300],
                verify_output=str(raw.get("verify_output") or ""),
            )
            if new_id is None:
                counts["duplicates"] += 1
            else:
                counts["new"] += 1
        return counts

    @staticmethod
    def _as_dict(row: Memory) -> Dict[str, object]:
        return {
            "id": row.id,
            "kind": row.kind,
            "text": row.text,
            "source": row.source,
            "confidence": round(row.confidence, 2),
            "created": row.created,
            "uses": row.uses,
            "status": row.status,
            "verify_command": row.verify_command,
            "verify_output": row.verify_output,
        }

    @staticmethod
    def _rejection(row: sqlite3.Row) -> Rejection:
        return Rejection(
            id=int(row["id"]),
            kind=str(row["kind"] or "lesson"),
            text=str(row["text"] or ""),
            key=str(row["lesson_key"] or ""),
            source=str(row["source"] or ""),
            confidence=float(row["confidence"] or 0.0),
            rejected=float(row["rejected"] or 0.0),
            count=int(row["count"] or 1),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            key=row["key"] or "",
            source=row["source"] or "",
            confidence=float(row["confidence"] or 0.0),
            created=float(row["created"] or 0.0),
            uses=int(row["uses"] or 0),
            status=str(row["status"] or "accepted"),
            verify_command=str(row["verify_command"] or ""),
            verify_output=str(row["verify_output"] or ""),
        )

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass
