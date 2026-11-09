# Release checklist

Nothing here authorises a tag, a GitHub release, or a package upload by itself.
Those are separate decisions for the maintainer.

## Before tagging

- [ ] Set the version in `offgrid/__init__.py` (the single source of truth).
- [ ] Update `CHANGELOG.md` and the test count/commands in `README.md`.
- [ ] `python3 -m unittest discover -s tests -t .` passes on Python 3.9 and 3.12.
- [ ] The required Tests checks and the `Tests` ruleset are in place (see
  [Required Tests checks and branch protection](#required-tests-checks-and-branch-protection)).
- [ ] `python3 -m offgrid demo --no-color` succeeds.
- [ ] `python3 -m offgrid security review` has no unreviewed critical finding.
- [ ] `git status` contains only intended files; no models, virtualenvs, build
  output, keys, `.env`, or generated bundles.

## Clean install and bundle

Use a fresh virtual environment and a separate output directory:

```console
python3 -m build --outdir /tmp/offgrid-dist
python3 -m venv /tmp/offgrid-venv
/tmp/offgrid-venv/bin/pip install /tmp/offgrid-dist/offgrid-*.whl
/tmp/offgrid-venv/bin/offgrid --version
/tmp/offgrid-venv/bin/offgrid-launch --version
mkdir -p /tmp/offgrid-work && cd /tmp/offgrid-work
/tmp/offgrid-venv/bin/offgrid demo --no-color
/tmp/offgrid-venv/bin/offgrid distribute --build /tmp/offgrid-portable.zip --with-tests
```

- [ ] The wheel installs without network access beyond the package index.
- [ ] `offgrid --version`, `offgrid-launch --version`, and `offgrid demo` work
  from the wheel.
- [ ] The portable zip contains `OffGrid.sh`, `OffGrid.command`, `OffGrid.bat`,
  `launch/`, `scripts/`, `README.md`, `LICENSE`, `pyproject.toml`, `offgrid/`,
  `offgrid.pyz`, and `tests/` when requested.
- [ ] The zip extracts and `offgrid.pyz --version` runs.
- [ ] A `distribute --to` install to a temporary mounted directory works.

## Real-model smoke tests

These need actual hardware and downloaded weights. Record the machine, model,
and result; do not mark a model tested because its dry-run planned correctly.

- [ ] 3B: `offgrid doctor`, one small `run`, and a failing-test repair.
- [ ] 7B: `offgrid doctor`, a multi-file `run`, tool calling, and verification.
- [ ] 14B: `offgrid doctor`, plan/review quality check, and verification.
- [ ] 35B: machine has about 32 GB RAM and enough free disk; `doctor` identifies
  the model, a small `run` completes, and the model file's SHA-256 matches the
  pinned value.
- [ ] Ollama, llama.cpp, and at least one other OpenAI-compatible server path
  where the platform supports it.
- [ ] A CPU-only machine reports honest timings rather than claiming GPU speed.

## Cross-platform

- [ ] Ubuntu (Python 3.9 and 3.12) full suite.
- [ ] Windows wheel install and `offgrid demo` or `offgrid --version`.
- [ ] macOS wheel install and `offgrid --version`; Apple Silicon launcher check.
- [ ] Portable bundle built on each target platform; do not reuse an x86-64
  Linux runtime on Apple Silicon.
- [ ] `offgrid protect --kit`, `--backup`, `--restore`, and a signed restore.

## Security and integrity

- [ ] Model SHA-256 values in `offgrid/fullbundle.py` match the upstream files.
- [ ] `offgrid protect --check --backup ... --sign-key ...` passes; a modified
  copy fails.
- [ ] Fleet `serve` prints the cleartext-HTTP warning.
- [ ] A wheel install built bundle is tested; no missing data files.
- [ ] No secrets in the diff or artifacts. Check `git diff` and the built zip.

## Required Tests checks and branch protection

The default branch must require every Tests job before a release is tagged. The
repository script creates or updates a ruleset named `Tests` for
`~DEFAULT_BRANCH`:

```console
python3 scripts/apply_tests_ruleset.py --dry-run   # show the API body only
GITHUB_TOKEN=... python3 scripts/apply_tests_ruleset.py
```

The token needs permission to administer repository rulesets (fine-grained:
Administration read and write; classic: `repo`). GitHub Free does not offer
rulesets for a private repository, so this needs GitHub Pro or a public
repository. The script is idempotent: re-running it after a job name changes
updates the same ruleset instead of creating a duplicate. It prints the ruleset
URL and required contexts on success.

The required contexts are:

- `Full test suite (Python 3.9)`
- `Full test suite (Python 3.12)`
- `Installed wheel and portable bundle`
- `Wheel install on windows-latest`
- `Wheel install on macos-latest`
- `Desktop shortcut on Windows`
- `Desktop shortcut on macOS`
- `Desktop shortcut on Linux`
- `Windows launcher messages`

`Publish public CI status` is deliberately excluded: it is skipped on pull
requests, so it cannot report a useful pull-request result. The ruleset also
requires the branch to be up to date (`strict_required_status_checks_policy`).
Administrators and deploy keys may bypass it, so the owner can still push a
release commit directly while the checks gate pull request merges.

Verify before tagging:

- [ ] Settings -> Rules -> Rulesets shows an active `Tests` ruleset targeting
  the default branch.
- [ ] The ruleset lists the nine contexts above and no badge job.
- [ ] A pull request shows the Tests checks; a failing or missing check blocks
  merging, and the merge stays disabled until the branch is up to date.
- [ ] If the GitHub plan still refuses rulesets, record that branch protection
  could not be applied instead of treating the release as protected.

## Tag and publish (maintainer only)

Once `__version__` in `offgrid/__init__.py` is bumped and merged to `main`, the
tag is the whole release step:

```console
git tag -a "v$(python3 -c 'import offgrid; print(offgrid.__version__)')" \
  -m "OffGrid release"
git push origin --tags
```

- [ ] Tag only after the checks above, and only from a commit that is on `main`.
- [ ] The pushed tag triggers `.github/workflows/release.yml`. It first checks
  that the tag matches `offgrid/__init__.py` and fails instead of publishing a
  bundle labelled with a different version.
- [ ] The workflow builds `offgrid-portable.zip`, checks it, and attaches it to
  the release; it does not publish to PyPI and does not upload a wheel or sdist.
- [ ] Re-running the workflow with an existing tag replaces the asset in place
  and refreshes the notes on the release.
- [ ] `CHANGELOG.md` has a `## [<version>] - <date>` section; the workflow
  publishes it as the release notes and fails rather than publishing a release
  with no notes.