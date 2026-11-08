"""The machine tells the truth about itself, and the tier follows from that."""

import json
import tempfile
import unittest
from pathlib import Path

from offgrid.hardware import (
    Capability,
    classify,
    fingerprint,
    observe,
    probe,
    ram_gb,
    recommend,
)

BASE = {
    "os": "Linux",
    "machine": "x86_64",
    "cpu_count": 8,
    "cpu_model": "AMD Ryzen 7",
    "ram_gb": 16.0,
    "disk_free_gb": 200.0,
    "accelerators": [],
    "vram_gb": 0.0,
    "android": False,
}


def raw(**overrides):
    data = dict(BASE)
    data.update(overrides)
    return data


class ClassifyTests(unittest.TestCase):
    def test_a_big_gpu_makes_a_workstation(self):
        capability = classify(raw(accelerators=["nvidia:RTX 4090 24GB"], vram_gb=24.0))
        self.assertEqual(capability.tier, "workstation")
        self.assertTrue(capability.gpu)

    def test_a_plain_laptop_is_modest(self):
        self.assertEqual(classify(raw()).tier, "modest")

    def test_a_small_machine_is_minimal(self):
        capability = classify(raw(cpu_count=4, ram_gb=4.0))
        self.assertEqual(capability.tier, "minimal")
        self.assertTrue(any("3B" in note for note in capability.notes))

    def test_eight_gb_of_vram_is_capable(self):
        self.assertEqual(classify(raw(vram_gb=8.0, ram_gb=16.0)).tier, "capable")

    def test_an_android_device_is_flagged(self):
        capability = classify(raw(android=True, ram_gb=6.0))
        self.assertTrue(capability.android)
        self.assertTrue(any("phone" in note for note in capability.notes))

    def test_missing_probes_are_reported_not_invented(self):
        capability = classify({"os": "Linux", "machine": "x86_64"})
        self.assertEqual(capability.ram_gb, 0.0)
        self.assertEqual(capability.cpu_count, 1)
        self.assertIn("tier", capability.as_dict())

    def test_score_rises_with_hardware(self):
        small = classify(raw(cpu_count=2, ram_gb=4.0))
        large = classify(raw(cpu_count=16, ram_gb=64.0, vram_gb=24.0))
        self.assertLess(small.score, large.score)

    def test_prompt_line_says_what_is_and_is_not_there(self):
        line = classify(raw()).line()
        self.assertIn("tier=modest", line)
        self.assertIn("no GPU", line)
        self.assertIn("16 GB RAM", line)
        gpu_line = classify(raw(accelerators=["nvidia:RTX 3060 12GB"], vram_gb=12.0)).line()
        self.assertIn("RTX 3060", gpu_line)

    def test_description_is_complete(self):
        text = "\n".join(classify(raw()).describe())
        for expected in ("os ", "cpu ", "memory", "graphics", "tier"):
            self.assertIn(expected, text)


class RecommendTests(unittest.TestCase):
    def recommend_for(self, **overrides):
        return recommend(classify(raw(**overrides)))

    def test_large_vram_gets_a_large_model(self):
        self.assertIn("32B", self.recommend_for(vram_gb=24.0, accelerators=["nvidia:x"]))

    def test_mid_vram_gets_fourteen_b(self):
        self.assertIn("14B", self.recommend_for(vram_gb=8.0, accelerators=["nvidia:x"]))

    def test_cpu_only_laptop_gets_seven_b(self):
        self.assertIn("7B", self.recommend_for())

    def test_tiny_machine_is_told_to_join_a_fleet(self):
        self.assertIn("fleet", self.recommend_for(ram_gb=4.0, cpu_count=4))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_observe_reports_this_machine(self):
        data = observe(str(self.root))
        for key in ("os", "machine", "cpu_count", "ram_gb", "accelerators", "vram_gb"):
            self.assertIn(key, data)
        self.assertGreaterEqual(data["cpu_count"], 1)
        self.assertGreaterEqual(ram_gb(), 0.0)

    def test_probe_is_cached_and_reused(self):
        first = probe(str(self.root))
        cache = self.root / ".offgrid" / "hardware.json"
        self.assertTrue(cache.is_file())
        cached = json.loads(cache.read_text())
        self.assertEqual(cached["fingerprint"], fingerprint(cached["raw"]))
        second = probe(str(self.root))
        self.assertEqual(first.tier, second.tier)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_fingerprint_ignores_disk_but_not_memory(self):
        self.assertEqual(fingerprint(raw(disk_free_gb=10)), fingerprint(raw(disk_free_gb=999)))
        self.assertNotEqual(fingerprint(raw(ram_gb=16)), fingerprint(raw(ram_gb=64)))

    def test_capability_round_trips_as_json(self):
        capability = Capability(os_name="Linux", machine="aarch64", cpu_count=8, ram_gb=8, tier="modest")
        self.assertEqual(json.loads(json.dumps(capability.as_dict()))["tier"], "modest")


if __name__ == "__main__":
    unittest.main()
