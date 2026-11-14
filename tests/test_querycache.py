"""The small process-local cache used by library and memory searches."""

import unittest

from offgrid.querycache import QueryCache


class QueryCacheTests(unittest.TestCase):
    def test_hits_and_misses_are_counted(self):
        cache = QueryCache(max_entries=2)
        self.assertIsNone(cache.get("missing"))
        cache.put("one", [1])
        self.assertEqual(cache.get("one"), [1])
        self.assertEqual(
            cache.stats(),
            {"entries": 1, "max_entries": 2, "hits": 1, "misses": 1},
        )

    def test_least_recently_used_entry_is_evicted(self):
        cache = QueryCache(max_entries=2)
        cache.put("one", 1)
        cache.put("two", 2)
        self.assertEqual(cache.get("one"), 1)
        cache.put("three", 3)

        self.assertIsNone(cache.get("two"))
        self.assertEqual(cache.get("one"), 1)
        self.assertEqual(cache.get("three"), 3)
        self.assertEqual(cache.stats()["entries"], 2)

    def test_clear_removes_entries_but_keeps_lifetime_counters(self):
        cache = QueryCache(max_entries=2)
        cache.put("one", 1)
        cache.get("one")
        cache.clear()
        self.assertIsNone(cache.get("one"))
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertGreaterEqual(cache.stats()["hits"], 1)


if __name__ == "__main__":
    unittest.main()
