# Changelog

All notable changes to OffGrid are recorded here.

## [0.1.0] - 2026-11-08

### Added

- A public, read-only CI status badge served by a small Sites Worker and D1
  database. The Tests workflow publishes the latest `main` result without
  exposing a GitHub token, so the badge works without a GitHub sign-in.
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
