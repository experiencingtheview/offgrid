"""Focused checks for the install manifest schema and verifier."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from offgrid import install_manifest


def _write(root: Path, relative: str, data: bytes, mode: int = 0o644) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _make_bundle(root: Path) -> str:
    _write(root, "offgrid/__init__.py", b"")
    _write(root, "offgrid/cli.py", b"print('x')\n")
    _write(root, "launch/offgrid.sh", b"#!/bin/sh\n", 0o755)
    _write(root, "OffGrid.sh", b"#!/bin/sh\n", 0o755)
    _write(root, "OffGrid.command", b"#!/bin/sh\n", 0o755)
    _write(root, "OffGrid.bat", b"@echo off\n")
    _write(root, "README-FIRST.txt", b"start here\n")
    _write(root, "config.json", b"{}\n")
    _write(root, "MODEL.txt", b"test.gguf\n")
    _write(root, ".offgrid/session", b"private\n")
    _write(root, ".offgrid-install/transaction.lock", b"private\n")
    _write(root, ".downloads/archive.zip", b"not part of the manifest\n")
    _write(root, "__pycache__/ignored.pyc", b"bytecode\n")
    _write(root, "models/test.gguf", b"fake gguf")
    _write(root, "runtime/python/bin/python3", b"python binary", 0o755)
    _write(root, "runtime/llama/llama-server", b"llama binary", 0o755)
    fingerprint = {
        "schema": 1,
        "platform": "linux-x64",
        "version": "test-llama",
        "archive_sha256": "b" * 64,
        "binary_sha256": hashlib.sha256(b"llama binary").hexdigest(),
    }
    _write(
        root,
        "runtime/llama/.offgrid-runtime.json",
        (json.dumps(fingerprint) + "\n").encode("utf-8"),
    )
    return hashlib.sha256(b"fake gguf").hexdigest()


def _build(root: Path) -> dict:
    model_hash = _make_bundle(root)
    return install_manifest.build_manifest(
        root,
        artifact_kind="full",
        platform="linux-x64",
        created="2026-01-02T03:04:05Z",
        model_hashes={"test.gguf": model_hash},
    )


def _zip_tree(root: Path, destination: Path, prefix: str) -> None:
    ignored = {
        ".downloads",
        ".git",
        ".offgrid",
        ".offgrid-install",
        ".pytest_cache",
        "__pycache__",
    }
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_file() and not any(part in ignored for part in relative.parts):
                archive.write(path, prefix + relative.as_posix())


class ManifestBuildTests(unittest.TestCase):
    def test_manifest_separates_files_state_runtimes_and_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _build(root)

            self.assertEqual(manifest["schema"], install_manifest.SCHEMA)
            self.assertEqual(manifest["artifact"]["kind"], "full")
            self.assertEqual(manifest["artifact"]["platform"], "linux-x64")
            self.assertEqual(manifest["artifact"]["created"], "2026-01-02T03:04:05Z")
            self.assertEqual(manifest["entrypoints"]["app"], "offgrid/cli.py")

            files = {entry["path"]: entry for entry in manifest["files"]}
            self.assertIn("offgrid/cli.py", files)
            self.assertIn("OffGrid.sh", files)
            self.assertNotIn("config.json", files)
            self.assertNotIn("MODEL.txt", files)
            self.assertNotIn("models/test.gguf", files)
            self.assertFalse(
                any(path.startswith(("runtime/", ".offgrid-install/")) for path in files)
            )
            self.assertTrue(files["OffGrid.sh"]["executable"])

            state = {entry["path"]: entry for entry in manifest["user_state"]}
            self.assertEqual(state["config.json"]["kind"], "config")
            self.assertEqual(state["MODEL.txt"]["kind"], "metadata")
            self.assertTrue(state["config.json"]["present"])

            runtimes = {
                entry["path"]: entry for entry in manifest["runtimes"]
            }
            self.assertEqual(
                set(runtimes), {"runtime/llama", "runtime/python"}
            )
            self.assertEqual(runtimes["runtime/llama"]["version"], "test-llama")
            self.assertEqual(
                runtimes["runtime/llama"]["fingerprint"],
                "runtime/llama/.offgrid-runtime.json",
            )
            self.assertEqual(
                runtimes["runtime/python"]["binary"], "bin/python3"
            )
            self.assertEqual(
                runtimes["runtime/python"]["binary_sha256"],
                hashlib.sha256(b"python binary").hexdigest(),
            )

            self.assertEqual(len(manifest["models"]), 1)
            self.assertEqual(
                manifest["models"][0]["sha256"],
                hashlib.sha256(b"fake gguf").hexdigest(),
            )
            self.assertEqual(manifest["totals"]["files"], len(files))
            self.assertEqual(
                manifest["totals"]["bytes"],
                sum(entry["size"] for entry in manifest["files"]),
            )
            self.assertEqual(manifest["totals"]["runtimes"], 2)
            self.assertEqual(manifest["totals"]["models"], 1)
            self.assertIn("runtime/**", manifest["excluded"])
            self.assertEqual(len(install_manifest.manifest_digest(manifest)), 64)

    def test_write_and_load_are_atomic_and_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _build(root)
            path = install_manifest.write_manifest(root, manifest)

            self.assertEqual(path, root / install_manifest.MANIFEST_NAME)
            self.assertEqual(
                path.read_bytes(), install_manifest.manifest_bytes(manifest)
            )
            self.assertEqual(
                install_manifest.load_manifest(path), manifest
            )
            self.assertEqual(
                install_manifest.manifest_digest(manifest),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

    def test_validation_rejects_unsafe_or_inconsistent_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = _build(root)

            def invalid(mutate, message):
                manifest = copy.deepcopy(original)
                mutate(manifest)
                with self.assertRaisesRegex(install_manifest.ManifestError, message):
                    install_manifest.validate_manifest(manifest)

            invalid(
                lambda data: data["files"][0].__setitem__("path", "../escape.py"),
                "unsafe component",
            )
            invalid(
                lambda data: data["files"][0].__setitem__("path", "/tmp/escape.py"),
                "must be relative",
            )
            invalid(
                lambda data: data["files"][0].__setitem__("path", "dir\\escape.py"),
                "POSIX separators",
            )
            invalid(
                lambda data: data["files"][0].__setitem__(
                    "path", install_manifest.MANIFEST_NAME
                ),
                "must not list itself",
            )
            invalid(
                lambda data: data["files"][1].__setitem__(
                    "path", data["files"][0]["path"].upper()
                ),
                "case-folded path collision",
            )
            invalid(
                lambda data: data["totals"].__setitem__("files", 999),
                "totals.files",
            )
            invalid(
                lambda data: data["totals"].__setitem__("files", True),
                "non-negative integer",
            )
            invalid(
                lambda data: data["entrypoints"].__setitem__("app", "missing.pyz"),
                "not in the immutable file list",
            )
            invalid(
                lambda data: next(
                    entry
                    for entry in data["files"]
                    if entry["path"] == "offgrid/cli.py"
                ).__setitem__("required", False),
                "must be a required file",
            )
            invalid(
                lambda data: data["files"][0].__setitem__("sha256", "not-a-hash"),
                "lowercase SHA-256",
            )
            invalid(
                lambda data: data["files"].reverse(),
                "must be sorted",
            )

    @unittest.skipUnless(os.name == "posix", "symlinks are POSIX-only")
    def test_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build(root)
            (root / "offgrid" / "alias.py").symlink_to(root / "offgrid" / "cli.py")
            with self.assertRaisesRegex(
                install_manifest.ManifestError, "symlink"
            ):
                install_manifest.build_manifest(
                    root, artifact_kind="full", platform="linux-x64"
                )


class ManifestVerifyTests(unittest.TestCase):
    def test_mutable_state_is_reported_but_hashed_files_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _build(root)
            install_manifest.write_manifest(root, manifest)

            clean = install_manifest.verify_manifest(root)
            self.assertTrue(clean.ok)
            self.assertEqual(clean.checked_files, manifest["totals"]["files"])
            self.assertEqual(clean.checked_bytes, manifest["totals"]["bytes"])
            self.assertEqual(clean.user_state_changed, ())

            (root / "config.json").write_text('{"offline": false}\n', encoding="utf-8")
            changed = install_manifest.verify_manifest(root)
            self.assertTrue(changed.ok)
            self.assertEqual(changed.user_state_changed, ("config.json",))

            (root / "offgrid" / "cli.py").write_bytes(b"print('y')\n")
            tampered = install_manifest.verify_manifest(root)
            self.assertFalse(tampered.ok)
            self.assertIn("SHA-256 mismatch: offgrid/cli.py", tampered.errors)
            unchecked = install_manifest.verify_manifest(root, check_hashes=False)
            self.assertTrue(unchecked.ok)

            (root / "OffGrid.sh").unlink()
            missing = install_manifest.verify_manifest(root)
            self.assertIn("missing file: OffGrid.sh", missing.errors)

    def test_assets_are_hashed_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _build(root)
            install_manifest.write_manifest(root, manifest)

            (root / "models" / "test.gguf").write_bytes(b"fake GGUF")
            (root / "runtime" / "llama" / "llama-server").write_bytes(
                b"llama binari"
            )
            quick = install_manifest.verify_manifest(root)
            self.assertTrue(quick.ok)
            self.assertEqual(quick.checked_assets, 0)

            full = install_manifest.verify_manifest(root, check_assets=True)
            self.assertFalse(full.ok)
            self.assertGreaterEqual(full.checked_assets, 3)
            self.assertIn("SHA-256 mismatch: models/test.gguf", full.errors)
            self.assertIn(
                "SHA-256 mismatch: runtime/llama/llama-server", full.errors
            )

            fingerprint = root / "runtime" / "llama" / ".offgrid-runtime.json"
            fingerprint.write_text(
                fingerprint.read_text(encoding="utf-8").replace(
                    "test-llama", "test-llamX"
                ),
                encoding="utf-8",
            )
            fingerprint_check = install_manifest.verify_manifest(
                root, check_assets=True
            )
            self.assertIn(
                "SHA-256 mismatch: runtime/llama/.offgrid-runtime.json",
                fingerprint_check.errors,
            )

    def test_verify_manifest_rejects_extra_files_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _build(root)
            install_manifest.write_manifest(root, manifest)
            (root / "offgrid" / "extra.py").write_text(
                "# unlisted\n", encoding="utf-8"
            )

            self.assertTrue(install_manifest.verify_manifest(root).ok)
            strict = install_manifest.verify_manifest(root, check_extra=True)
            self.assertFalse(strict.ok)
            self.assertIn("unexpected file: offgrid/extra.py", strict.errors)

    def test_verify_manifest_honors_optional_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _build(root)
            next(
                entry
                for entry in manifest["files"]
                if entry["path"] == "README-FIRST.txt"
            )["required"] = False
            install_manifest.write_manifest(root, manifest)
            (root / "README-FIRST.txt").unlink()

            report = install_manifest.verify_manifest(root)
            self.assertTrue(report.ok)
            self.assertNotIn("missing file: README-FIRST.txt", report.errors)

    @unittest.skipUnless(os.name == "posix", "symlinks are POSIX-only")
    def test_verify_manifest_rejects_a_symlinked_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "bundle"
            base.mkdir()
            manifest = _build(base)
            install_manifest.write_manifest(base, manifest)
            alias = Path(tmp) / "alias"
            alias.symlink_to(base, target_is_directory=True)
            with self.assertRaisesRegex(install_manifest.ManifestError, "symlink"):
                install_manifest.verify_manifest(alias)

    def test_verify_zip_hashes_the_manifest_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir()
            manifest = _build(root)
            install_manifest.write_manifest(root, manifest)
            archive = Path(tmp) / "offgrid-test.zip"
            _zip_tree(root, archive, "offgrid-test/")

            report = install_manifest.verify_zip(archive)
            self.assertTrue(report.ok)
            self.assertEqual(report.checked_files, manifest["totals"]["files"])
            self.assertEqual(report.checked_bytes, manifest["totals"]["bytes"])

            tampered_root = Path(tmp) / "tampered"
            shutil.copytree(root, tampered_root)
            (tampered_root / "offgrid" / "cli.py").write_bytes(b"print('y')\n")
            bad_archive = Path(tmp) / "offgrid-test-tampered.zip"
            _zip_tree(tampered_root, bad_archive, "offgrid-test/")

            bad = install_manifest.verify_zip(bad_archive)
            self.assertFalse(bad.ok)
            self.assertIn("SHA-256 mismatch: offgrid/cli.py", bad.errors)

    def test_verify_zip_rejects_unlisted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir()
            manifest = _build(root)
            install_manifest.write_manifest(root, manifest)
            archive = Path(tmp) / "offgrid-test.zip"
            _zip_tree(root, archive, "offgrid-test/")
            with zipfile.ZipFile(archive, "a") as handle:
                handle.writestr("offgrid-test/extra.py", b"unlisted\n")

            report = install_manifest.verify_zip(archive)
            self.assertFalse(report.ok)
            self.assertIn("unexpected file: extra.py", report.errors)

    def test_verify_zip_rejects_unsafe_and_duplicate_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.zip"
            with self.assertRaisesRegex(
                install_manifest.ManifestError, "not a valid zip"
            ):
                install_manifest.verify_zip(missing)

            not_zip = Path(tmp) / "not-a-zip.zip"
            not_zip.write_bytes(b"not a zip\n")
            with self.assertRaisesRegex(
                install_manifest.ManifestError, "not a valid zip"
            ):
                install_manifest.verify_zip(not_zip)

            unsafe = Path(tmp) / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as handle:
                handle.writestr("../escape.py", b"escape\n")
            with self.assertRaisesRegex(install_manifest.ManifestError, "unsafe"):
                install_manifest.verify_zip(unsafe)

            drive = Path(tmp) / "drive.zip"
            with zipfile.ZipFile(drive, "w") as handle:
                handle.writestr("C:escape.py", b"escape\n")
            with self.assertRaisesRegex(install_manifest.ManifestError, "unsafe"):
                install_manifest.verify_zip(drive)

            duplicate = Path(tmp) / "duplicate.zip"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "w") as handle:
                    handle.writestr("offgrid-test/install-manifest.json", b"{}")
                    handle.writestr("offgrid-test/install-manifest.json", b"{}")
            with self.assertRaisesRegex(install_manifest.ManifestError, "duplicate"):
                install_manifest.verify_zip(duplicate)

            symlink = Path(tmp) / "symlink.zip"
            info = zipfile.ZipInfo("offgrid-test/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(symlink, "w") as handle:
                handle.writestr(info, "offgrid/cli.py")
            with self.assertRaisesRegex(install_manifest.ManifestError, "symlink"):
                install_manifest.verify_zip(symlink)

    def test_verify_zip_requires_one_canonical_manifest_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir()
            manifest = _build(root)
            install_manifest.write_manifest(root, manifest)

            nested = Path(tmp) / "nested.zip"
            _zip_tree(root, nested, "outer/inner/")
            with self.assertRaisesRegex(install_manifest.ManifestError, "top-level"):
                install_manifest.verify_zip(nested)

            duplicate_manifest = Path(tmp) / "duplicate-manifest.zip"
            _zip_tree(root, duplicate_manifest, "offgrid-test/")
            with zipfile.ZipFile(duplicate_manifest, "a") as handle:
                handle.writestr("docs/install-manifest.json", b"{}")
            with self.assertRaisesRegex(install_manifest.ManifestError, "exactly one"):
                install_manifest.verify_zip(duplicate_manifest)

    def test_verify_zip_checks_runtime_and_model_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir()
            manifest = _build(root)
            install_manifest.write_manifest(root, manifest)
            (root / "models" / "test.gguf").write_bytes(b"fake GGUF")
            (root / "runtime" / "llama" / "llama-server").write_bytes(
                b"llama binari"
            )
            fingerprint = root / "runtime" / "llama" / ".offgrid-runtime.json"
            fingerprint.write_text(
                fingerprint.read_text(encoding="utf-8").replace(
                    "test-llama", "test-llamX"
                ),
                encoding="utf-8",
            )
            archive = Path(tmp) / "offgrid-test.zip"
            _zip_tree(root, archive, "offgrid-test/")

            quick = install_manifest.verify_zip(archive)
            self.assertTrue(quick.ok)
            self.assertEqual(quick.checked_assets, 0)

            full = install_manifest.verify_zip(archive, check_assets=True)
            self.assertFalse(full.ok)
            self.assertGreaterEqual(full.checked_assets, 3)
            self.assertIn("SHA-256 mismatch: models/test.gguf", full.errors)
            self.assertIn(
                "SHA-256 mismatch: runtime/llama/llama-server", full.errors
            )
            self.assertIn(
                "SHA-256 mismatch: runtime/llama/.offgrid-runtime.json",
                full.errors,
            )


if __name__ == "__main__":
    unittest.main()
