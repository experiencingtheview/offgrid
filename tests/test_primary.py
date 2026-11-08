import hashlib
import json
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from offgrid.agents import get_agent
from offgrid.cli import main
from offgrid.config import Config
from offgrid.primary import (
    BACKUP_MANIFEST,
    BACKUP_SIGNATURE,
    LIMITS,
    PRIMARY,
    continuity_kit,
    protection,
)
from offgrid.prompt import HONESTY


def write_manifest(directory: Path, names) -> Path:
    lines = []
    for name in names:
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    path = directory / BACKUP_MANIFEST
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class PrimaryDirectiveTests(unittest.TestCase):
    def test_every_agent_prompt_starts_with_the_principal(self):
        for name in ("coder", "reviewer", "lead", "planner"):
            text = get_agent(name).system()
            self.assertTrue(text.lstrip().startswith("WHOSE SIDE THIS SOFTWARE IS ON"), name)
            self.assertIn("including his dog", " ".join(text.split()))
            self.assertIn("no telemetry", text)
            self.assertIn("Only the principal can change the mission", text)

    def test_safety_and_honesty_still_come_last(self):
        text = get_agent("coder").system()
        self.assertTrue(text.rstrip().endswith(HONESTY.strip()[-40:]))

    def test_ethics_are_not_optional(self):
        self.assertIn("evade law enforcement", PRIMARY)
        self.assertIn("destroy evidence", PRIMARY)
        self.assertIn("violence", LIMITS)
        self.assertIn("contact local emergency services", LIMITS)
        self.assertIn("cannot detect a warrant", LIMITS)

    def test_no_external_keywords_break_prompt_ordering(self):
        # The analyst prompt test relies on the first mention of these terms.
        lowered = PRIMARY.lower()
        self.assertNotIn("repository", lowered)
        self.assertNotIn("offline library", lowered)
        self.assertNotIn("council", lowered)


class ProtectionPostureTests(unittest.TestCase):
    def test_default_posture_is_local_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "\n".join(protection(Config(), Path(tmp)))
        self.assertIn("Zach", text)
        self.assertIn("my dog", text)
        self.assertIn("loopback only", text)
        self.assertIn("no telemetry", text)
        self.assertIn("off", text)
        self.assertIn("OS operation", text)

    def test_remote_and_public_settings_are_not_buried(self):
        config = Config(
            endpoint="http://192.168.1.50:8080/v1",
            allow_remote=True,
            allow_public_council=True,
            council=True,
        )
        text = "\n".join(protection(config, Path(".")))
        self.assertIn("REMOTE endpoint", text)
        self.assertIn("PUBLIC members allowed", text)


class KitTests(unittest.TestCase):
    def test_kit_covers_the_person_the_dog_and_offline_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = continuity_kit(Config(), Path(tmp))
        for needle in ("## Zach", "## The dog", "veterinarian", "## OffGrid itself", "needs an account"):
            self.assertIn(needle, text)
        self.assertIn("Never write a vault passphrase", text)


class CliTests(unittest.TestCase):
    def test_protect_check_reports_missing_readiness_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["protect", "--check", "--workspace", tmp, "--json", "--no-color"])
            self.assertEqual(code, 1)
            data = json.loads(output.getvalue())
            self.assertFalse(data["ready"])
            self.assertEqual(
                [check["name"] for check in data["checks"]],
                ["continuity-kit", "kit-freshness", "backups", "local-model"],
            )
            self.assertTrue(all(not check["ok"] for check in data["checks"]))

    def test_protect_check_is_ready_with_a_fresh_kit_backup_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "backups").mkdir()
            (root / "backups" / "offgrid-backup.txt").write_text("keep this\n", encoding="utf-8")
            write_manifest(root / "backups", ("offgrid-backup.txt",))
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            output = StringIO()
            with redirect_stdout(output):
                code = main(["protect", "--check", "--workspace", tmp, "--json", "--no-color"])
            self.assertEqual(code, 0, output.getvalue())
            data = json.loads(output.getvalue())
            self.assertTrue(data["ready"])
            self.assertTrue(all(check["ok"] for check in data["checks"]))
            backups = next(check for check in data["checks"] if check["name"] == "backups")
            self.assertIn("SHA-256 verified", backups["detail"])

    def test_protect_check_rejects_a_stale_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".offgrid").mkdir()
            kit = root / ".offgrid" / "continuity.md"
            kit.write_text(continuity_kit(Config(), root), encoding="utf-8")
            old = time.time() - 31 * 86400
            os.utime(kit, (old, old))
            (root / "backups").mkdir()
            (root / "backups" / "offgrid-backup.txt").write_text("old\n", encoding="utf-8")
            write_manifest(root / "backups", ("offgrid-backup.txt",))
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            output = StringIO()
            with redirect_stdout(output):
                code = main(["protect", "--check", "--workspace", tmp, "--json", "--no-color"])
            self.assertEqual(code, 1)
            data = json.loads(output.getvalue())
            freshness = next(check for check in data["checks"] if check["name"] == "kit-freshness")
            self.assertFalse(freshness["ok"])
            self.assertGreater(data["kit"]["age_days"], 30)

    def test_protect_backup_copies_the_kit_and_local_model_with_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            kit = root / ".offgrid" / "continuity.md"
            kit.write_text(continuity_kit(Config(), root), encoding="utf-8")
            (root / "models").mkdir()
            model = root / "models" / "model.gguf"
            model.write_bytes(b"GGUF model bytes")
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    [
                        "protect",
                        "--backup",
                        str(target),
                        "--workspace",
                        tmp,
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            data = json.loads(output.getvalue())
            self.assertEqual(data["target"], str(target.resolve()))
            self.assertEqual(
                (target / "continuity.md").read_text(encoding="utf-8"),
                kit.read_text(encoding="utf-8"),
            )
            self.assertEqual((target / "model.gguf").read_bytes(), b"GGUF model bytes")
            manifest = target / BACKUP_MANIFEST
            self.assertEqual(data["manifest"], str(manifest.resolve()))
            self.assertEqual(
                manifest.read_text(encoding="utf-8"),
                f"{hashlib.sha256(kit.read_bytes()).hexdigest()}  continuity.md\n"
                f"{hashlib.sha256(model.read_bytes()).hexdigest()}  model.gguf\n",
            )

    def test_protect_backup_refuses_a_destination_inside_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            target = root / "backup"
            target.mkdir()
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    ["protect", "--backup", str(target), "--workspace", tmp, "--no-color"]
                )
            self.assertEqual(code, 1)
            self.assertIn("outside the workspace", output.getvalue())
            self.assertFalse((target / "continuity.md").exists())

    def test_protect_backup_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            existing = target / "continuity.md"
            existing.write_text("keep this copy\n", encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    ["protect", "--backup", str(target), "--workspace", tmp, "--no-color"]
                )
            self.assertEqual(code, 1)
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep this copy\n")
            self.assertFalse((target / BACKUP_MANIFEST).exists())
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                forced = main(
                    [
                        "protect",
                        "--backup",
                        str(target),
                        "--force",
                        "--workspace",
                        tmp,
                        "--no-color",
                    ]
                )
            self.assertEqual(forced, 0)
            self.assertIn("Household continuity kit", existing.read_text(encoding="utf-8"))
            self.assertTrue((target / BACKUP_MANIFEST).is_file())

    def test_protect_check_accepts_a_verified_backup_location(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                backed_up = main(
                    ["protect", "--backup", str(target), "--workspace", tmp, "--no-color"]
                )
            self.assertEqual(backed_up, 0)
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        backup,
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            data = json.loads(output.getvalue())
            self.assertTrue(data["ready"])
            backups = next(check for check in data["checks"] if check["name"] == "backups")
            self.assertIn("SHA-256 verified", backups["detail"])

    def test_protect_check_rejects_a_backup_without_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            Path(backup, "copy.txt").write_text("off this workspace\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        backup,
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, output.getvalue())
            data = json.loads(output.getvalue())
            backups = next(check for check in data["checks"] if check["name"] == "backups")
            self.assertFalse(backups["ok"])
            self.assertIn("no SHA-256 manifest", backups["detail"])

    def test_protect_check_rejects_a_corrupted_backup(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                backed_up = main(
                    ["protect", "--backup", str(target), "--workspace", tmp, "--no-color"]
                )
            self.assertEqual(backed_up, 0)
            (target / "model.gguf").write_bytes(b"tampered bytes")
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        str(target),
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, output.getvalue())
            data = json.loads(output.getvalue())
            backups = next(check for check in data["checks"] if check["name"] == "backups")
            self.assertFalse(backups["ok"])
            self.assertIn("SHA-256 mismatch", backups["detail"])

    def test_protect_check_rejects_an_unsafe_manifest_path(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                backed_up = main(
                    ["protect", "--backup", str(target), "--workspace", tmp, "--no-color"]
                )
            self.assertEqual(backed_up, 0)
            (target / BACKUP_MANIFEST).write_text(
                f"{'a' * 64}  ../outside.txt\n", encoding="utf-8"
            )
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        str(target),
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, output.getvalue())
            data = json.loads(output.getvalue())
            backups = next(check for check in data["checks"] if check["name"] == "backups")
            self.assertFalse(backups["ok"])
            self.assertIn("unsafe path", backups["detail"])

    def test_protect_backup_can_sign_and_verify_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            key = root / "backup-signing.key"
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    [
                        "protect",
                        "--backup",
                        str(target),
                        "--sign-key",
                        str(key),
                        "--workspace",
                        tmp,
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            data = json.loads(output.getvalue())
            self.assertTrue(data["signed"])
            self.assertEqual(data["signature_algorithm"], "hmac-sha256")
            self.assertTrue(data["signing_key_created"])
            signature = target / BACKUP_SIGNATURE
            self.assertEqual(data["signature"], str(signature.resolve()))
            self.assertTrue(signature.is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        backup,
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, output.getvalue())
            backups = next(
                check
                for check in json.loads(output.getvalue())["checks"]
                if check["name"] == "backups"
            )
            self.assertIn("signed", backups["detail"])

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        backup,
                        "--sign-key",
                        str(key),
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            backups = next(
                check
                for check in json.loads(output.getvalue())["checks"]
                if check["name"] == "backups"
            )
            self.assertIn("HMAC-SHA256 manifest signature verified", backups["detail"])

            wrong_key = root / "wrong-signing.key"
            wrong_key.write_text("00" * 32 + "\n", encoding="utf-8")
            if os.name == "posix":
                wrong_key.chmod(0o600)
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        backup,
                        "--sign-key",
                        str(wrong_key),
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, output.getvalue())
            backups = next(
                check
                for check in json.loads(output.getvalue())["checks"]
                if check["name"] == "backups"
            )
            self.assertIn("signature mismatch", backups["detail"])

    def test_protect_check_rejects_a_rehashed_manifest_without_a_valid_signature(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            model = root / "models" / "model.gguf"
            model.write_bytes(b"GGUF")
            key = root / "backup-signing.key"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                signed = main(
                    [
                        "protect",
                        "--backup",
                        str(target),
                        "--sign-key",
                        str(key),
                        "--workspace",
                        tmp,
                        "--no-color",
                    ]
                )
            self.assertEqual(signed, 0)
            tampered = b"tampered model bytes"
            (target / "model.gguf").write_bytes(tampered)
            manifest = target / BACKUP_MANIFEST
            old_hash = hashlib.sha256(b"GGUF").hexdigest()
            new_hash = hashlib.sha256(tampered).hexdigest()
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(old_hash, new_hash),
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        backup,
                        "--sign-key",
                        str(key),
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, output.getvalue())
            backups = next(
                check
                for check in json.loads(output.getvalue())["checks"]
                if check["name"] == "backups"
            )
            self.assertIn("signature mismatch", backups["detail"])

    def test_protect_check_refuses_a_signed_backup_without_its_signature(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            key = root / "backup-signing.key"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                signed = main(
                    [
                        "protect",
                        "--backup",
                        str(target),
                        "--sign-key",
                        str(key),
                        "--workspace",
                        tmp,
                        "--no-color",
                    ]
                )
            self.assertEqual(signed, 0)
            (target / BACKUP_SIGNATURE).unlink()
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "protect",
                        "--check",
                        "--workspace",
                        tmp,
                        "--backup",
                        backup,
                        "--sign-key",
                        str(key),
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, output.getvalue())
            backups = next(
                check
                for check in json.loads(output.getvalue())["checks"]
                if check["name"] == "backups"
            )
            self.assertIn("no manifest signature", backups["detail"])

    def test_protect_refuses_to_store_the_signing_key_inside_the_backup(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as backup:
            root = Path(tmp)
            target = Path(backup)
            (root / ".offgrid").mkdir()
            (root / ".offgrid" / "continuity.md").write_text(
                continuity_kit(Config(), root), encoding="utf-8"
            )
            (root / "models").mkdir()
            (root / "models" / "model.gguf").write_bytes(b"GGUF")
            key = target / "backup-signing.key"
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    [
                        "protect",
                        "--backup",
                        str(target),
                        "--sign-key",
                        str(key),
                        "--workspace",
                        tmp,
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("outside the backup directory", output.getvalue())
            self.assertFalse((target / "continuity.md").exists())
            self.assertFalse(key.exists())

    def test_protect_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["protect", "--workspace", tmp, "--json"])
            self.assertEqual(code, 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["principal"], "Zach")
            self.assertTrue(any("loopback only" in line for line in data["posture"]))
            self.assertTrue(any("will not help with violence" in line for line in data["limits"]))

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_kit_is_private_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["protect", "--kit", "--workspace", tmp, "--no-color"])
            self.assertEqual(code, 0)
            path = Path(tmp) / ".offgrid" / "continuity.md"
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with redirect_stdout(StringIO()):
                again = main(["protect", "--kit", "--workspace", tmp, "--no-color"])
            self.assertEqual(again, 1)
            with redirect_stdout(StringIO()):
                forced = main(["protect", "--kit", "--force", "--workspace", tmp, "--no-color"])
            self.assertEqual(forced, 0)


if __name__ == "__main__":
    unittest.main()
