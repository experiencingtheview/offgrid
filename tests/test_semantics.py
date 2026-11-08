"""Meaning matching without a model: stems, concepts, n-grams, explanations."""

import json
import tempfile
import unittest
from pathlib import Path

from offgrid.semantics import (
    CONCEPT_GROUPS,
    concepts,
    cosine,
    load_lexicon,
    match,
    rank,
    stem,
    vector,
    words,
)


class StemTests(unittest.TestCase):
    def test_inflections_collapse(self):
        self.assertEqual(stem("deploying"), "deploy")
        self.assertEqual(stem("deployed"), "deploy")
        self.assertEqual(stem("tests"), "test")
        self.assertEqual(stem("checking"), "check")
        self.assertEqual(stem("verifies"), "verify")

    def test_short_words_are_left_alone(self):
        self.assertEqual(stem("sql"), "sql")
        self.assertEqual(stem("api"), "api")


class ConceptTests(unittest.TestCase):
    def test_synonyms_land_in_the_same_group(self):
        self.assertIn("verify", concepts("check that the tests pass"))
        self.assertIn("verify", concepts("the build is validated by pytest"))
        self.assertIn("deploy", concepts("ship it to production"))
        self.assertIn("water", concepts("drinking water"))

    def test_concept_groups_are_readable_and_disjoint(self):
        seen = {}
        for name, members in CONCEPT_GROUPS.items():
            self.assertTrue(members, name)
            for member in members:
                if member in seen:
                    self.assertEqual(seen[member], name, member)
                seen[member] = name

    def test_nothing_is_invented_for_unknown_words(self):
        self.assertEqual(concepts("quantum chromodynamics"), ())


class CustomLexiconTests(unittest.TestCase):
    def load(self, payload):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "lexicon.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_lexicon(path)

    def test_project_file_extends_a_builtin_group(self):
        lexicon = self.load(
            {"groups": {"verify": ["typecheck"], "cloud": ["cloud", "aws", "azure"]}}
        )
        self.assertEqual(lexicon.warnings, ())
        self.assertIn("verify", concepts("typecheck this", lexicon))
        self.assertIn("cloud", concepts("where is the aws account", lexicon))
        found = match("which cloud", "the aws deployment", lexicon)
        self.assertTrue(found.useful)
        self.assertIn("cloud", found.shared)

    def test_custom_lexicon_does_not_leak_into_the_builtin_one(self):
        self.load({"groups": {"cloud": ["aws"]}})
        self.assertEqual(concepts("aws"), ())
        self.assertEqual(concepts("typecheck"), ())

    def test_a_word_already_in_another_group_is_not_moved(self):
        lexicon = self.load({"groups": {"cloud": ["ship"]}})
        self.assertEqual(lexicon.concept_of["ship"], "deploy")
        self.assertTrue(any("already belongs" in warning for warning in lexicon.warnings))
        self.assertIn("deploy", concepts("ship it", lexicon))

    def test_broken_json_falls_back_with_a_warning(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "lexicon.json"
        path.write_text("{not json", encoding="utf-8")
        lexicon = load_lexicon(path)
        self.assertTrue(lexicon.warnings)
        self.assertIn("verify", concepts("check that the tests pass", lexicon))

    def test_direct_group_mapping_is_accepted(self):
        lexicon = self.load({"cloud": ["aws"]})
        self.assertIn("cloud", concepts("aws account", lexicon))

    def test_custom_base_word_matches_its_plural(self):
        lexicon = self.load({"groups": {"billing": ["invoice"]}})
        self.assertIn("billing", concepts("invoices", lexicon))


class MatchTests(unittest.TestCase):
    def test_meaning_beats_exact_wording(self):
        found = match("how do I check that the build works", "Verification command: `make test` (it passed).")
        self.assertTrue(found.useful)
        self.assertIn("verify", found.shared)
        self.assertIn("build", found.shared)
        self.assertTrue(found.reason().startswith("same idea:"))

    def test_synonym_matches_without_a_single_shared_word(self):
        found = match("how do I ship this", "Deployment runs scripts/release.sh")
        self.assertTrue(found.useful)
        self.assertIn("deploy", found.shared)

    def test_morphology_matches(self):
        found = match("deploying the service", "deploy the service")
        self.assertEqual(found.lexical, 1.0)

    def test_typos_still_match(self):
        found = match("verifcation command", "verification command for this project")
        self.assertTrue(found.useful)
        self.assertGreater(found.semantic, 0.4)

    def test_unrelated_text_does_not_match(self):
        self.assertFalse(match("quantum chromodynamics", "The API base url lives in config/settings.py").useful)
        self.assertFalse(match("totally different topic", "something about widgets").useful)

    def test_library_topics(self):
        found = match("how do I make drinking water safe", "To disinfect water, boil for one minute.")
        self.assertTrue(found.useful)
        self.assertIn("water", found.shared)

    def test_empty_input_is_not_a_match(self):
        self.assertFalse(match("", "anything at all").useful)
        self.assertFalse(match("something", "").useful)

    def test_vector_and_cosine_are_deterministic(self):
        first = vector("deploy the service")
        second = vector("deploy the service")
        self.assertEqual(first, second)
        self.assertAlmostEqual(cosine(first, second), 1.0, places=5)
        self.assertLess(cosine(first, vector("boil water")), 0.4)


class RankTests(unittest.TestCase):
    def test_ranks_by_meaning_and_keeps_only_real_matches(self):
        documents = [
            "To disinfect water, boil for one minute.",
            "Deployment runs scripts/release.sh",
            "unrelated note about widgets",
        ]
        ranked = rank("how do I make drinking water safe", documents)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0][0], 0)
        self.assertNotIn(2, [index for index, _match in ranked])


if __name__ == "__main__":
    unittest.main()
