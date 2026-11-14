# Expensive query cache

OffGrid's semantic search is deterministic but not free: a library search may
read text files and score them line by line, and memory recall scores up to 500
notes on every prompt. Repeated identical queries reuse a small in-process cache
instead of doing that work again.

## What is cached

- **Library search** results, keyed by normalised query, result limit and a
  cheap snapshot of every library file's path, size and modification time.
- **Memory recall** results, keyed by query, limit, kind filter and cache
  generation.
- **Rejection searches** used by `memory forgive`, with the same memory cache
  and invalidation rules.

The cache holds at most 24 library queries and 32 memory queries. Entries are
least-recently-used, so a long session cannot grow memory without bound.

## Invalidation

- The library cache is invalidated when a file is added, removed or changed,
  including when `library.index()` rewrites `index.json`.
- The memory cache is cleared by `remember`, `forget`, `accept`, `reject`,
  `edit` and `forgive`. Those are the operations that can change what recall
  returns. The key also includes SQLite's `data_version`, so a commit made by
  another process invalidates the next read without a restart.
- `touch` only increments usage counters and does not invalidate recall results;
  usage counters are not part of ranking or the rendered note.

A new cache entry is never written over a newer generation, so a write racing
with a search cannot leave a stale answer behind.

## Scope and privacy

The cache is **process-local and in memory only**. It does not survive a
restart and is never written to `.offgrid` or anywhere else. Library snippets
and memory text stay in the running process; no network request is made. Each
process, including a later `offgrid` command, starts with an empty cache.

This is a cache of local query results, not a cache of model responses. Model
answers are not reused, so a changed prompt or workspace cannot receive a
previous run's completion.

## Tests

The tests verify LRU eviction and counters, that a repeated library search skips
the text scan, that a caller mutating a returned list cannot corrupt the cached
value, that library edits invalidate the cache, and that memory writes and
pending-note acceptance invalidate recall and rejection results.
