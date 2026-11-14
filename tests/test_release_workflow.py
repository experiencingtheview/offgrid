"""The release the website links to is built, checked and published by CI."""

import importlib.util
import re
import unittest

from pathlib import Path

from offgrid import __version__

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = ROOT / "pyproject.toml"
GUARD = ROOT / "scripts" / "check_release_version.py"
ASSET = "offgrid-portable.zip"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_release_version", GUARD)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {GUARD}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_the_asset_is_built_by_the_repository_builder(self):
        self.assertIn("offgrid.cli distribute", self.workflow)
        self.assertIn(f"--build dist/{ASSET}", self.workflow)
        self.assertIn("contents: write", self.workflow)

    def test_the_asset_is_checked_and_attached(self):
        self.assertIn("unzip -t", self.workflow)
        self.assertIn("gh release create", self.workflow)
        self.assertIn("gh release upload", self.workflow)
        for launcher in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
            self.assertIn(f'"{launcher}"', self.workflow)

    def test_the_publish_step_confirms_what_it_attached(self):
        # `gh release view` has no `isLatest` field: asking for it fails the job
        # after the upload, so the check stays on fields that exist.
        self.assertNotIn("isLatest", self.workflow)
        self.assertIn(f"for asset in {ASSET} SHA256SUMS; do", self.workflow)
        self.assertIn("has no $asset attached", self.workflow)

    def test_the_checksum_file_is_written_and_checked(self):
        self.assertIn(f"sha256sum {ASSET} > SHA256SUMS", self.workflow)
        self.assertIn("sha256sum --check --strict SHA256SUMS", self.workflow)
        # Both files travel together, so the checksum cannot describe another zip.
        self.assertIn(f"dist/{ASSET} dist/SHA256SUMS", self.workflow)
        self.assertLess(
            self.workflow.index("> SHA256SUMS"),
            self.workflow.index("gh release create"),
        )

    def test_the_readme_says_how_to_check_a_download(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("SHA256SUMS", readme)
        self.assertIn("sha256sum -c SHA256SUMS", readme)
        # Windows ships neither sha256sum nor shasum, so its check has to be a
        # command that fails rather than a hash to compare by eye.
        self.assertIn("Get-FileHash", readme)
        self.assertIn("Select-String", readme)
        self.assertIn("throw 'SHA-256 mismatch'", readme)

    def test_the_website_links_the_archive_it_serves(self):
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'href="/{ASSET}"', page)
        self.assertTrue((ROOT / "public" / ASSET).is_file(), ASSET)
        self.assertTrue((ROOT / "public" / "SHA256SUMS").is_file(), "SHA256SUMS")

    def test_the_version_is_declared_once(self):
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn('version = { attr = "offgrid.__version__" }', pyproject)
        self.assertEqual(_load_guard().package_version(), __version__)

    def test_a_tag_becomes_a_release_only_when_it_matches_the_version(self):
        guard = _load_guard()
        self.assertEqual(guard.problem(f"v{__version__}", __version__), "")
        self.assertEqual(guard.problem(__version__, __version__), "")
        message = guard.problem("v9.9.9", __version__)
        self.assertIn("__version__", message)
        self.assertEqual(guard.main(["v9.9.9"]), 1)

    def test_the_workflow_checks_the_tag_before_it_builds(self):
        self.assertIn('check_release_version.py "$TAG"', self.workflow)
        self.assertLess(
            self.workflow.index("check_release_version.py"),
            self.workflow.index("offgrid.cli distribute"),
        )

    def test_the_site_serves_the_archive_it_links(self):
        # The download must keep working even when the GitHub release is stale
        # or rate-limited. The digest is read from the same public asset.
        worker = (ROOT / "site-worker.js").read_text(encoding="utf-8")
        deploy = (ROOT / "deploy.json").read_text(encoding="utf-8")
        self.assertNotIn("site-archive", worker + deploy)
        self.assertIn("/SHA256SUMS", worker)
        self.assertIn("env.ASSETS", worker)
        self.assertTrue((ROOT / "public" / ASSET).is_file())

    def test_the_website_shows_the_digest_of_the_file_it_serves(self):
        # Read from the release at request time, so the page cannot show a
        # digest that belongs to a different build than the download button.
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        worker = (ROOT / "site-worker.js").read_text(encoding="utf-8")
        self.assertIn("data-release-digest", page)
        self.assertIn("sha256sum -c SHA256SUMS", page)
        self.assertIn("Get-FileHash", page)
        self.assertIn("throw 'SHA-256 mismatch'", page)

    def test_the_digest_can_be_copied_without_selecting_it(self):
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "copy.js").read_text(encoding="utf-8")
        minified = (ROOT / "public" / "copy.min.js").read_text(encoding="utf-8")
        worker = (ROOT / "site-worker.js").read_text(encoding="utf-8")
        self.assertIn("data-copy-digest", page)
        self.assertIn('/styles.min.css', page)
        self.assertIn('src="/copy.min.js" defer fetchpriority="low"', page)
        self.assertLess(page.index("copy.min.js"), page.index("</head>"))
        self.assertLess(len(minified), len(script))
        for marker in ("navigator.clipboard.writeText", "selectNodeContents", "[0-9a-f]{64}"):
            self.assertIn(marker, minified)
        self.assertIn("navigator.clipboard.writeText", script)
        # Only that one same-origin file may run, and never inline code.
        self.assertIn("script-src 'self'", worker)
        self.assertNotIn("unsafe-inline", worker)
        # A missing clipboard selects the digest instead of doing nothing.
        self.assertIn("selectNodeContents", script)
        # The button must not offer to copy the "see SHA256SUMS" fallback.
        self.assertIn("[0-9a-f]{64}", script)
        self.assertIn("data-release-digest", worker)
        self.assertIn("DIGEST_ASSET", worker)
        self.assertIn("env.ASSETS", worker)
        self.assertIn("/SHA256SUMS", worker)
        self.assertIn("AbortSignal.timeout", worker)
        # The injection has to happen before the page is returned.
        self.assertLess(worker.index("releaseDigest(env, url)"), worker.index("withDigest(page"))

    def test_the_site_uses_the_cloudflare_edge_cache(self):
        worker = (ROOT / "site-worker.js").read_text(encoding="utf-8")
        self.assertIn("globalThis.caches?.default", worker)
        self.assertIn("cache.match(cacheKey)", worker)
        self.assertIn("cache.put(cacheKey", worker)
        self.assertIn("ctx.waitUntil", worker)
        self.assertIn("cdn-cache-control", worker)
        # A failed digest lookup must not become the five-minute cached page.
        self.assertIn("if (digest && cache && ctx?.waitUntil)", worker)
        self.assertIn('headers.set("cache-control", "no-store")', worker)
        self.assertIn("CACHE_VERSION", worker)
        self.assertIn("EDGE_CACHE_SECONDS = 300", worker)
        self.assertIn("let digestCache", worker)
        self.assertIn("digestCache.expires", worker)
        # A cache failure must not stop the page from rendering.
        self.assertIn("A cache miss must never stop the page", worker)


if __name__ == "__main__":
    unittest.main()
