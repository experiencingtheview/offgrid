# Deep research, saved for offline use

`offgrid research` lets the team look beyond the local disk **when you explicitly
allow it**, then keep chosen copies in the same offline library that
`library_search` and `library_read` use.

It is deliberately narrower than a web browser.

## Sources

Only these open-licensed Wikimedia projects are queried, through their official
MediaWiki APIs:

| Source | Content | Licence |
|---|---|---|
| Wikipedia | general reference articles | CC BY-SA 4.0 |
| Wikibooks | open textbooks and practical guides | CC BY-SA 4.0 |
| Wikiversity | open learning resources and course material | CC BY-SA 4.0 |

No arbitrary URL is accepted. The source list is fixed in code, HTTPS is
required, redirects are refused, response types and sizes are checked, and the
text comes from the API rather than scraped HTML. That avoids turning the
research tool into an SSRF or scraping surface.

## What "verified" means here

Every result records:

- the project and article URL;
- the article's revision id and a link to that revision;
- the open licence and licence URL;
- when it was retrieved;
- a SHA-256 of the text OffGrid received;
- the API and HTTPS checks that succeeded.

This is **source verification**: we can show where the copy came from and that
it is the revision we recorded. It is **not a truth verdict**. Wikipedia and the
other projects can be incomplete, outdated or wrong. When results from different
projects share query terms, the report labels that as a cross-source hint; it is
not independent fact-checking. The human decides what is worth keeping.

## Command line

```console
offgrid research "solar panel maintenance" --online
offgrid research "water disinfection" --online --limit 4
offgrid research "water disinfection" --online --source wikibooks
offgrid research "solar panel maintenance" --online --save 1 3
offgrid research "solar panel maintenance" --online --json
```

- `--online` is required. `--offline` always wins and refuses the operation.
- `--limit` is 1–8 results (default 6).
- `--source` can be repeated to restrict the search.
- Without `--save`, the terminal prints the candidates and asks which result
  numbers or ids to keep. Enter nothing to save nothing.
- `--save` is the explicit non-interactive form. It accepts result numbers from
  the report or full ids. `--save all` saves every candidate, deliberately.
- `--json` emits the report; when combined with `--save`, it emits both the
  report and the save result.

Nothing is saved automatically. The command exits `2` when online research is
not enabled or the network is denied, and `1` when there are no usable results
or a requested save failed. Individual source failures are warnings so the
other selected projects can still contribute.

## API response cache

Successful search and article responses are cached so that repeating a query
does not make the same requests again. The cache lives in
`.offgrid/research-cache` by default, uses `0700` directories and `0600` files,
and stores only JSON that already passed the source, size, type and API-error
checks. Failed requests are never cached.

- `--research-cache DIR` chooses another directory. An empty
  `research_cache` in config disables the cache.
- `--research-cache-ttl SECONDS` controls freshness; the default is one hour,
  `0` disables the cache, and the maximum is seven days.
- `--refresh-research` ignores existing entries and fetches fresh responses.
- `OFFGRID_RESEARCH_CACHE`, `OFFGRID_RESEARCH_CACHE_TTL` and
  `OFFGRID_RESEARCH_REFRESH=1` set the same values.

Cache keys are SHA-256 hashes of the source API and request parameters, so the
query text is not exposed in cache filenames. Entries are written atomically;
corrupt, oversized, mismatched or expired entries are ignored. The report shows
how many responses came from the cache. A cache hit still requires `--online`;
`--offline` and `"network": "deny"` refuse research rather than serving cached
material.

## Agent workflow

When the run is started with `--online`, the team has two tools:

- `research_query` fetches candidate sources and returns ids, URLs, revisions,
  licences, verification details and short excerpts. It saves nothing.
- `research_save` accepts ids from the latest query. It asks the human to
  approve the batch before writing anything. If no human is available, it
  refuses.

The model is asked to propose a shortlist based on the user's request, not to
save everything. Source text is untrusted data: it may contain instructions or
claims, which must be reported rather than followed. The model must not call a
statement "verified true" merely because it came from a retrieved page.

Outside research is off by default for agent runs. Enable it per run:

```console
offgrid run "research current solar panel maintenance guidance and save what I approve" --online
```

It can also be enabled in `.offgrid.json`:

```json
{
  "research_online": true,
  "research_timeout": 20,
  "research_cache": ".offgrid/research-cache",
  "research_cache_ttl": 3600
}
```

Or through `OFFGRID_RESEARCH_ONLINE=1` and `OFFGRID_RESEARCH_TIMEOUT`. The
`--offline` flag and `"network": "deny"` still block every request.

## Where saved notes go

Approved results are written as Markdown under:

```text
offgrid-library/research/<title-slug>-<content-hash>.md
```

Each file starts with metadata and then the article text:

```markdown
---
offgrid_research: 1
title: "Solar panel"
source: "wikipedia"
source_url: "https://en.wikipedia.org/wiki/Solar_panel"
revision: 1385544154
license: "CC BY-SA 4.0"
retrieved: "..."
sha256: "..."
verification: ["official MediaWiki API", "HTTPS source", "revision ...", ...]
---
# Solar panel
> Offline research copy. Source: ...
...
```

The library index reads that front matter, so searches show the real title,
licence and source. Plain text from the article remains line-searchable by
`offgrid library search`, `library_search` and `library_read`. The copy is
self-contained apart from images and links; it does not refresh itself. Run a
new research query when currency matters.

Files are written atomically and an existing file is never overwritten. If the
same result was already saved, it is reported as skipped.

## Licensing and attribution

Wikimedia text is CC BY-SA 4.0. Keep the source URL, revision and licence with
the copy when you share or reuse it, and follow the licence's share-alike terms.
The metadata is included for that reason; stripping it makes the copy harder to
attribute and is not recommended.

## Limits and failure modes

- Only three Wikimedia projects are supported; this is not general web access.
- Research is only as good as the source. Cross-source term overlap is not
  proof, and a revision id proves identity, not correctness.
- Long articles are capped at 120,000 characters in the saved copy; the report
  says when a copy was truncated.
- A query is sent to the selected Wikimedia APIs; no account, API key, telemetry
  or local file is uploaded.
- Results can change between queries. The saved revision stays fixed, but the
  live page may not. Cached responses also age: by default a repeated query may
  reuse a response up to one hour old; use `--refresh-research` when currency
  matters.
- The project APIs can be unavailable or rate-limited; each source failure is
  reported and other sources can still succeed.
- Images, templates, references behind external links and interactive content
  are not copied.
- The research report is not a substitute for medical, legal, safety or
  professional advice.

## Tests

The research tests use a fake JSON transport, not the network. They cover source
provenance, the online/offline guard, source failures, selection, human approval,
front matter, offline library search, cache hits, TTL expiry, refresh and the
rule that failures are not cached. A real online smoke test was run manually
against Wikipedia during development, including a repeat query that was served
entirely from the cache; the first run on your network is still an operational
check of API availability and rate limits.
