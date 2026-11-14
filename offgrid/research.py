"""Deep research from open-licensed sources, with human-approved offline saves.

The network is deliberately narrow: only the public MediaWiki APIs for the
Wikimedia projects below are queried.  There is no arbitrary URL fetcher and no
HTML scraping.  Every result keeps its project, article URL, revision id,
licence, retrieval time and a SHA-256 of the text we received.  That is
source verification, not a truth oracle; the human chooses what is worth
keeping offline.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .library import Library
from .private import ensure_private_dir, harden_file, write_private_text
from .semantics import words as meaning_words

UA = "offgrid-research/0.1 (+offline; human-approved saves)"
MAX_QUERY = 300
MAX_TEXT = 120_000
MAX_RESPONSE = 8 * 1024 * 1024
MAX_ITEMS = 8
CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
SLUG_RE = re.compile(r"[^a-z0-9]+")


class ResearchError(Exception):
    """A user-facing research error with no credentials or stack noise."""


@dataclass(frozen=True)
class ResearchSource:
    id: str
    title: str
    api: str
    article_base: str
    license: str
    license_url: str
    description: str


SOURCES: Tuple[ResearchSource, ...] = (
    ResearchSource(
        id="wikipedia",
        title="Wikipedia",
        api="https://en.wikipedia.org/w/api.php",
        article_base="https://en.wikipedia.org",
        license="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        description="General reference articles, openly licensed and revisioned.",
    ),
    ResearchSource(
        id="wikibooks",
        title="Wikibooks",
        api="https://en.wikibooks.org/w/api.php",
        article_base="https://en.wikibooks.org",
        license="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        description="Open textbooks and practical guides.",
    ),
    ResearchSource(
        id="wikiversity",
        title="Wikiversity",
        api="https://en.wikiversity.org/w/api.php",
        article_base="https://en.wikiversity.org",
        license="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        description="Open learning resources and course material.",
    ),
)

SOURCE_BY_ID = {source.id: source for source in SOURCES}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ResearchError(f"research source redirected to {newurl}; redirects are refused")


def _source_url(source: ResearchSource, title: str) -> str:
    return f"{source.article_base}/wiki/{urllib.parse.quote(title.replace(' ', '_'), safe='')}"


def _safe_slug(title: str) -> str:
    slug = SLUG_RE.sub("-", (title or "").lower()).strip("-")
    return slug[:60] or "research"


def _iso_time(seconds: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds if seconds is not None else time.time()))


def _clean_fragment(text: str, limit: int = 700) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 3] + "..."


def _query_terms(query: str) -> List[str]:
    seen: List[str] = []
    for word in meaning_words(query):
        if len(word) >= 3 and word not in seen:
            seen.append(word)
    return seen


@dataclass
class ResearchItem:
    id: str
    source_id: str
    source_title: str
    title: str
    pageid: int
    revision: int
    revision_sha1: str
    revision_url: str
    url: str
    license: str
    license_url: str
    retrieved_at: float
    sha256: str
    text: str
    excerpt: str
    verification: List[str] = field(default_factory=list)
    corroborated_by: List[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def verified(self) -> bool:
        return bool(self.revision and self.sha256 and self.verification)

    def as_dict(self, include_text: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "source": self.source_id,
            "source_title": self.source_title,
            "title": self.title,
            "pageid": self.pageid,
            "revision": self.revision,
            "revision_sha1": self.revision_sha1,
            "revision_url": self.revision_url,
            "url": self.url,
            "license": self.license,
            "license_url": self.license_url,
            "retrieved_at": _iso_time(self.retrieved_at),
            "sha256": self.sha256,
            "chars": len(self.text),
            "excerpt": self.excerpt,
            "verification": list(self.verification),
            "verified": self.verified,
            "corroborated_by": list(self.corroborated_by),
            "truncated": self.truncated,
        }
        if include_text:
            data["text"] = self.text
        return data

    def markdown(self, query: str) -> str:
        metadata: Dict[str, Any] = {
            "offgrid_research": 1,
            "title": self.title,
            "source": self.source_id,
            "source_title": self.source_title,
            "source_url": self.url,
            "revision": self.revision,
            "revision_url": self.revision_url,
            "license": self.license,
            "license_url": self.license_url,
            "retrieved": _iso_time(self.retrieved_at),
            "sha256": self.sha256,
            "revision_sha1": self.revision_sha1,
            "query": query,
            "verification": list(self.verification),
            "truncated": self.truncated,
        }
        lines = ["---"]
        lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items())
        lines.extend(
            [
                "---",
                "",
                f"# {self.title}",
                "",
                f"> Offline research copy. Source: [{self.source_title}]({self.url}) · "
                f"licence {self.license} · revision {self.revision}.",
                "> This is a verified copy of a source, not a guarantee that every claim is true.",
                "",
                self.text.strip(),
            ]
        )
        return "\n".join(lines).rstrip() + "\n"


class ResearchCache:
    """Private disk cache for successful, already-validated API responses."""

    version = 1

    def __init__(
        self,
        root: Path,
        ttl: float = 3600.0,
        enabled: bool = True,
        refresh: bool = False,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.root = Path(root)
        try:
            self.ttl = max(0.0, min(float(ttl), 604800.0))
        except (TypeError, ValueError):
            self.ttl = 3600.0
        self.enabled = bool(enabled) and bool(str(root or "").strip()) and self.ttl > 0
        self.refresh = bool(refresh)
        self.clock = clock or time.time
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.errors = 0

    def stats(self) -> Dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "errors": self.errors,
        }

    @staticmethod
    def _source_key(source_id: str) -> Optional[str]:
        return source_id if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", source_id or "") else None

    def path_for(self, source_id: str, api: str, params: Dict[str, str]) -> Optional[Path]:
        source_key = self._source_key(source_id)
        if source_key is None:
            return None
        material = json.dumps(
            {"api": api, "params": params}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        return self.root / source_key / f"{digest}.json"

    def get(
        self,
        source_id: str,
        api: str,
        params: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled or self.refresh:
            self.misses += 1
            return None
        path = self.path_for(source_id, api, params)
        if path is None or not path.is_file():
            self.misses += 1
            return None
        try:
            if path.stat().st_size > MAX_RESPONSE + 1024:
                self.misses += 1
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        if not isinstance(entry, dict) or entry.get("version") != self.version:
            self.misses += 1
            return None
        if entry.get("api") != api or entry.get("params") != params:
            self.misses += 1
            return None
        try:
            age = self.clock() - float(entry.get("fetched") or 0)
        except (TypeError, ValueError):
            self.misses += 1
            return None
        if age < 0 or age > self.ttl:
            self.misses += 1
            return None
        data = entry.get("data")
        if not isinstance(data, dict):
            self.misses += 1
            return None
        self.hits += 1
        return data

    def put(
        self,
        source_id: str,
        api: str,
        params: Dict[str, str],
        data: Dict[str, Any],
    ) -> bool:
        if not self.enabled or not isinstance(data, dict):
            return False
        path = self.path_for(source_id, api, params)
        if path is None:
            return False
        entry = {
            "version": self.version,
            "source": source_id,
            "api": api,
            "params": params,
            "fetched": self.clock(),
            "data": data,
        }
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            ensure_private_dir(path.parent)
            write_private_text(temporary, json.dumps(entry, ensure_ascii=False))
            temporary.replace(path)
            harden_file(path)
            self.writes += 1
            return True
        except (OSError, TypeError, ValueError):
            self.errors += 1
            try:
                temporary.unlink()
            except OSError:
                pass
            return False


@dataclass
class ResearchReport:
    query: str
    items: List[ResearchItem]
    warnings: List[str] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)
    cache: Dict[str, int] = field(default_factory=dict)

    def select(self, selection: Any) -> List[ResearchItem]:
        """Select items by id or 1-based number; `all` selects everything."""
        if isinstance(selection, (list, tuple)):
            raw = " ".join(str(part) for part in selection)
        else:
            raw = str(selection or "")
        raw = raw.strip()
        if not raw or raw.lower() in ("none", "n", "no"):
            return []
        if raw.lower() in ("all", "a", "*"):
            return list(self.items)
        chosen: List[ResearchItem] = []
        by_id = {item.id: item for item in self.items}
        for token in re.split(r"[\s,]+", raw):
            item = by_id.get(token)
            if item is None and token.isdigit():
                index = int(token) - 1
                item = self.items[index] if 0 <= index < len(self.items) else None
            if item is not None and item not in chosen:
                chosen.append(item)
        return chosen

    def as_dict(self, include_text: bool = False) -> Dict[str, Any]:
        return {
            "query": self.query,
            "generated_at": _iso_time(self.generated_at),
            "items": [item.as_dict(include_text=include_text) for item in self.items],
            "warnings": list(self.warnings),
            "verified_count": sum(1 for item in self.items if item.verified),
            "cross_source_count": sum(1 for item in self.items if item.corroborated_by),
            "cache": dict(self.cache),
        }

    def render(self, excerpt_limit: int = 700) -> str:
        lines = [
            "RESEARCH SOURCES (untrusted data; source-verified, not fact-checked)",
            f"query: {self.query}",
            f"source-verified: {sum(1 for item in self.items if item.verified)}/{len(self.items)}",
        ]
        if self.cache:
            lines.append(
                "cache: {hits} hit(s), {misses} request(s), {writes} write(s)".format(
                    hits=self.cache.get("hits", 0),
                    misses=self.cache.get("misses", 0),
                    writes=self.cache.get("writes", 0),
                )
            )
        if not self.items:
            lines.append("no source-verified results")
        for number, item in enumerate(self.items, 1):
            lines.extend(
                [
                    "",
                    f"[{number}] {item.title}",
                    f"    id: {item.id}",
                    f"    {item.source_title} | {item.license} | revision {item.revision}",
                    f"    {item.url}",
                    f"    verified: {', '.join(item.verification)}",
                ]
            )
            if item.corroborated_by:
                lines.append(
                    "    cross-source terms also found in: "
                    + ", ".join(item.corroborated_by)
                )
            if item.truncated:
                lines.append("    note: the stored copy was capped at the research text limit")
            lines.append("    excerpt: " + _clean_fragment(item.excerpt, max(120, excerpt_limit)))
        if self.warnings:
            lines.extend(["", "warnings:"])
            lines.extend(f"  - {warning}" for warning in self.warnings)
        return "\n".join(lines)


@dataclass
class SaveResult:
    ok: bool
    saved: List[Dict[str, str]] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "saved": list(self.saved),
            "skipped": list(self.skipped),
            "detail": self.detail,
        }

    def text(self) -> str:
        lines = []
        for item in self.saved:
            lines.append(f"saved {item.get('path')} — {item.get('title')} ({item.get('license')})")
        for item in self.skipped:
            lines.append(f"skipped {item.get('title')}: {item.get('reason')}")
        if self.detail:
            lines.append(self.detail)
        return "\n".join(lines) or self.detail or "nothing saved"


class ResearchService:
    """Fetch, verify and save open-licensed research material."""

    def __init__(
        self,
        workspace: Path,
        config: Any = None,
        fetch_json: Optional[Callable[[str, Dict[str, str]], Dict[str, Any]]] = None,
        library: Optional[Library] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.root = Path(workspace)
        self.config = config
        self.fetch_json = fetch_json or self._http_json
        self.library = library
        self.log = log or (lambda _message: None)
        try:
            self.timeout = max(5.0, min(float(getattr(config, "research_timeout", 20) or 20), 60.0))
        except (TypeError, ValueError):
            self.timeout = 20.0
        self.online = bool(getattr(config, "research_online", False))
        cache_setting = str(getattr(config, "research_cache", ".offgrid/research-cache") or "")
        cache_root = Path(cache_setting).expanduser() if cache_setting else Path("")
        if cache_setting and not cache_root.is_absolute():
            cache_root = self.root / cache_root
        self.cache = ResearchCache(
            cache_root,
            ttl=getattr(config, "research_cache_ttl", 3600.0),
            enabled=bool(cache_setting),
            refresh=bool(getattr(config, "research_refresh", False)),
        )
        self.last_report: Optional[ResearchReport] = None

    # -- network guard ---------------------------------------------------
    def _guard(self) -> None:
        if not self.online:
            raise ResearchError(
                "online research is disabled; run with --online (or set research_online in the config)"
            )
        if getattr(self.config, "offline", False) or getattr(self.config, "network", "allow") == "deny":
            raise ResearchError("research needs networking, but OffGrid is running offline")

    def sources(self) -> List[ResearchSource]:
        return list(SOURCES)

    @staticmethod
    def _validate_source(source: ResearchSource) -> None:
        parsed = urllib.parse.urlsplit(source.api)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ResearchError(f"unsafe research source URL for {source.id}")

    def _http_json(self, url: str, params: Dict[str, str]) -> Dict[str, Any]:
        self._validate_source_url(url)
        target = url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
            },
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                content_type = response.headers.get_content_type()
                if content_type not in ("application/json", "text/json"):
                    raise ResearchError(f"research source returned {content_type}, not JSON")
                body = response.read(MAX_RESPONSE + 1)
        except ResearchError:
            raise
        except urllib.error.HTTPError as exc:
            raise ResearchError(f"research source returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ResearchError(f"could not reach the research source: {exc}") from None
        if len(body) > MAX_RESPONSE:
            raise ResearchError("research source response is too large")
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            raise ResearchError("research source returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ResearchError("research source returned an unexpected response")
        return data

    def _validate_source_url(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ResearchError("research sources must use HTTPS")
        allowed = {urllib.parse.urlsplit(source.api).hostname for source in SOURCES}
        if parsed.hostname not in allowed:
            raise ResearchError(f"research source {parsed.hostname} is not allowlisted")

    # -- fetching --------------------------------------------------------
    def _request(self, source: ResearchSource, params: Dict[str, str]) -> Dict[str, Any]:
        self._validate_source(source)
        cached = self.cache.get(source.id, source.api, params)
        if cached is not None:
            return cached
        try:
            data = self.fetch_json(source.api, params)
        except ResearchError:
            raise
        except Exception as exc:
            raise ResearchError(f"{source.title} request failed: {exc}") from None
        if not isinstance(data, dict):
            raise ResearchError(f"{source.title} returned an unexpected response")
        error = data.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "error")[:80]
            info = str(error.get("info") or "").strip()[:200]
            raise ResearchError(f"{source.title} API error: {code}{': ' + info if info else ''}")
        self.cache.put(source.id, source.api, params, data)
        return data

    def _search(self, source: ResearchSource, query: str, limit: int) -> List[Dict[str, Any]]:
        data = self._request(
            source,
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(max(1, min(limit, 10))),
                "srprop": "snippet|wordcount|timestamp",
                "format": "json",
                "formatversion": "2",
            },
        )
        results = (data.get("query") or {}).get("search") or []
        if not isinstance(results, list):
            return []
        cleaned: List[Dict[str, Any]] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            pageid = result.get("pageid")
            title = str(result.get("title") or "").strip()
            if not isinstance(pageid, int) or not title:
                continue
            snippet = html.unescape(re.sub(r"<[^>]+>", "", str(result.get("snippet") or "")))
            cleaned.append(
                {
                    "pageid": pageid,
                    "title": title,
                    "snippet": re.sub(r"\s+", " ", snippet).strip(),
                }
            )
        return cleaned

    @staticmethod
    def _page(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pages = (data.get("query") or {}).get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())
        if not isinstance(pages, list) or not pages:
            return None
        page = pages[0]
        return page if isinstance(page, dict) else None

    def _fetch_page(
        self,
        source: ResearchSource,
        candidate: Dict[str, Any],
        query: str,
    ) -> Optional[ResearchItem]:
        data = self._request(
            source,
            {
                "action": "query",
                "prop": "extracts|info|revisions",
                "explaintext": "1",
                "exsectionformat": "plain",
                "inprop": "url",
                "rvprop": "ids|timestamp|sha1",
                "rvslots": "main",
                "pageids": str(candidate["pageid"]),
                "format": "json",
                "formatversion": "2",
            },
        )
        page = self._page(data)
        if not page or page.get("missing"):
            return None
        text = str(page.get("extract") or "").strip()
        if not text:
            return None
        title = str(page.get("title") or candidate.get("title") or "").strip()
        revisions = page.get("revisions") or []
        revision_data = revisions[0] if isinstance(revisions, list) and revisions else {}
        if not isinstance(revision_data, dict):
            revision_data = {}
        revision = int(revision_data.get("revid") or 0)
        if not revision:
            return None
        revision_sha1 = str(revision_data.get("sha1") or "")
        fullurl = str(page.get("fullurl") or "").strip()
        if fullurl:
            parsed = urllib.parse.urlsplit(fullurl)
            if parsed.scheme != "https" or parsed.hostname != urllib.parse.urlsplit(source.article_base).hostname:
                fullurl = ""
        if not fullurl:
            fullurl = _source_url(source, title)
        truncated = len(text) > MAX_TEXT
        if truncated:
            text = text[:MAX_TEXT].rstrip()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        revision_url = f"{source.article_base}/w/index.php?oldid={revision}"
        verification = [
            "official MediaWiki API",
            "HTTPS source",
            f"revision {revision}",
            f"licence {source.license}",
            f"SHA-256 {digest[:12]}…",
        ]
        excerpt = _excerpt(text, query)
        return ResearchItem(
            id=f"{source.id}:{candidate['pageid']}:{revision}",
            source_id=source.id,
            source_title=source.title,
            title=title,
            pageid=int(candidate["pageid"]),
            revision=revision,
            revision_sha1=revision_sha1,
            revision_url=revision_url,
            url=fullurl,
            license=source.license,
            license_url=source.license_url,
            retrieved_at=time.time(),
            sha256=digest,
            text=text,
            excerpt=excerpt,
            verification=verification,
            truncated=truncated,
        )

    def investigate(
        self,
        query: str,
        limit: int = 6,
        source_ids: Optional[Sequence[str]] = None,
    ) -> ResearchReport:
        self._guard()
        query = re.sub(r"\s+", " ", str(query or "")).strip()
        if not query:
            raise ResearchError("research query is empty")
        if len(query) > MAX_QUERY:
            query = query[:MAX_QUERY].rstrip()
        try:
            count = max(1, min(int(limit), MAX_ITEMS))
        except (TypeError, ValueError):
            count = 6
        if source_ids:
            sources = [SOURCE_BY_ID[ident] for ident in source_ids if ident in SOURCE_BY_ID]
            if not sources:
                raise ResearchError("no known research source selected")
        else:
            sources = list(SOURCES)
        report = ResearchReport(query=query, items=[])
        cache_before = self.cache.stats()
        per_source = max(1, (count + len(sources) - 1) // len(sources))
        seen: set = set()
        for source in sources:
            try:
                candidates = self._search(source, query, per_source)
            except ResearchError as exc:
                report.warnings.append(str(exc))
                continue
            for candidate in candidates:
                if len(report.items) >= count:
                    break
                try:
                    item = self._fetch_page(source, candidate, query)
                except ResearchError as exc:
                    report.warnings.append(str(exc))
                    continue
                if item is None or item.id in seen:
                    continue
                seen.add(item.id)
                report.items.append(item)
        self._corroborate(report)
        cache_after = self.cache.stats()
        report.cache = {
            key: cache_after[key] - cache_before.get(key, 0)
            for key in ("hits", "misses", "writes", "errors")
        }
        if not report.items:
            report.warnings.append(
                "no usable open-licensed source was found; nothing can be saved from this query"
            )
        self.last_report = report
        return report

    @staticmethod
    def _corroborate(report: ResearchReport) -> None:
        terms = set(_query_terms(report.query))
        if len(terms) < 2:
            return
        for item in report.items:
            for other in report.items:
                if other.id == item.id or other.source_id == item.source_id:
                    continue
                other_terms = set(_query_terms(other.title + " " + other.text[:8000]))
                if len(terms & other_terms) >= 2 and other.id not in item.corroborated_by:
                    item.corroborated_by.append(other.id)

    # -- saving ----------------------------------------------------------
    def _library(self) -> Library:
        if self.library is not None:
            return self.library
        root = self.root / str(getattr(self.config, "library_dir", "offgrid-library") or "offgrid-library")
        return Library(root)

    @staticmethod
    def _confirm(confirm: Optional[Callable[[str], bool]], message: str) -> bool:
        if confirm is None:
            return False
        try:
            return bool(confirm(message))
        except Exception:
            return False

    def save(
        self,
        report: ResearchReport,
        selection: Any,
        confirm: Optional[Callable[[str], bool]] = None,
        category: str = "research",
    ) -> SaveResult:
        chosen = report.select(selection)
        if not chosen:
            return SaveResult(False, detail="no matching research items selected; nothing was saved")
        if not CATEGORY_RE.fullmatch(str(category or "")):
            return SaveResult(False, detail="category must be lower-case letters, digits, _ or -")
        message = "Save these researched sources offline?\n" + "\n".join(
            f"  - {item.title} ({item.source_title}, {item.license})" for item in chosen
        )
        if not self._confirm(confirm, message):
            return SaveResult(False, detail="not saved; human approval is required")
        library = self._library()
        saved: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []
        for item in chosen:
            stem = _safe_slug(item.title)
            relative = f"{category}/{stem}-{item.sha256[:10]}.md"
            candidate = library.root / relative
            if candidate.exists():
                skipped.append(
                    {"id": item.id, "title": item.title, "reason": f"already exists: {relative}"}
                )
                continue
            try:
                path = library.save_text(relative, item.markdown(report.query))
            except (OSError, ValueError) as exc:
                skipped.append({"id": item.id, "title": item.title, "reason": str(exc)})
                continue
            saved.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "path": str(path),
                    "url": item.url,
                    "license": item.license,
                }
            )
        detail = ""
        if skipped:
            detail = f"{len(skipped)} item(s) were not saved"
        return SaveResult(bool(saved), saved, skipped, detail)

    def save_last(
        self,
        selection: Any,
        confirm: Optional[Callable[[str], bool]] = None,
        category: str = "research",
    ) -> SaveResult:
        if self.last_report is None:
            return SaveResult(False, detail="run research_query before research_save")
        return self.save(self.last_report, selection, confirm=confirm, category=category)


def _excerpt(text: str, query: str, width: int = 700) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    lowered = normalized.lower()
    positions = [
        lowered.find(word.lower())
        for word in _query_terms(query)
        if lowered.find(word.lower()) >= 0
    ]
    start = max(0, min(positions) - 200) if positions else 0
    return normalized[start : start + width].strip()


def load_sources() -> List[Dict[str, str]]:
    return [
        {
            "id": source.id,
            "title": source.title,
            "license": source.license,
            "license_url": source.license_url,
            "description": source.description,
        }
        for source in SOURCES
    ]
