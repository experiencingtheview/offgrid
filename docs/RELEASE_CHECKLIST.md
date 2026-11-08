# Release checklist

Nothing here authorises a tag, a GitHub release, or a package upload by itself.
Those are separate decisions for the maintainer.

## Before tagging

- [ ] Set the version in `offgrid/__init__.py` (the single source of truth).
- [ ] Update `CHANGELOG.md` and the test count/commands in `README.md`.
- [ ] `python3 -m unittest discover -s tests -t .` passes on Python 3.9 and 3.12.
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
mkdir -p /tmp/offgrid-work && cd /tmp/offgrid-work
/tmp/offgrid-venv/bin/offgrid demo --no-color
/tmp/offgrid-venv/bin/offgrid distribute --build /tmp/offgrid-portable.zip --with-tests
```

- [ ] The wheel installs without network access beyond the package index.
- [ ] `offgrid --version` and `offgrid demo` work from the wheel.
- [ ] The portable zip contains `launch/`, `scripts/`, `README.md`, `LICENSE`,
  `pyproject.toml`, `offgrid/`, `offgrid.pyz`, and `tests/` when requested.
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

## Tag and publish (maintainer only)

- [ ] Create an annotated tag and GitHub release only after the checks above.
- [ ] The release workflow uploads artifacts and `SHA256SUMS`; it does not
  publish to PyPI.
- [ ] Attach the wheel, sdist, portable zip, `.pyz`, and checksums.
- [ ] Publish the release notes from `CHANGELOG.md`.
