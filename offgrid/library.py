"""The offline library: documents you carry with you.

An agent with no internet still needs reference material. This module downloads
and indexes real, openly licensed collections - survival and practical medicine,
offline coding documentation, a medical encyclopaedia - into a folder you can
search with the `library_search` tool.

Sources are resolved from live directory listings rather than hardcoded
date-stamped filenames, because Kiwix re-publishes monthly.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .semantics import Lexicon, match, words as meaning_words

UA = "offgrid-library/0.1 (+offline)"
KIWIX = "https://download.kiwix.org/zim"
TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".rst", ".html", ".htm", ".json", ".csv", ".py", ".js")
MAX_READ_BYTES = 2 * 1024 * 1024
MAX_SCAN_FILES = 200
MAX_SCAN_LINES = 4000
MAX_SCAN_LINE_BUDGET = 20000


@dataclass
class LibraryEntry:
    id: str
    title: str
    category: str
    prefix: str
    directory: str
    description: str
    license: str
    approx_gb: float = 0.0
    kind: str = "kiwix"

    @property
    def landing(self) -> str:
        return f"{KIWIX}/{self.directory}/"


# Curated and verified against the live listings: every prefix below existed on
# 2026-11-08. Sizes are approximate and only used for planning.
COLLECTIONS: List[LibraryEntry] = [
    LibraryEntry(
        id="water",
        title="Water treatment and sanitation (Zimgit)",
        category="survival",
        prefix="zimgit-water_en_",
        directory="other",
        description="Boiling, filtering, storage and disinfection of drinking water.",
        license="CC-BY-SA 4.0",
        approx_gb=0.1,
    ),
    LibraryEntry(
        id="medicine",
        title="Medicine without a pharmacy (Zimgit)",
        category="survival",
        prefix="zimgit-medicine_en_",
        directory="other",
        description="Field medicine, common infections, wound care, dosing basics.",
        license="CC-BY-SA 4.0",
        approx_gb=0.3,
    ),
    LibraryEntry(
        id="disaster",
        title="Post-disaster survival (Zimgit)",
        category="survival",
        prefix="zimgit-post-disaster_en_",
        directory="other",
        description="Shelter, food, hygiene and first steps after a disaster.",
        license="CC-BY-SA 4.0",
        approx_gb=0.1,
    ),
    LibraryEntry(
        id="appropedia",
        title="Appropedia - practical sustainability",
        category="survival",
        prefix="appropedia_en_all_maxi_",
        directory="other",
        description="Off-grid building, water, sanitation, food preservation, energy.",
        license="CC-BY-SA 4.0",
        approx_gb=2.0,
    ),
    LibraryEntry(
        id="python",
        title="Python reference (DevDocs)",
        category="coding",
        prefix="devdocs_en_python_",
        directory="devdocs",
        description="Python standard library and language reference, offline.",
        license="PSF / CC-BY",
        approx_gb=0.28,
    ),
    LibraryEntry(
        id="javascript",
        title="JavaScript reference (DevDocs)",
        category="coding",
        prefix="devdocs_en_javascript_",
        directory="devdocs",
        description="JavaScript language and browser APIs, offline.",
        license="CC-BY",
        approx_gb=0.2,
    ),
    LibraryEntry(
        id="rust",
        title="Rust reference (DevDocs)",
        category="coding",
        prefix="devdocs_en_rust_",
        directory="devdocs",
        description="Rust standard library and book, offline.",
        license="MIT / Apache-2.0",
        approx_gb=0.3,
    ),
    LibraryEntry(
        id="bash",
        title="Bash and shell reference (DevDocs)",
        category="coding",
        prefix="devdocs_en_bash_",
        directory="devdocs",
        description="Bash builtins, POSIX shell, coreutils.",
        license="CC-BY",
        approx_gb=0.05,
    ),
    LibraryEntry(
        id="medicine-wiki",
        title="Wikipedia medicine (no images)",
        category="reference",
        prefix="wikipedia_en_medicine_nopic_",
        directory="wikipedia",
        description="Medical conditions, symptoms and treatments, no images.",
        license="CC-BY-SA 4.0",
        approx_gb=1.6,
    ),
]


def _http_text(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def download(
    url: str,
    dest: Path,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    timeout: int = 120,
) -> Path:
    """Resumable download. Safe to re-run after a dropped connection."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    resume = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": UA}
    if resume:
        headers["Range"] = f"bytes={resume}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # already complete
            part.replace(dest)
            return dest
        raise
    with response:
        length = response.headers.get("Content-Length")
        total = (int(length) + resume) if length else None
        done = resume
        mode = "ab" if resume else "wb"
        with part.open(mode) as handle:
            while True:
                block = response.read(1 << 18)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
    part.replace(dest)
    return dest


class Library:
    """A folder of offline references, plus an index agents can search."""

    def __init__(
        self,
        root: Path,
        fetch_text: Optional[Callable[[str], str]] = None,
        lexicon: Optional[Lexicon] = None,
    ) -> None:
        self.root = Path(root)
        self._fetch = fetch_text or _http_text
        self.lexicon = lexicon

    # -- manifest --------------------------------------------------------
    def entries(self) -> List[LibraryEntry]:
        return list(COLLECTIONS)

    def entry(self, ident: str) -> Optional[LibraryEntry]:
        for item in COLLECTIONS:
            if item.id == ident:
                return item
        return None

    def index_path(self) -> Path:
        return self.root / "index.json"

    # -- resolution ------------------------------------------------------
    def resolve(self, entry: LibraryEntry) -> Tuple[str, str]:
        """Find the newest published file for a collection, live."""
        try:
            listing = self._fetch(entry.landing)
        except Exception as exc:
            raise RuntimeError(f"cannot read {entry.landing}: {exc}") from None
        names = re.findall(r'href="([^"]+\.zim)"', listing)
        matches = sorted(name for name in names if name.startswith(entry.prefix))
        if not matches:
            raise RuntimeError(
                f"no file starting with '{entry.prefix}' in {entry.landing} "
                "(the collection may have been renamed)"
            )
        newest = matches[-1]
        return f"{KIWIX}/{entry.directory}/{newest}", newest

    def resolve_url(self, url: str) -> Tuple[str, str]:
        name = url.rstrip("/").split("/")[-1].split("?")[0] or "document.zim"
        return url, name

    # -- download --------------------------------------------------------
    def target_for(self, entry: LibraryEntry, filename: str) -> Path:
        return self.root / entry.category / filename

    def download(
        self,
        entry: LibraryEntry,
        progress: Optional[Callable[[str, int, Optional[int]], None]] = None,
    ) -> Path:
        url, filename = self.resolve(entry)
        dest = self.target_for(entry, filename)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest

        def report(done: int, total: Optional[int]) -> None:
            if progress:
                progress(filename, done, total)

        return download(url, dest, progress=report)

    def verify(self, entry: LibraryEntry) -> Tuple[bool, str]:
        """Is this collection still published? No download, one request."""
        try:
            url, filename = self.resolve(entry)
        except Exception as exc:
            return False, str(exc)
        request = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                size = response.headers.get("Content-Length") or "?"
                return True, f"{filename} ({int(size) / 1e6:.0f} MB)" if size != "?" else filename
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 405):  # HEAD not allowed; the resolve already proved it exists
                return True, filename + " (HEAD refused, listed)"
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # -- index -----------------------------------------------------------
    def index(self) -> Dict[str, Any]:
        entries: List[Dict[str, Any]] = []
        if self.root.is_dir():
            for path in sorted(self.root.rglob("*")):
                if not path.is_file() or path.name.endswith(".part") or path == self.index_path():
                    continue
                rel = str(path.relative_to(self.root))
                meta = self._describe(path, rel)
                entries.append(meta)
        payload = {"version": 1, "updated": time.time(), "entries": entries}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.index_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass
        return payload

    def _describe(self, path: Path, rel: str) -> Dict[str, Any]:
        category = rel.split("/")[0] if "/" in rel else "misc"
        matched = None
        for entry in COLLECTIONS:
            if entry.category == category and entry.prefix in path.name:
                matched = entry
                break
        info: Dict[str, Any] = {
            "path": rel,
            "category": category,
            "title": matched.title if matched else path.stem.replace("_", " "),
            "collection": matched.id if matched else "",
            "bytes": path.stat().st_size,
            "license": matched.license if matched else "unknown",
            "searchable": path.suffix.lower() in TEXT_SUFFIXES,
        }
        if info["searchable"] and path.stat().st_size <= MAX_READ_BYTES:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                info["chars"] = len(text)
                info["head"] = re.sub(r"\s+", " ", text[:1200]).strip()
            except OSError:
                pass
        return info

    def index_data(self) -> Dict[str, Any]:
        path = self.index_path()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("entries"), list):
                    return data
            except (OSError, ValueError):
                pass
        return self.index()

    def downloaded(self) -> List[Dict[str, Any]]:
        return list(self.index_data().get("entries") or [])

    def stats(self) -> Dict[str, Any]:
        items = self.downloaded()
        total = sum(int(item.get("bytes") or 0) for item in items)
        categories: Dict[str, int] = {}
        for item in items:
            categories[item.get("category", "misc")] = categories.get(item.get("category", "misc"), 0) + 1
        return {"files": len(items), "bytes": total, "categories": categories, "root": str(self.root)}

    # -- search ----------------------------------------------------------
    def search(self, query: str, limit: int = 6) -> List[str]:
        """Meaning-first search: concepts and shared wording, not exact keywords.

        Ranking uses `semantics.match`, so "how do I make drinking water safe"
        finds a page about boiling and disinfection even though no word matches.
        """
        query = (query or "").strip()
        if not query:
            return []
        results: List[Tuple[float, str]] = []
        for item in self.downloaded():
            path = str(item.get("path") or "")
            title = str(item.get("title") or "")
            head = str(item.get("head") or "")
            body = " ".join(
                part
                for part in (title, str(item.get("collection") or ""), path.replace("/", " "), head)
                if part
            )
            found = match(query, body, lexicon=self.lexicon)
            if not found.useful:
                continue
            snippet = self._snippet(head, query) if head else ""
            if snippet:
                results.append(
                    (found.score + 0.2, f"{path} ({title}): {snippet} [{found.reason()}]")
                )
                continue
            note = "" if item.get("searchable") else " [archive: open with Kiwix, not text-searchable here]"
            results.append((found.score, f"{path} ({title}){note}"))

        for score, citation in self._scan_text(query, limit * 2):
            results.append((score, citation))
        results.sort(key=lambda pair: pair[0], reverse=True)
        return [text for _score, text in results[:limit]]

    @staticmethod
    def _snippet(text: str, query: str, width: int = 150) -> str:
        lowered = text.lower()
        positions = [
            lowered.find(word) for word in meaning_words(query) if lowered.find(word) >= 0
        ]
        if not positions:
            return ""
        start = max(0, min(positions) - 40)
        return text[start : start + width].strip()

    def _scan_text(self, query: str, limit: int) -> List[Tuple[float, str]]:
        """Line-level citations, found by meaning within each text file."""
        found: List[Tuple[float, str]] = []
        if not self.root.is_dir():
            return found
        scanned = 0
        budget = MAX_SCAN_LINE_BUDGET
        for path in sorted(self.root.rglob("*")):
            if len(found) >= limit or scanned >= MAX_SCAN_FILES or budget <= 0:
                break
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path == self.index_path():
                continue
            try:
                if path.stat().st_size > MAX_READ_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            if not match(query, text[:20000], lexicon=self.lexicon).useful:
                continue
            best: Optional[Tuple[float, int, str]] = None
            for line_no, line in enumerate(text.splitlines()[:MAX_SCAN_LINES], 1):
                budget -= 1
                if budget <= 0 or len(line.strip()) < 4:
                    break
                line_match = match(query, line, lexicon=self.lexicon)
                if line_match.useful and (best is None or line_match.score > best[0]):
                    best = (line_match.score, line_no, line)
            if best:
                rel = path.relative_to(self.root)
                found.append((best[0] + 0.15, f"{rel}:{best[1]}: {best[2].strip()[:180]}"))
        return found

    # -- reading ---------------------------------------------------------
    def read(self, path: str, offset: int = 1, limit: int = 200) -> str:
        target = (self.root / path).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError:
            raise ValueError(f"'{path}' is outside the library") from None
        if not target.is_file():
            raise ValueError(f"no such library file: {path}")
        if target.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError(
                f"{path} is a {target.suffix} archive; this tool reads text files only. "
                "ZIM archives need a ZIM reader (Kiwix Desktop)."
            )
        if target.stat().st_size > MAX_READ_BYTES:
            raise ValueError(f"{path} is too large to read whole; narrow the search instead")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(int(offset) - 1, 0)
        end = min(start + max(int(limit), 1), len(lines))
        return "\n".join(f"{index + 1}| {lines[index]}" for index in range(start, end))
