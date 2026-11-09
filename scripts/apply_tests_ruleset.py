#!/usr/bin/env python3
"""Apply the required "Tests" ruleset to the repository's default branch.

GitHub Free does not offer rulesets for private repositories. Run this after
the repository is on GitHub Pro, or after it is made public. The script is
idempotent: it updates the ruleset named "Tests" when one exists and creates it
otherwise.

The required contexts are the job names in .github/workflows/tests.yml. The
"Publish public CI status" job is intentionally excluded because it is skipped
on pull requests. Administrators and deploy keys are listed as bypass actors so
the owner can still push a release commit directly; the checks gate pull
request merges.

Usage:
    GITHUB_TOKEN=... python3 scripts/apply_tests_ruleset.py
    python3 scripts/apply_tests_ruleset.py --repo owner/name --dry-run

The token needs permission to administer repository rulesets: fine-grained
"Administration" read and write, or the classic "repo" scope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Sequence

API_URL = "https://api.github.com"
RULESET_NAME = "Tests"
REQUIRED_CHECKS = (
    "Full test suite (Python 3.9)",
    "Full test suite (Python 3.12)",
    "Installed wheel and portable bundle",
    "Wheel install on windows-latest",
    "Wheel install on macos-latest",
    "Desktop shortcut on Windows",
    "Desktop shortcut on macOS",
    "Desktop shortcut on Linux",
    "Windows launcher messages",
    "Secret scanning",
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_HOSTS = {"github.com", "ssh.github.com"}


def ruleset_payload() -> Dict[str, Any]:
    """The API body for the Tests ruleset."""
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}
        },
        "bypass_actors": [
            {"actor_id": None, "actor_type": "DeployKey", "bypass_mode": "always"},
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        ],
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": context} for context in REQUIRED_CHECKS
                    ],
                },
            }
        ],
    }


def _repository_from_remote(remote: str) -> str:
    """Turn a GitHub remote URL into OWNER/REPO."""
    value = remote.strip()
    if not value:
        raise ValueError("empty remote URL")
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        path = parsed.path
    else:
        # scp-like syntax: git@github.com:owner/repo.git
        host_part, _, path = value.partition(":")
        host = host_part.rsplit("@", 1)[-1].lower()
    if host not in GITHUB_HOSTS:
        raise ValueError(f"remote host {host or '(none)'} is not GitHub")
    path = urllib.parse.unquote(path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def _validate_repository(repo: str) -> str:
    value = repo.strip().strip("/")
    if not REPOSITORY_RE.match(value):
        raise ValueError("expected OWNER/REPO")
    return value


def repository_from_origin() -> str:
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"could not read the origin remote: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError("the repository has no origin remote")
    return _repository_from_remote(completed.stdout)


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500] or "(no response body)"
    if isinstance(data, dict):
        message = str(data.get("message") or text)
        errors = data.get("errors")
        if errors:
            message += " " + json.dumps(errors, ensure_ascii=False)[:300]
        return message
    return text[:500]


def request(
    method: str, path: str, token: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    api_request = urllib.request.Request(
        API_URL + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "OffGridRuleset/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(api_request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc.read())
        hint = ""
        if exc.code == 403:
            hint = (
                "\nPrivate repositories need GitHub Pro (or must be public) before "
                "rulesets can be applied; the token also needs Administration write access."
            )
        raise SystemExit(
            f"GitHub API {method} {path} failed: HTTP {exc.code}: {detail}{hint}"
        )
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach {API_URL}: {exc.reason}")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SystemExit(f"GitHub returned invalid JSON for {method} {path}: {exc}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the required Tests ruleset to the default branch."
    )
    parser.add_argument(
        "--repo", default="", help="OWNER/REPO (default: the origin remote)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the API body and exit without calling GitHub",
    )
    args = parser.parse_args(argv)

    try:
        repo = (
            _validate_repository(args.repo)
            if args.repo
            else repository_from_origin()
        )
    except ValueError as exc:
        raise SystemExit(
            f"could not determine the repository: {exc}. Pass --repo OWNER/REPO."
        )

    payload = ruleset_payload()
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    token = os.environ.get("GITHUB_TOKEN", "").strip() or os.environ.get(
        "GH_TOKEN", ""
    ).strip()
    if not token:
        raise SystemExit(
            "set GITHUB_TOKEN or GH_TOKEN to a token that can administer repository rulesets"
        )

    repository = request("GET", f"/repos/{repo}", token)
    if not isinstance(repository, dict):
        raise SystemExit(f"GitHub returned an unexpected repository response for {repo}")
    default_branch = str(repository.get("default_branch") or "")

    listed = request("GET", f"/repos/{repo}/rulesets?per_page=100", token)
    existing = None
    if isinstance(listed, list):
        for item in listed:
            if (
                isinstance(item, dict)
                and item.get("name") == RULESET_NAME
                and item.get("target") in (None, "branch")
            ):
                existing = item
                break

    if existing is not None:
        ruleset_id = existing.get("id")
        if ruleset_id is None:
            raise SystemExit(f"ruleset {RULESET_NAME!r} has no id; inspect it on GitHub")
        result = request(
            "PUT", f"/repos/{repo}/rulesets/{ruleset_id}", token, payload
        )
        action = "updated"
    else:
        result = request("POST", f"/repos/{repo}/rulesets", token, payload)
        action = "created"
    if not isinstance(result, dict):
        raise SystemExit(f"GitHub returned an unexpected ruleset response for {repo}")

    print(
        f"{RULESET_NAME} ruleset {action} for {repo} "
        f"(id {result.get('id', 'unknown')})."
    )
    if default_branch:
        print(f"Default branch: {default_branch}")
    print("Required checks:")
    for context in REQUIRED_CHECKS:
        print(f"  - {context}")
    url = result.get("html_url")
    if url:
        print(f"Ruleset: {url}")
    print(
        'The "Publish public CI status" job is not required because it is skipped '
        "on pull requests."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
