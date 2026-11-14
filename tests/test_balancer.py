"""Multiple model servers: round-robin calls with failover, no cloud surprises."""

import unittest

from offgrid.config import Config
from offgrid.models import Message
from offgrid.providers import (
    LoadBalancedProvider,
    LocalProvider,
    ProviderError,
    open_provider,
)
from tests.fake_llm import serve, text


class LoadBalancedProviderTests(unittest.TestCase):
    def setUp(self):
        self.first = serve([], default=text("answered by first"))
        self.second = serve([], default=text("answered by second"))
        self.config = Config(
            endpoints=(self.first.url, self.second.url),
            model="fake-coder-7b",
            request_timeout=5,
        )
        self.provider = LoadBalancedProvider(self.config)

    def tearDown(self):
        self.first.stop()
        self.second.stop()

    def test_calls_rotate_across_endpoints(self):
        one = self.provider.chat([Message("user", "hello")])
        two = self.provider.chat([Message("user", "hello again")])
        self.assertEqual(one.text, "answered by first")
        self.assertEqual(two.text, "answered by second")
        self.assertEqual(len(self.first.requests), 1)
        self.assertEqual(len(self.second.requests), 1)
        self.assertEqual(self.provider.routes[self.first.url], 1)
        self.assertEqual(self.provider.routes[self.second.url], 1)
        self.assertIn("balance:", self.provider.summary())

    def test_a_failed_endpoint_falls_over_to_the_next_one(self):
        self.first.stop()
        completion = self.provider.chat([Message("user", "hello")])
        self.assertEqual(completion.text, "answered by second")
        self.assertEqual(self.provider.routes, {self.second.url: 1})
        self.assertEqual(self.provider.failures, 1)

    def test_all_endpoints_failing_is_reported_not_hidden(self):
        self.first.stop()
        self.second.stop()
        with self.assertRaises(ProviderError) as ctx:
            self.provider.chat([Message("user", "hello")])
        self.assertIn("all balanced endpoints failed", str(ctx.exception))

    def test_open_provider_selects_balancing_when_endpoints_are_configured(self):
        self.assertIsInstance(open_provider(self.config), LoadBalancedProvider)

    def test_remote_balanced_endpoints_still_require_opt_in(self):
        config = Config(endpoints=("https://api.example.com/v1",))
        with self.assertRaises(ProviderError) as ctx:
            LoadBalancedProvider(config)
        self.assertIn("offline", str(ctx.exception))

    def test_health_reports_how_many_endpoints_answered(self):
        ok, message = self.provider.health()
        self.assertTrue(ok)
        self.assertIn("2/2", message)
        self.assertEqual(self.provider.model, "fake-coder-7b")


class EndpointConfigTests(unittest.TestCase):
    def test_endpoints_are_normalized_and_deduplicated(self):
        config = Config(endpoints=("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"))
        self.assertEqual(config.resolved_endpoints(), ("http://127.0.0.1:8080/v1",))

    def test_the_single_endpoint_remains_the_default(self):
        config = Config(endpoint="http://127.0.0.1:8081/v1/")
        self.assertEqual(config.resolved_endpoint(), "http://127.0.0.1:8081/v1")
        self.assertEqual(LocalProvider(config).endpoint, "http://127.0.0.1:8081/v1")

    def test_empty_endpoint_entries_fall_back_to_the_single_endpoint(self):
        config = Config(endpoints=("",), endpoint="http://127.0.0.1:8081/v1")
        self.assertEqual(config.resolved_endpoints(), ("http://127.0.0.1:8081/v1",))


if __name__ == "__main__":
    unittest.main()
