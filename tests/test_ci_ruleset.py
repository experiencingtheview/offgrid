"""Keep the required-check ruleset in step with the Tests workflow.

The ruleset cannot import GitHub Actions job names, so this test parses the
small part of ``tests.yml`` that produces check names: job ids, ``name``
values, matrix values, the badge job's pull-request condition, and the results
its aggregate actually requires. It uses only the standard library, like the
rest of the project.
"""

import importlib.util
import itertools
import re
import unittest
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
RULESET_SCRIPT = ROOT / "scripts" / "apply_tests_ruleset.py"
EXCLUDED_JOB_NAME = "Publish public CI status"
MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
RESULT_ENV = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*):\s*\$\{\{\s*needs\.([A-Za-z0-9_-]+)\.result\s*\}\}\s*$",
    re.MULTILINE,
)
SUCCESS_CHECK = re.compile(r'\[\s*"\$([A-Z][A-Z0-9_]*)"\s*=\s*"success"\s*\]')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _job_blocks(text: str) -> Dict[str, List[str]]:
    """Return each job id and the indented lines that belong to it."""
    lines = text.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if line.strip() == "jobs:"
        )
    except StopIteration:
        raise AssertionError("tests.yml has no jobs: section") from None
    blocks: Dict[str, List[str]] = {}
    current = None
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = _indent(line)
        if indent == 0:
            break
        if indent == 2 and line.rstrip().endswith(":"):
            current = line.strip()[:-1]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def _job_name(job_id: str, lines: List[str]) -> str:
    for line in lines:
        if _indent(line) == 4 and line.lstrip().startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"job {job_id} has no name")


def _if_condition(lines: List[str]) -> str:
    for index, line in enumerate(lines):
        if _indent(line) == 4 and line.lstrip().startswith("if:"):
            value = line.split(":", 1)[1].strip()
            if value in (">", ">-", "|", "|-"):
                parts = []
                for follow in lines[index + 1 :]:
                    if not follow.strip():
                        continue
                    if _indent(follow) <= 4:
                        break
                    parts.append(follow.strip())
                return " ".join(parts)
            return value
    return ""


def _needs(job_id: str, lines: List[str]) -> List[str]:
    """Return the job ids listed under ``needs:`` in one job block."""
    for index, line in enumerate(lines):
        if _indent(line) != 4 or not line.lstrip().startswith("needs:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value.startswith("["):
            if not value.endswith("]"):
                raise AssertionError(f"job {job_id} has an unterminated needs list")
            return [
                item.strip().strip("'\"")
                for item in value[1:-1].split(",")
                if item.strip()
            ]
        if value:
            raise AssertionError(
                f"job {job_id} has an unsupported needs value: {value}"
            )
        listed: List[str] = []
        for follow in lines[index + 1 :]:
            if not follow.strip():
                continue
            if _indent(follow) <= 4:
                break
            item = follow.strip()
            if not item.startswith("- "):
                break
            listed.append(item[2:].strip().strip("'\""))
        return listed
    raise AssertionError(f"job {job_id} has no needs: entry")


def _result_env(job_id: str, lines: List[str]) -> Dict[str, str]:
    """Map every ``NAME: ${{ needs.job.result }}`` env var to its job id."""
    mapping: Dict[str, str] = {}
    for name, needed in RESULT_ENV.findall("\n".join(lines)):
        if name in mapping:
            raise AssertionError(f"job {job_id} sets {name} twice")
        mapping[name] = needed
    return mapping


def _aggregated_results(job_id: str, lines: List[str]) -> List[str]:
    """Return the env vars the aggregate requires to equal ``success``."""
    found = SUCCESS_CHECK.findall("\n".join(lines))
    if not found:
        raise AssertionError(f"job {job_id} has no success checks")
    return found


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _scalar_values(raw: str) -> List[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_unquote(part.strip()) for part in inner.split(",") if part.strip()]
    return [_unquote(raw)]


def _matrix_values(job_id: str, lines: List[str]) -> Dict[str, List[str]]:
    """Read direct matrix keys and include entries from one job block."""
    matrix_index = None
    for index, line in enumerate(lines):
        if line.strip() == "matrix:":
            matrix_index = index
            break
    if matrix_index is None:
        return {}

    matrix_indent = _indent(lines[matrix_index])
    block: List[str] = []
    for line in lines[matrix_index + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) <= matrix_indent:
            break
        block.append(line)

    values: Dict[str, List[str]] = {}
    include_indent = None
    for index, line in enumerate(block):
        stripped = line.strip()
        indent = _indent(line)
        if stripped == "include:":
            include_indent = indent
            continue
        if include_indent is not None and indent > include_indent:
            match = re.match(r"^(?:-\s*)?([A-Za-z0-9_-]+):\s*(.+)$", stripped)
            if match:
                key, raw = match.groups()
                values.setdefault(key, []).extend(_scalar_values(raw))
            continue

        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", stripped)
        if not match:
            continue
        key, raw = match.groups()
        if key == "include":
            continue
        if raw:
            values.setdefault(key, []).extend(_scalar_values(raw))
            continue

        sequence: List[str] = []
        for follow in block[index + 1 :]:
            if not follow.strip() or follow.lstrip().startswith("#"):
                continue
            if _indent(follow) <= indent:
                break
            item = follow.strip()
            if not item.startswith("- "):
                break
            sequence.extend(_scalar_values(item[2:]))
        values.setdefault(key, []).extend(sequence)

    if not values:
        raise AssertionError(f"job {job_id} has a matrix block but no values")
    return values


def _expand_job_name(
    job_id: str, name: str, matrix: Dict[str, List[str]]
) -> List[str]:
    keys = MATRIX_EXPRESSION.findall(name)
    if not keys:
        if "${{" in name:
            raise AssertionError(f"job {job_id} uses an unsupported name expression")
        return [name]

    options = []
    for key in keys:
        if key not in matrix or not matrix[key]:
            raise AssertionError(
                f"job {job_id} references matrix.{key} without values"
            )
        options.append(matrix[key])

    names = []
    for combination in itertools.product(*options):
        mapping = dict(zip(keys, combination))
        names.append(
            MATRIX_EXPRESSION.sub(lambda match: mapping[match.group(1)], name)
        )
    if len(names) != len(set(names)):
        raise AssertionError(f"job {job_id} expands to duplicate names: {names}")
    return names


def _load_ruleset_module():
    spec = importlib.util.spec_from_file_location(
        "apply_tests_ruleset", RULESET_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {RULESET_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RequiredCheckDriftTests(unittest.TestCase):
    def test_ruleset_checks_match_tests_workflow_jobs(self):
        module = _load_ruleset_module()
        blocks = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))

        badge_ids = [
            job_id
            for job_id, lines in blocks.items()
            if _job_name(job_id, lines) == EXCLUDED_JOB_NAME
        ]
        self.assertEqual(
            len(badge_ids),
            1,
            f"expected exactly one {EXCLUDED_JOB_NAME!r} job in {WORKFLOW.name}",
        )
        badge_id = badge_ids[0]
        self.assertIn(
            "github.event_name != 'pull_request'",
            _if_condition(blocks[badge_id]),
            "the badge job must stay skipped on pull requests",
        )

        workflow_checks: List[str] = []
        for job_id, lines in blocks.items():
            name = _job_name(job_id, lines)
            expanded = _expand_job_name(job_id, name, _matrix_values(job_id, lines))
            if job_id == badge_id:
                continue
            workflow_checks.extend(expanded)

        self.assertEqual(
            len(workflow_checks),
            len(set(workflow_checks)),
            "tests.yml produces duplicate check names",
        )
        required = list(module.REQUIRED_CHECKS)
        self.assertEqual(
            len(required),
            len(set(required)),
            "the ruleset lists a required check more than once",
        )

        missing = sorted(set(workflow_checks) - set(required))
        extra = sorted(set(required) - set(workflow_checks))
        self.assertFalse(
            missing or extra,
            "the Tests ruleset has drifted from tests.yml: "
            f"workflow checks missing from the ruleset: {missing}; "
            f"ruleset checks not produced by the workflow: {extra}",
        )

    def test_badge_aggregate_covers_every_needed_job(self):
        blocks = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))
        badge_ids = [
            job_id
            for job_id, lines in blocks.items()
            if _job_name(job_id, lines) == EXCLUDED_JOB_NAME
        ]
        self.assertEqual(
            len(badge_ids),
            1,
            f"expected exactly one {EXCLUDED_JOB_NAME!r} job in {WORKFLOW.name}",
        )
        badge_id = badge_ids[0]
        lines = blocks[badge_id]

        needs = _needs(badge_id, lines)
        self.assertGreaterEqual(len(needs), 2, "the badge job must depend on other jobs")
        self.assertEqual(
            len(needs), len(set(needs)), "the badge job lists a need more than once"
        )
        self.assertEqual(
            sorted(needs),
            sorted(job_id for job_id in blocks if job_id != badge_id),
            "the badge job must depend on every job in the workflow, otherwise a "
            "failure in a job it does not wait for still publishes a green badge",
        )

        env = _result_env(badge_id, lines)
        by_job: Dict[str, str] = {}
        for name, needed in env.items():
            if needed in by_job:
                raise AssertionError(
                    f"the badge job reads {needed!r} through both "
                    f"{by_job[needed]} and {name}"
                )
            by_job[needed] = name

        self.assertEqual(
            sorted(set(by_job) - set(needs)),
            [],
            "the badge job reads results it does not declare under needs",
        )
        self.assertEqual(
            sorted(set(needs) - set(by_job)),
            [],
            "the badge job needs a job it never reads, so that job's result is ignored",
        )

        aggregated = _aggregated_results(badge_id, lines)
        duplicates = sorted(
            {name for name in aggregated if aggregated.count(name) > 1}
        )
        self.assertEqual(
            duplicates, [], f"the badge aggregate checks {duplicates} more than once"
        )
        self.assertEqual(
            sorted(set(aggregated) - set(env)),
            [],
            "the badge aggregate checks a result no needs entry sets",
        )
        unchecked = sorted(name for name in env if name not in set(aggregated))
        self.assertEqual(
            unchecked,
            [],
            "the badge aggregate does not require these results, so the matching "
            f"job can fail while the badge stays green: {unchecked}",
        )


if __name__ == "__main__":
    unittest.main()
