# Contributing

OffGrid is deliberately small: Python 3.9+, the standard library only, and no
runtime dependencies. Please keep it that way unless there is a strong reason
and the change is discussed first.

## Set up

```console
git clone https://github.com/experiencingtheview/offgrid.git
cd offgrid
python3 -m unittest discover -s tests -t .
python3 scripts/demo.py
```

No model, server, account, or network is needed for the test suite or demo. The
tests use a scripted OpenAI-compatible server on localhost.

## What to check before opening a pull request

- `python3 -m unittest discover -s tests -t .` passes on Python 3.9 and 3.12
  (CI covers both).
- New behavior has a focused test. Security and permission changes need a test
  that would fail without the fix.
- No new runtime dependency, telemetry, hidden network call, or broad filesystem
  access.
- User-facing changes update `README.md` and, when notable, `CHANGELOG.md`.
- Error messages say what failed and what to do next; do not silently skip a
  missing security check.

## Packaging changes

An installed wheel must still be useful. Check the data files and portable
bundle from a clean virtual environment:

```console
python3 -m build --outdir /tmp/offgrid-dist
python3 -m venv /tmp/offgrid-venv
/tmp/offgrid-venv/bin/pip install /tmp/offgrid-dist/offgrid-*.whl
/tmp/offgrid-venv/bin/offgrid --version
/tmp/offgrid-venv/bin/offgrid-launch --version
mkdir -p /tmp/offgrid-work
cd /tmp/offgrid-work
/tmp/offgrid-venv/bin/offgrid distribute --build /tmp/offgrid-portable.zip
```

The resulting zip must contain `OffGrid.sh`, `OffGrid.command`, `OffGrid.bat`,
`launch/`, `scripts/`, `README.md`, `LICENSE`, `pyproject.toml`, the package,
and `offgrid.pyz`. On Windows, use the venv's
`Scripts\` paths. Do not commit build output, virtual environments, or model
weights.

## Style

- Match the surrounding code and keep comments about *why*, not line-by-line
  narration.
- Prefer the standard library; avoid clever abstractions that hide a security
  check.
- Keep changes scoped. A bug fix should not also reorganise unrelated modules.
- Do not add AI attribution to commits unless the project explicitly asks for
  it.

## Security

Read [SECURITY.md](SECURITY.md) before changing path validation, command
execution, fleet transport, backup signing, download handling, or permissions.
Report vulnerabilities privately rather than in a public issue.
