import io
import json
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from offgrid.easy import EasyServer
from offgrid.fullbundle import extract


class FakeController:
    def __init__(self):
        self.install_calls = 0
        self.open_calls = 0
        self._status = {
            "ready": False,
            "stage": "needs-model",
            "message": "No local AI found yet.",
            "error": "",
            "source": "",
            "server_url": "",
            "studio_url": "",
            "workspace": "/tmp/offgrid",
            "app_folder": "/tmp/offgrid",
            "model": "Qwen2.5-Coder 3B",
            "model_key": "3b",
            "model_size": "about 2 GB",
            "progress": {"done": 0, "total": 0, "percent": 0, "label": ""},
        }

    def status(self):
        return dict(self._status)

    def start_install(self):
        self.install_calls += 1
        self._status["stage"] = "preparing"
        return self.status()

    def check(self):
        return self.status()

    def open_studio(self):
        self.open_calls += 1
        return "http://127.0.0.1:8099/"


class EasyServerTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.server = EasyServer(self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.stop()
        self.thread.join(timeout=5)

    def request(self, path, *, method="GET", data=None, headers=None):
        request = urllib.request.Request(
            self.server.url.rstrip("/") + path,
            method=method,
            data=data,
            headers=headers or {},
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_setup_page_and_assets_are_served(self):
        with self.request("/") as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("OffGrid setup", body)
            self.assertIn("Set up OffGrid", body)
            csp = response.headers.get("Content-Security-Policy", "")
            self.assertIn("default-src 'none'", csp)
            self.assertIn("script-src 'self'", csp)
        with self.request("/setup.css") as response:
            self.assertEqual(response.headers.get_content_type(), "text/css")
        with self.request("/setup.js") as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")

    def test_status_is_local_and_not_cached(self):
        with self.request("/api/status") as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["stage"], "needs-model")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_install_and_open_use_the_controller(self):
        with self.request("/api/install", method="POST") as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(self.controller.install_calls, 1)
        with self.request("/api/open", method="POST") as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["studio_url"], "http://127.0.0.1:8099/")
            self.assertEqual(self.controller.open_calls, 1)

    def test_cross_origin_post_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "/api/install",
                method="POST",
                headers={"Origin": "https://example.invalid"},
            )
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.controller.install_calls, 0)

    def test_missing_paths_are_plain_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/../offgrid/easy.py")
        self.assertEqual(caught.exception.code, 404)

    def test_engine_archive_with_a_dot_root_entry_extracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "engine.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.addfile(tarfile.TarInfo("./"))
                payload = b"binary"
                info = tarfile.TarInfo("engine/llama-server")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            extract(archive, root / "out")
            self.assertEqual((root / "out" / "engine" / "llama-server").read_bytes(), b"binary")

    def test_setup_files_exist(self):
        web = Path(__file__).resolve().parent.parent / "offgrid" / "web"
        for name in ("setup.html", "setup.css", "setup.js"):
            self.assertTrue((web / name).is_file(), name)
        page = (web / "setup.html").read_text(encoding="utf-8")
        self.assertIn("Set up OffGrid", page)
        self.assertIn("Check again", page)
        self.assertNotIn("http://", page.split("<body", 1)[-1])


if __name__ == "__main__":
    unittest.main()
