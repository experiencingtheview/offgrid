import io
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from offgrid.vault import (
    VaultError,
    available,
    decrypt_path,
    encrypt_path,
    guard,
    plan,
    seal,
    unseal,
)

PASS = "correct horse battery staple"


class GuardTests(unittest.TestCase):
    def test_refuses_system_locations(self):
        with self.assertRaises(VaultError):
            guard(Path("/"))

    def test_refuses_home(self):
        with self.assertRaises(VaultError):
            guard(Path.home())

    def test_refuses_workspace_and_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            target = workspace / "data.txt"
            target.write_text("x", encoding="utf-8")
            with self.assertRaises(VaultError):
                guard(target, workspace=workspace)
            with self.assertRaises(VaultError):
                guard(Path(tmp), workspace=workspace)

    def test_refuses_state_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".git"
            state.mkdir()
            with self.assertRaises(VaultError):
                guard(state)


@unittest.skipUnless(available(), "openssl is not installed")
class CryptoRoundTripTests(unittest.TestCase):
    def test_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.txt"
            source.write_text("secret plans\n", encoding="utf-8")
            blob = encrypt_path(source, PASS)
            self.assertEqual(blob.suffix, ".ogv")
            self.assertNotIn(b"secret plans", blob.read_bytes())
            restored = decrypt_path(blob, PASS, out=root / "restored.txt")
            self.assertEqual(restored.read_text(encoding="utf-8"), "secret plans\n")

    def test_wrong_passphrase_and_tampering_are_rejected(self):
        data = b"authenticated payload"
        blob = seal(data, PASS)
        with self.assertRaises(VaultError):
            unseal(blob, "not the passphrase")
        damaged = bytearray(blob)
        damaged[len(damaged) // 2] ^= 0x01
        with self.assertRaises(VaultError):
            unseal(bytes(damaged), PASS)

    def test_folder_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "project"
            (folder / "src").mkdir(parents=True)
            (folder / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
            blob = encrypt_path(folder, PASS)
            shutil.rmtree(folder)
            restored = decrypt_path(blob, PASS)
            self.assertEqual(restored, folder)
            self.assertEqual((folder / "src" / "app.py").read_text(encoding="utf-8"), "print('hi')\n")

    def test_passphrase_minimum_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.txt"
            source.write_text("x", encoding="utf-8")
            with self.assertRaises(VaultError):
                encrypt_path(source, "short")

    def test_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.txt"
            source.write_text("x", encoding="utf-8")
            encrypt_path(source, PASS)
            with self.assertRaises(VaultError):
                encrypt_path(source, PASS)

    def test_archive_extraction_skips_traversal_and_links(self):
        from offgrid.vault import _safe_members

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            good = tarfile.TarInfo("good.txt")
            payload = b"ok"
            good.size = len(payload)
            archive.addfile(good, io.BytesIO(payload))
            bad = tarfile.TarInfo("../escape.txt")
            bad.size = 1
            archive.addfile(bad, io.BytesIO(b"x"))
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "out"
            destination.mkdir()
            with tarfile.open(fileobj=io.BytesIO(buffer.getvalue()), mode="r:gz") as archive:
                names = [member.name for member in _safe_members(archive, destination)]
            self.assertEqual(names, ["good.txt"])


class PlanTests(unittest.TestCase):
    def test_plan_refuses_full_disk_and_names_the_os_route(self):
        text = plan()
        self.assertIn("will not encrypt the running system", text)
        self.assertTrue(any(name in text for name in ("BitLocker", "FileVault", "LUKS")))
        self.assertIn("does not erase the plaintext", text)


if __name__ == "__main__":
    unittest.main()
