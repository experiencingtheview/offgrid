# Changelog

All notable changes to OffGrid are recorded here.

## [0.1.0] - 2026-11-08

### Added

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
