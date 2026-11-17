# Changelog

All notable changes to OffGrid are recorded here.

## [Unreleased]

### Added

- Recovery restore previews now include a stable plan ID, blockers and warnings,
  the planned safety snapshot, per-asset provenance, free-space estimates,
  latest-archive selection details, and platform/pyz diagnostics. Applying a
  restore with `--expect-plan` refuses when the destination or archive has
  changed since the preview, and unmanaged destinations now require explicit
  confirmation before replacement.
- Bounded, process-local caches for expensive library and memory searches.
  Repeated identical queries skip the semantic scan; library changes are
  detected with a file-stat snapshot, memory writes clear recall/rejection
  entries, and the cache is never written to disk. See `docs/QUERY_CACHE.md`.
- Offline image optimization with `offgrid images compress`: PNG files are
  losslessly recompressed with the standard library, JPEG APPn/COM metadata can
  be removed with `--strip-metadata`, and writes are atomic and never overwrite
  by default. `--in-place` replaces originals deliberately; directory inputs
  are recursive and `--output` preserves relative paths. See `docs/IMAGES.md`.
- SQLite memory query indexes: composite status/created and
  status/kind/created indexes cover the review queue, recent listing and
  accepted-by-kind stats. Existing databases receive them during the normal
  migration; no manual command is needed. The memory store also reuses a
  bounded pool of four connections with WAL and a busy timeout; writes remain
  serialized while reads can overlap.
- Round-robin load balancing across multiple OpenAI-compatible model endpoints:
  `--endpoints URL[,URL...]`, repeatable `--endpoints` flags, the
  `endpoints` config field and `OFFGRID_ENDPOINTS`. Each model call starts at
  the next endpoint; a failed endpoint falls through to the others, and all
  endpoints failing is reported rather than hidden. Every endpoint keeps the
  existing loopback/`--allow-remote` guard, and the closing report shows the
  distribution. See `docs/LOAD_BALANCER.md`.
- Deep research with human-approved offline saves: `offgrid research "topic"
  --online` queries only the open-licensed Wikimedia APIs (Wikipedia, Wikibooks
  and Wikiversity), records the article URL, licence, revision id, retrieval
  time and SHA-256, and treats the result as source-verified rather than
  fact-checked. Nothing is saved by default; the terminal asks which results to
  keep, or `--save` names them explicitly. Agents use `research_query` and
  `research_save`; the save tool requires human confirmation. Saved notes land
  in `offgrid-library/research/`, remain searchable offline, and retain their
  attribution. `--offline` blocks the feature, and arbitrary URLs, scraped HTML
  and redirects are not supported. Successful research API responses are cached
  privately for one hour by default, with `--research-cache`,
  `--research-cache-ttl` and `--refresh-research` controls; failed responses are
  not cached. See `docs/RESEARCH.md`.
- The project site now uses Cloudflare's edge cache for the rendered page and
  its static assets, with a five-minute freshness window and a safe fallback
  when the cache is unavailable. The Worker isolate also keeps the last release
  digest in memory for five minutes, so a cache miss does not immediately go
  back to GitHub. No third-party CDN or client script was added.
- OffGrid Studio: `offgrid studio` serves one local browser app for the team,
  built from llama.cpp's MIT server web UI. It adds OffGrid branding, a live
  team/workspace panel, and an OpenAI-compatible endpoint that runs each chat
  message through the planner/coder/reviewer/tester team. The badge and Team
  panel use reduced-motion-aware loading skeletons until the roster request
  resolves. Studio search inputs update their text immediately but debounce the
  filter callback by 150 ms; clearing cancels the pending callback and Enter
  flushes it. The optional PDF.js worker is split into its own chunk and loaded
  only when a PDF is opened. Chat attachment thumbnails, PDF pages and inline
  tool images use native lazy loading and async decoding, while an opened
  full-size preview stays eager. The non-critical Studio badge and site copy
  scripts are deferred with low fetch priority. Static files are held in a process-local server
  cache with ETags, so repeat loads can return `304 Not Modified` without
  rereading the bundle. The conversation sidebar shows the newest 20 chats and
  reveals older ones in batches, keeping the active chat visible. The team
  badge and panel poll every 30 seconds but only touch the DOM when the roster,
  summary, or status actually changes. Hand-written Studio and website CSS and
  JavaScript are served from minified `.min` assets; readable sources are kept
  beside them. JSON API
  responses are gzip-compressed when the client accepts it, and gzip request
  bodies are accepted with the same 1 MB decompressed-size limit. Local-only by
  default; `--access-key` is required
  before binding to a non-loopback interface. The UI bundle and its MIT licence are in
  `offgrid/web/`.
- Flipper Zero signal support when suitable hardware is connected over USB
  serial: `offgrid signals status|info|read|scan|ir-send` and the agent tools
  `signals_status`, `signal_read`, `signal_scan` and `signal_send_ir`. RFID, NFC,
  iButton, IR receive and Sub-GHz receive are read-only; IR transmit is the only
  write, is `read-only` by default, requires the `ask`/`auto` signal policy and
  is audited before it is sent. Sub-GHz transmit, replay, cloning, emulation,
  BadUSB, GPIO writes and arbitrary Flipper commands are deliberately absent.
  Linux/macOS only; see `docs/SIGNALS.md`.
- Local device control: `.offgrid/devices.json` declares typed sensors, gauges,
  lights, relays and thermostats for Home Assistant, direct HTTP JSON or Modbus
  TCP, plus a simulator for tests. Agents gain `devices_list`, `device_read`,
  `device_diagnose` and `device_set` tools; `offgrid devices` provides the same
  operations from the CLI. Writes are read-only by default, require declared
  safe ranges, gate dangerous points, refuse public hosts and are audited
  before they are sent. See `docs/DEVICES.md` for the safety rules.

### Fixed

- The website quickstart no longer forces horizontal scrolling on narrow
  phones; its grid can shrink, code blocks scroll inside their cards, and the
  hero glow is contained at 320 px.
- A failed release-digest lookup is no longer cached as a page. The next
  request retries GitHub, and versioned Worker cache keys bypass old fallback
  entries left by earlier deployments.
- Memory review and sync import now batch SQLite reads and writes instead of
  issuing several queries per lesson. Accept, reject, forgive and bulk import
  stay within a small, bounded number of statements even when hundreds of rows
  are involved.

## [0.1.0] - 2026-11-08

### Added

- A workspace roster: `.offgrid/agents.json` (or `agents_file` in
  `.offgrid.json`) can add any number of custom agents, each with its own title,
  prompt, tool allowlist and turn budget. `"like": "coder"` clones a built-in
  role for another worker. The planner is shown the extra agents and can assign
  steps to them; `offgrid agents` lists the merged roster and reports entries it
  refused. Built-in roles, the reviewer, testing and the honesty rules are
  unchanged.
- `scripts/apply_tests_ruleset.py`: idempotently applies (or updates) the `Tests`
  ruleset that requires every Tests job on the default branch. Run it once the
  repository's GitHub plan supports rulesets for private repositories; supports
  `--dry-run` and derives `OWNER/REPO` from the origin remote.
- `offgrid sync peers list|add|remove`: manage sync peers from the CLI without
  hand-editing JSON. Add and remove accept one or more URLs in one command,
  check every URL before touching the config, and merge or remove the batch in
  a single write, preserving unrelated settings. Remove reports peers that were
  not configured and exits 1; list uses the same config file.
- `offgrid sync status` now reports the managed peer config path and validates
  each configured URL, flagging plain HTTP unless explicitly allowed and
  surfacing an unreadable config without failing the default status command.
  `--strict` exits 1 when any configured peer is invalid or the peer config
  cannot be read, for scripts and timers.
- EarthAngel's primary loyalty is now a fixed invariant. Config files,
  `OFFGRID_PRINCIPAL`, direct `Config` values, and prompt builder inputs cannot
  rename or supersede the principal; the local caretaker remains second. A
  regression test covers file, environment, override, direct-config, prompt,
  posture, and continuity-kit paths. A config file or `OFFGRID_PRINCIPAL` that
  sets `principal` now produces a startup warning on stderr, visible even with
  `--quiet` or `--json`, explaining that the value was ignored because the
  principal is fixed.
- Opt-in `offgrid sync schedule`: repeats the accepted-only push/pull exchange on
  an interval, or once with `--once` for cron/systemd. Every pass refuses to run
  when networking is denied or `--offline` is set, and each push, pull and
  scheduled pass is recorded in `.offgrid/sync.jsonl` without lesson text or the
  token.
- A public, read-only CI status badge served by a small Sites Worker and D1
  database. The Tests workflow publishes the latest `main` result without
  exposing a GitHub token, so the badge works without a GitHub sign-in.
- The Tests workflow also runs once a week on `main`, so a broken download or a
  transient outage in the secret-scan setup surfaces without waiting for the
  next push.
- The CI badge reports `stale` once its published result is more than a week
  old, instead of showing a result that no longer reflects `main`; the status
  page says so too and `status.json` exposes the flag. `ci-badge/worker.test.js`
  covers that rule through the worker's own `fetch` handler, and the Tests
  workflow runs it with Node's built-in runner so a regression that kept a
  week-old result looking green fails CI. The same file covers the publish
  endpoint: a missing or wrong token is refused, a malformed body never reaches
  D1, and an older run cannot overwrite a newer result. Those write tests run
  against real in-memory SQLite through Node's built-in driver, so the upsert
  and its ordering guard are evaluated by SQL rather than by a JavaScript
  stand-in, and the workflow pins Node 24 for that module.
- The website's download button serves `offgrid-portable.zip` from the latest
  GitHub release: source plus macOS, Linux and Windows launchers, so setup needs
  no second download. A launcher opens a terminal, runs `offgrid setup`, and
  starts the chat when a model server answers; nothing is installed system-wide
  and an old or missing Python is reported instead of failing later.
- `.github/workflows/release.yml`: pushing a `v*` tag builds that archive with
  `offgrid distribute --build`, verifies it with `unzip -t` and the presence of
  executable launchers, and attaches it to the release. The tag must match
  `__version__` in `offgrid/__init__.py`, the only place the version is written:
  `scripts/check_release_version.py` fails the run before it builds anything
  when the two disagree, so a release cannot be labelled with a stale version.
  The release notes are that version's `CHANGELOG.md` section, read by
  `scripts/changelog_notes.py`, so the release page and the changelog say the
  same thing and a version with no section is refused instead of published
  with empty notes. Each release also attaches `SHA256SUMS`, the `sha256sum`
  line for that archive, so a download can be checked with
  `sha256sum -c SHA256SUMS` before it is run. The website reads that file when
  it serves the page and shows the digest next to the download button with the
  command to check it, so the page and the published archive cannot disagree.
  The page and the README also give a PowerShell one-liner that reads the
  expected digest out of `SHA256SUMS` and throws on a mismatch, so a Windows
  download fails loudly instead of being compared by eye; Windows has no
  `sha256sum`. A **Copy** button beside the digest puts it on the clipboard from
  one small same-origin script (the page's CSP allows that one file and no
  inline code), and selects the digest when the clipboard API is unavailable.
- `offgrid setup`: a guided first run that checks the interpreter and platform,
  finds a local model server or explains the two free ways to start one, and
  offers the desktop shortcut. `--no-input` never prompts, `--json` is
  machine-readable, and it exits 1 until a model server answers. Starting the
  chat with no server answering offers the same check before giving up.
- Desktop shortcut installers: `offgrid desktop install` writes a native
  `OffGrid.desktop` entry on Linux, a double-clickable `OffGrid.command` on
  macOS, or an `OffGrid.lnk` on Windows. `offgrid desktop path` and
  `offgrid desktop remove` inspect and clean up the shortcut.
- Clear top-level launchers (`OffGrid.command`, `OffGrid.bat`, `OffGrid.sh`)
  that start interactive chat when double-clicked and are included in source
  checkouts, installed wheels, portable zips, and full USB bundles.
  `offgrid-launch` is the installed-command equivalent.
- `offgrid maintain`: a server-safe self-debug and maintenance loop. It checks
  workspace, private state, request files, model health and free disk; repairs
  state permissions; prunes only old terminal self-improvement requests while
  preserving pending, applied and rollback-failed ones; and can prepare one
  pending improvement per pass with `--improve`. Supports `--once --json` for
  cron or systemd timers and never applies code without approval.
- Gated self-improvement: `offgrid improve` stages a model-written proposal in
  a private copy, requires explicit approval before touching the live tree,
  snapshots every affected file, automatically rolls back failed verification,
  and supports `restore`/`undo`. `chat --improve-when-idle` only creates pending
  requests after a quiet period.
- `offgrid improve terminal`: an interactive, offline-first terminal for
  vibe-coding improvements. It keeps a multi-turn conversation in one private
  staging copy; `/done` creates the pending request, prints the diff, and
  requires explicit approval before applying, while `/cancel` discards it;
  `/show`, `/approve`, `/reject`, `/restore`, and `/quit` are built in.
- Offline `--speak` for `run` and `chat`: final reports are read aloud through
  macOS `say`, Windows SAPI, or Linux `espeak-ng`/`espeak`/`festival`, with
  `--voice`, `OFFGRID_SPEAK`, and `OFFGRID_VOICE` support. No account, network,
  or extra Python dependency; microphone input is not included.
- A real `35b` full-bundle option: Qwen3.6-35B-A3B Q4_K_M from `ggml-org`, about
  20.5 GB, with a pinned SHA-256 and Apache-2.0 licensing. `7b` remains the
  default.
- A `12b` full-bundle option: JetBrains Mellum2.1-12B-A2.5B-Thinking Q4_K_M,
  about 8.1 GB, with a pinned SHA-256 and Apache-2.0 licensing. It is a 12B
  mixture-of-experts model with only ~2.5B active parameters, tuned for
  agentic coding, so a 16 GB laptop or a 12 GB GPU gets long tool-using runs
  without a data-centre card. The builder warns below 15 GB of RAM and
  recommends 16 GB; `7b` remains the default.
- An opt-in `80b` full-bundle option: Qwen3-Next-80B-A3B-Instruct Q4_K_M from
  `Qwen`, about 48.4 GB, with a pinned SHA-256 and Apache-2.0 licensing. The
  wizard warns below 64 GB RAM, and both the wizard and direct builder check
  free space before downloading.
- `offgrid distribute --check-drive PATH --bundle BUNDLE`: verifies exFAT,
  write access, free space with a 1 GB margin, and every bundled GGUF against
  its pinned SHA-256 before copying. The `--to` path and the interactive
  wizard's install option run the same check for mounted device roots; both
  also install a full-bundle directory as well as a portable zip or pyz.
- `offgrid demo`: the full planner/coder/tester/reviewer/lead pipeline against a
  packaged scripted model, with no downloads, model weights, account, or
  internet.
- `offgrid protect --restore PATH`: verify a backup's manifest and optional
  HMAC-SHA256 signature, then restore the continuity kit and model. `--out`,
  `--model-out`, and `--force` control the destination.
- `offgrid-setup-usb` console entry point and wheel data files, so an installed
  `offgrid distribute --build` produces a complete portable bundle instead of
  silently omitting launchers, scripts, README, and licence.
- SHA-256 verification for every full-bundle model download.
- Owner-only permissions for local state directories and files, plus a
  continuity kit written with private permissions.
- `SECURITY.md`, `CONTRIBUTING.md`, troubleshooting and release-check
  documentation, and CI coverage for pull requests, installed-wheel bundles,
  Windows, and macOS.
- A tag-triggered release workflow that builds and checksums artifacts without
  publishing them.

### Changed

- `pyproject.toml` now has a single source of truth for the version, project
  URLs and classifiers, a development extra, and package data files.
- Fleet `serve` now warns that peer traffic is cleartext HTTP with a bearer
  token and is suitable only for a trusted private LAN.
- README install, model, backup/restore, demo, and troubleshooting guidance is
  updated for the new options.

### Security

- Model fixtures are checked against pinned hashes and deleted on mismatch.
- Portable bundle builders fail loudly when installation assets are missing.
- Local memory, session logs, watch baselines, security history, and hardware
  cache are created under a private `.offgrid` directory.
