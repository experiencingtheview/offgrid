"""Deep research: source verification, selection, and offline saves."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.library import Library
from offgrid.research import ResearchCache, ResearchError, ResearchReport, ResearchService
from offgrid.tools import ToolBox
from offgrid.workspace import Workspace


class FakeWiki:
    def __init__(self) -> None:
        self.calls = []
        self.fail_wikibooks = False

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if "wikibooks" in url and self.fail_wikibooks:
            raise ResearchError("wikibooks unavailable")
        if params.get("list") == "search":
            source = "wikipedia" if "wikipedia" in url else "wikibooks"
            if source == "wikipedia":
                return {
                    "query": {
                        "search": [
                            {
                                "title": "Solar panel",
                                "pageid": 1,
                                "snippet": "A <span>solar</span> panel converts sunlight.",
                            },
                            {
                                "title": "Solar inverter",
                                "pageid": 2,
                                "snippet": "An inverter converts direct current.",
                            },
                        ]
                    }
                }
            return {
                "query": {
                    "search": [
                        {
                            "title": "Solar energy",
                            "pageid": 10,
                            "snippet": "Solar energy and panel maintenance.",
                        }
                    ]
                }
            }
        pageid = int(params["pageids"])
        if pageid == 1:
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": 1,
                            "title": "Solar panel",
                            "fullurl": "https://en.wikipedia.org/wiki/Solar_panel",
                            "extract": (
                                "A solar panel converts sunlight into electricity. "
                                "Solar panel maintenance includes cleaning and inspection."
                            ),
                            "revisions": [{"revid": 101, "sha1": "abc123"}],
                        }
                    ]
                }
            }
        if pageid == 10:
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": 10,
                            "title": "Solar energy",
                            "fullurl": "https://en.wikibooks.org/wiki/Solar_energy",
                            "extract": "Solar energy uses panels. Maintenance matters for output.",
                            "revisions": [{"revid": 202, "sha1": "def456"}],
                        }
                    ]
                }
            }
        return {"query": {"pages": [{"missing": True}]}}


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = FakeWiki()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def service(self, **overrides) -> ResearchService:
        values = dict(
            workspace=str(self.root),
            research_online=True,
            research_timeout=5,
            library_dir="offgrid-library",
            verbose=0,
            color=False,
            network="allow",
        )
        values.update(overrides)
        config = Config(**values)
        library = Library(self.root / config.library_dir)
        return ResearchService(self.root, config, fetch_json=self.fake, library=library)

    def test_research_records_source_provenance(self) -> None:
        report = self.service().investigate(
            "solar panel maintenance", limit=2, source_ids=["wikipedia", "wikibooks"]
        )
        self.assertEqual(len(report.items), 2)
        for item in report.items:
            self.assertTrue(item.verified)
            self.assertIn(item.license, ("CC BY-SA 4.0",))
            self.assertGreater(item.revision, 0)
            self.assertEqual(len(item.sha256), 64)
            self.assertTrue(item.url.startswith("https://"))
            self.assertTrue(item.verification)
        self.assertIn("source-verified", report.render())
        self.assertNotIn("text", report.as_dict()["items"][0])
        self.assertIn("text", report.as_dict(include_text=True)["items"][0])

    def test_offline_and_opt_in_guards(self) -> None:
        with self.assertRaises(ResearchError) as ctx:
            self.service(research_online=False).investigate("solar", limit=1)
        self.assertIn("--online", str(ctx.exception))
        with self.assertRaises(ResearchError) as ctx:
            self.service(offline=True, network="deny").investigate("solar", limit=1)
        self.assertIn("offline", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_cache_avoids_repeated_api_requests(self) -> None:
        service = self.service()
        first = service.investigate("solar panel", limit=1, source_ids=["wikipedia"])
        self.assertTrue(first.items)
        calls = len(self.fake.calls)
        second = service.investigate("solar panel", limit=1, source_ids=["wikipedia"])
        self.assertEqual(len(self.fake.calls), calls)
        self.assertTrue(second.items)
        self.assertGreater(second.cache["hits"], 0)
        cache_files = list((self.root / ".offgrid" / "research-cache").rglob("*.json"))
        self.assertTrue(cache_files)

    def test_refresh_and_zero_ttl_bypass_cache(self) -> None:
        refreshed = self.service(research_refresh=True)
        refreshed.investigate("solar panel", limit=1, source_ids=["wikipedia"])
        first_calls = len(self.fake.calls)
        refreshed.investigate("solar panel", limit=1, source_ids=["wikipedia"])
        self.assertGreater(len(self.fake.calls), first_calls)

        self.fake.calls.clear()
        uncached = self.service(research_cache_ttl=0)
        uncached.investigate("solar panel", limit=1, source_ids=["wikipedia"])
        first_calls = len(self.fake.calls)
        uncached.investigate("solar panel", limit=1, source_ids=["wikipedia"])
        self.assertGreater(len(self.fake.calls), first_calls)

    def test_cache_ttl_expiry_is_deterministic(self) -> None:
        now = [1000.0]
        cache = ResearchCache(self.root / "cache", ttl=10, clock=lambda: now[0])
        params = {"action": "query", "pageids": "1"}
        self.assertIsNone(cache.get("wikipedia", "https://en.wikipedia.org/w/api.php", params))
        self.assertTrue(cache.put("wikipedia", "https://en.wikipedia.org/w/api.php", params, {"ok": True}))
        self.assertEqual(
            cache.get("wikipedia", "https://en.wikipedia.org/w/api.php", params), {"ok": True}
        )
        now[0] = 1011.0
        self.assertIsNone(cache.get("wikipedia", "https://en.wikipedia.org/w/api.php", params))

    def test_failed_requests_are_not_cached(self) -> None:
        self.fake.fail_wikibooks = True
        service = self.service()
        failed = service.investigate("solar panel", limit=1, source_ids=["wikibooks"])
        self.assertFalse(failed.items)
        self.assertTrue(service.cache.path_for("wikibooks", "https://en.wikibooks.org/w/api.php", {}))
        self.assertFalse(list((self.root / ".offgrid" / "research-cache" / "wikibooks").glob("*.json")))

    def test_source_failure_becomes_a_warning(self) -> None:
        self.fake.fail_wikibooks = True
        report = self.service().investigate(
            "solar panel", limit=2, source_ids=["wikipedia", "wikibooks"]
        )
        self.assertTrue(report.items)
        self.assertTrue(report.warnings)
        self.assertIn("wikibooks unavailable", " ".join(report.warnings))

    def test_only_selected_results_are_saved_after_approval(self) -> None:
        service = self.service()
        report = service.investigate(
            "solar panel maintenance", limit=2, source_ids=["wikipedia", "wikibooks"]
        )
        declined = service.save(report, ["1"], confirm=lambda _message: False)
        self.assertFalse(declined.ok)
        self.assertFalse((self.root / "offgrid-library" / "research").exists())

        saved = service.save(report, ["1"], confirm=lambda message: "Solar panel" in message)
        self.assertTrue(saved.ok, saved.detail)
        self.assertEqual(len(saved.saved), 1)
        path = Path(saved.saved[0]["path"])
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("title: \"Solar panel\"", text)
        self.assertIn("CC BY-SA 4.0", text)
        self.assertIn("revision: 101", text)
        self.assertIn("source_url:", text)
        self.assertIn("Solar panel maintenance", text)

        library = Library(self.root / "offgrid-library")
        library.index()
        hits = library.search("solar panel maintenance")
        self.assertTrue(any("research/" in hit for hit in hits), hits)
        self.assertEqual(len(service.library.stats()["categories"]), 1)

    def test_save_requires_human_approval_and_a_previous_report(self) -> None:
        service = self.service()
        self.assertFalse(service.save_last(["1"], confirm=lambda _m: True).ok)
        report = service.investigate("solar", limit=1, source_ids=["wikipedia"])
        self.assertFalse(service.save(report, ["1"]).ok)
        self.assertFalse(service.save(report, [], confirm=lambda _m: True).ok)
        self.assertFalse(service.save(report, ["does-not-exist"], confirm=lambda _m: True).ok)

    def test_tool_query_then_save_flow(self) -> None:
        service = self.service()
        approvals = {"allow": False}
        toolbox = ToolBox(
            Workspace(self.root),
            service.config,
            research=service,
            confirm_action=lambda _message: approvals["allow"],
            log=lambda _message: None,
        )
        query = toolbox.call("research_query", {"query": "solar panel maintenance", "limit": 2})
        self.assertTrue(query.ok, query.content)
        self.assertIn("id:", query.content)
        report = service.last_report
        self.assertIsNotNone(report)
        ids = [report.items[0].id]
        refused = toolbox.call("research_save", {"ids": ids})
        self.assertFalse(refused.ok)
        approvals["allow"] = True
        saved = toolbox.call("research_save", {"ids": ids, "category": "research"})
        self.assertTrue(saved.ok, saved.content)
        self.assertIn("saved ", saved.content)

    def test_selection_accepts_numbers_ids_all_and_none(self) -> None:
        report = ResearchReport("q", [])
        from offgrid.research import ResearchItem

        def item(number):
            return ResearchItem(
                id=f"wikipedia:{number}:1",
                source_id="wikipedia",
                source_title="Wikipedia",
                title=f"Item {number}",
                pageid=number,
                revision=1,
                revision_sha1="x",
                revision_url="https://en.wikipedia.org/w/index.php?oldid=1",
                url="https://en.wikipedia.org/wiki/Item",
                license="CC BY-SA 4.0",
                license_url="https://creativecommons.org/licenses/by-sa/4.0/",
                retrieved_at=0,
                sha256="a" * 64,
                text="text",
                excerpt="text",
                verification=["official MediaWiki API"],
            )

        report.items = [item(1), item(2)]
        self.assertEqual([entry.pageid for entry in report.select("1")], [1])
        self.assertEqual([entry.pageid for entry in report.select(report.items[1].id)], [2])
        self.assertEqual(len(report.select("all")), 2)
        self.assertEqual(report.select("none"), [])

    def test_research_error_is_json_serializable_without_full_text(self) -> None:
        report = self.service().investigate("solar", limit=1, source_ids=["wikipedia"])
        payload = json.loads(json.dumps(report.as_dict()))
        self.assertNotIn("text", payload["items"][0])
        self.assertIn("sha256", payload["items"][0])


if __name__ == "__main__":
    unittest.main()
