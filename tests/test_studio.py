import gzip
import json
import os
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from offgrid.home import HomeSetupError
from offgrid.studio import StudioServer, _StaticAssetCache

ROOT = Path(__file__).resolve().parent.parent


class FakeRunner:
    def __init__(self):
        self.tasks = []
        self.home_calls = []
        self.config = types.SimpleNamespace(max_tokens=2048, temperature=0.2)

    def info(self):
        return {
            "app": "OffGrid Studio",
            "workspace": str(Path("/tmp/offgrid")),
            "endpoint": "http://127.0.0.1:8080/v1",
            "model": "fake",
            "agents": [
                {"key": "coder", "name": "coder", "title": "who writes", "custom": False},
                {"key": "security", "name": "security", "title": "who audits", "custom": True},
            ],
            "warnings": [],
        }

    def run(self, task):
        self.tasks.append(task)
        return {
            "content": "done: " + task,
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            "meta": {"changed": []},
        }

    def busy(self):
        return False

    def home_status(self):
        return {
            "available": True,
            "connected": False,
            "policy": "read-only",
            "devices": [],
            "warnings": [],
            "token_present": False,
            "defaults": {"url": "http://homeassistant.local:8123", "name": "home"},
        }

    def home_scan(self, url, token):
        self.home_calls.append(("scan", url, token))
        return {
            "ok": True,
            "url": url,
            "entities": [
                {
                    "entity_id": "light.porch",
                    "name": "Porch light",
                    "group": "Lights & switches",
                    "kind": "light",
                    "state": "off",
                    "selected": True,
                    "writable": True,
                }
            ],
            "count": 1,
            "truncated": False,
        }

    def home_connect(self, url, token, name, entities, allow_control=False):
        self.home_calls.append(("connect", url, token, name, entities, allow_control))
        return {
            "ok": True,
            "device": name,
            "points": len(entities),
            "policy": "auto" if allow_control else "read-only",
            "policy_preserved": False,
            "message": "Connected.",
            "status": self.home_status(),
        }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(port, path, method="GET", payload=None, headers=None, body=None):
    data = body if body is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None else None
    )
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.server = StudioServer("127.0.0.1", 0, self.runner)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()
        self.thread.join(timeout=2)

    def test_index_is_branded_and_references_the_studio_assets(self):
        status, headers, body = request(self.port, "/")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("<title>OffGrid Studio</title>", text)
        self.assertIn('id="offgrid-badge"', text)
        self.assertIn('aria-busy="true"', text)
        self.assertIn("offgrid-skeleton-line", text)
        self.assertIn('aria-live="polite"', text)
        self.assertIn('id="offgrid-home-toggle"', text)
        self.assertIn('id="offgrid-home"', text)
        self.assertIn("Long-lived access token", text)
        self.assertIn("Read-only is the default", text)
        self.assertIn("./offgrid.min.css", text)
        self.assertIn("./offgrid.min.js", text)
        self.assertIn('src="./offgrid.min.js" defer fetchpriority="low"', text)
        self.assertLess(text.index("offgrid.min.js"), text.index("</head>"))
        self.assertIn("./offgrid.svg", text)

    def test_loading_skeleton_styles_and_script_are_served(self):
        status, headers, body = request(self.port, "/offgrid.min.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["Content-Type"])
        css = body.decode("utf-8")
        self.assertIn("offgrid-skeleton-shimmer", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("offgrid-home-entities", css)

        status, _, body = request(self.port, "/offgrid.min.js")
        self.assertEqual(status, 200)
        script = body.decode("utf-8")
        self.assertIn('setAttribute("aria-busy"', script)
        self.assertIn('start a local model server to chat', script)
        self.assertIn("/v1/offgrid/home/scan", script)
        self.assertIn("long-lived access token", script.lower())

        # The readable sources are kept next to the generated minified files.
        source = (ROOT / "offgrid" / "web" / "offgrid.js").read_text(encoding="utf-8")
        self.assertIn("setLoading(false)", source)
        self.assertIn("if (loadingState === loading) return", source)
        self.assertIn("if (lastPanelMarkup === markup) return", source)
        self.assertIn("element.textContent !== value", source)
        minified = (ROOT / "offgrid" / "web" / "offgrid.min.js").read_bytes()
        self.assertLess(len(minified), len((ROOT / "offgrid" / "web" / "offgrid.js").read_bytes()))

    def test_home_status_and_scan_endpoints(self):
        status, _, body = request(self.port, "/v1/offgrid/home")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["available"])
        self.assertFalse(payload["connected"])

        status, _, body = request(
            self.port,
            "/v1/offgrid/home/scan",
            "POST",
            {"url": "http://127.0.0.1:8123", "token": "private-token"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["entities"][0]["entity_id"], "light.porch")
        self.assertEqual(
            self.runner.home_calls[-1], ("scan", "http://127.0.0.1:8123", "private-token")
        )
        self.assertNotIn("private-token", body.decode("utf-8"))

    def test_home_connect_endpoint_and_readable_errors(self):
        status, _, body = request(
            self.port,
            "/v1/offgrid/home/connect",
            "POST",
            {
                "url": "http://127.0.0.1:8123",
                "token": "private-token",
                "name": "cabin",
                "entities": ["light.porch"],
                "allow_control": False,
            },
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["device"], "cabin")
        self.assertEqual(self.runner.home_calls[-1][0], "connect")

        original = self.runner.home_scan

        def fail(url, token):
            raise HomeSetupError("Home Assistant rejected that access token.")

        self.runner.home_scan = fail
        try:
            status, _, body = request(
                self.port,
                "/v1/offgrid/home/scan",
                "POST",
                {"url": "http://127.0.0.1:8123", "token": "bad-token"},
            )
        finally:
            self.runner.home_scan = original
        self.assertEqual(status, 400)
        self.assertIn("rejected", json.loads(body)["error"]["message"])
        self.assertNotIn("bad-token", body.decode("utf-8"))

    def test_the_chat_bundle_and_favicon_are_served(self):
        status, headers, body = request(self.port, "/bundle.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertGreater(len(body), 1_000_000)
        script = body.decode("utf-8", errors="replace")
        self.assertIn("__offgridDebounceTimer", script)
        self.assertIn("setTimeout(run2,150)", script)
        self.assertIn("__offgrid_show_more__", script)
        self.assertIn("visibleCount=state$1(20)", script)
        self.assertIn("get$3(visibleCount)!==20", script)
        self.assertIn("older conversation", script)
        self.assertNotIn("pdf_worker_min", script)
        self.assertIn("pdf.worker.mjs", script)
        self.assertGreaterEqual(script.count('loading="lazy"'), 4)
        self.assertGreaterEqual(script.count('decoding="async"'), 5)
        self.assertIn('object-cover" loading="lazy" decoding="async"', script)
        self.assertIn(
            'max-h-[80vh] max-w-[80vw] rounded-lg object-contain shadow-lg" decoding="async"',
            script,
        )

        status, headers, body = request(self.port, "/pdf.worker.mjs")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn("no-cache", headers["Cache-Control"].lower())
        self.assertGreater(len(body), 1_000_000)
        self.assertIn(b"WorkerMessageHandler", body)
        self.assertIn(b"export{WorkerMessageHandler}", body)

        status, headers, _ = request(self.port, "/offgrid.svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", headers["Content-Type"])

    def test_static_cache_reloads_when_the_file_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asset.js"
            path.write_bytes(b"first")
            cache = _StaticAssetCache()
            first, first_etag = cache.get(path)
            self.assertEqual(first, b"first")
            self.assertIs(cache.get(path)[0], first)

            previous = path.stat()
            path.write_bytes(b"second")
            os.utime(
                path,
                ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000),
            )
            second, second_etag = cache.get(path)
            self.assertEqual(second, b"second")
            self.assertNotEqual(second_etag, first_etag)

    def test_static_assets_are_cached_and_revalidated(self):
        status, headers, body = request(self.port, "/bundle.js")
        self.assertEqual(status, 200)
        etag = headers.get("ETag")
        self.assertTrue(etag)
        self.assertGreater(len(body), 1_000_000)

        status, headers, body = request(
            self.port, "/bundle.js", headers={"If-None-Match": etag}
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("ETag"), etag)

    def test_json_api_compresses_responses_and_accepts_gzip_requests(self):
        status, headers, body = request(
            self.port, "/v1/offgrid/info", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(json.loads(gzip.decompress(body))["model"], "fake")

        status, headers, body = request(
            self.port, "/v1/offgrid/info", headers={"Accept-Encoding": "gzip;q=0"}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(json.loads(body)["model"], "fake")

        payload = {
            "model": "offgrid",
            "messages": [{"role": "user", "content": "compressed request"}],
        }
        compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
        status, headers, body = request(
            self.port,
            "/v1/chat/completions",
            "POST",
            body=compressed,
            headers={"Content-Encoding": "gzip", "Accept-Encoding": "identity"},
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(
            json.loads(body)["choices"][0]["message"]["content"],
            "done: compressed request",
        )

    def test_compressed_request_errors_are_readable(self):
        status, _, _ = request(
            self.port,
            "/v1/chat/completions",
            "POST",
            body=b"not gzip",
            headers={"Content-Encoding": "gzip"},
        )
        self.assertEqual(status, 400)

        status, _, _ = request(
            self.port,
            "/v1/chat/completions",
            "POST",
            body=b"{}",
            headers={"Content-Encoding": "br"},
        )
        self.assertEqual(status, 415)

        oversized = gzip.compress(
            json.dumps({"padding": "x" * 1_000_001}).encode("utf-8")
        )
        status, _, _ = request(
            self.port,
            "/v1/chat/completions",
            "POST",
            body=oversized,
            headers={"Content-Encoding": "gzip"},
        )
        self.assertEqual(status, 413)

    def test_models_props_and_info_describe_the_offgrid_team(self):
        status, _, body = request(self.port, "/v1/models")
        models = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(models["data"][0]["id"], "offgrid")

        status, _, body = request(self.port, "/props")
        props = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(props["total_slots"], 1)
        self.assertEqual(props["default_generation_settings"]["n_ctx"], 8192)

        status, _, body = request(self.port, "/v1/offgrid/info")
        info = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(info["model"], "fake")
        self.assertEqual(info["agents"][1]["key"], "security")
        self.assertTrue(info["agents"][1]["custom"])

    def test_a_chat_message_runs_the_team_once(self):
        payload = {
            "model": "offgrid",
            "messages": [
                {"role": "system", "content": "be useful"},
                {"role": "user", "content": "fix the parser"},
            ],
        }
        status, _, body = request(self.port, "/v1/chat/completions", "POST", payload)
        answer = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(answer["choices"][0]["message"]["content"], "done: fix the parser")
        self.assertEqual(answer["usage"]["total_tokens"], 6)
        self.assertEqual(self.runner.tasks, ["fix the parser"])

    def test_streaming_uses_sse_and_finishes_with_done(self):
        payload = {"messages": [{"role": "user", "content": "stream this"}], "stream": True}
        status, headers, body = request(self.port, "/v1/chat/completions", "POST", payload)
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertIn("data: [DONE]", text)
        content = ""
        for line in text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[6:])
                content += chunk["choices"][0]["delta"].get("content", "")
        self.assertEqual(content, "done: stream this")
        self.assertEqual(self.runner.tasks, ["stream this"])

    def test_missing_user_message_and_bad_json_are_rejected(self):
        status, _, body = request(
            self.port, "/v1/chat/completions", "POST", {"messages": []}
        )
        self.assertEqual(status, 400)
        self.assertIn("user message", json.loads(body)["error"]["message"])

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=b"{not json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        else:
            self.fail("malformed JSON was accepted")

    def test_cross_origin_and_plain_text_posts_are_refused(self):
        payload = {"messages": [{"role": "user", "content": "run"}]}
        status, _, body = request(
            self.port,
            "/v1/chat/completions",
            "POST",
            payload,
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", json.loads(body)["error"]["message"])

        status, _, body = request(
            self.port,
            "/v1/chat/completions",
            "POST",
            payload,
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"]["message"])
        self.assertEqual(self.runner.tasks, [])

    def test_path_traversal_is_refused(self):
        status, _, body = request(self.port, "/..%2Foffgrid%2Fconfig.py")
        self.assertEqual(status, 404)
        self.assertNotIn(b"PRINCIPAL", body)

    def test_unknown_api_is_json_404(self):
        status, _, body = request(self.port, "/v1/not-a-route")
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body)["error"]["message"])

    def test_access_key_protects_the_api_and_can_set_a_cookie(self):
        server = StudioServer("127.0.0.1", 0, FakeRunner(), api_key="secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _, body = request(server.port, "/v1/models")
            self.assertEqual(status, 401)
            self.assertIn("access key", json.loads(body)["error"]["message"])

            status, _, body = request(
                server.port, "/v1/models", headers={"Authorization": "Bearer secret"}
            )
            self.assertEqual(status, 200)

            opener = urllib.request.build_opener(NoRedirect)
            try:
                opener.open(f"http://127.0.0.1:{server.port}/?key=secret", timeout=10)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 302)
                self.assertIn("offgrid_studio", exc.headers["Set-Cookie"])
                cookie = exc.headers["Set-Cookie"].split(";", 1)[0]
            else:
                self.fail("the login redirect was followed")

            status, _, _ = request(
                server.port, "/v1/models", headers={"Cookie": cookie}
            )
            self.assertEqual(status, 200)
        finally:
            server.stop()
            thread.join(timeout=2)


class ReplyFormattingTests(unittest.TestCase):
    def test_reply_mentions_changed_files_and_verification(self):
        from offgrid.models import Stats
        from offgrid.orchestrator import AgentRun, RunResult
        from offgrid.studio import _reply

        result = RunResult(
            task="fix",
            summary="fixed it",
            changed=["app.py"],
            verify_command="python3 -m unittest",
            verify_ok=True,
            runs=[AgentRun(agent="coder", turns=2)],
            stats=Stats(prompt_tokens=10, completion_tokens=5),
        )
        reply = _reply(result)
        self.assertIn("fixed it", reply["content"])
        self.assertIn("app.py", reply["content"])
        self.assertIn("Verified", reply["content"])
        self.assertIn("coder (2 turns)", reply["content"])
        self.assertEqual(reply["usage"]["total_tokens"], 15)
