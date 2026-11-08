import os
import stat
import tempfile
import unittest
from pathlib import Path

from offgrid.memory import MemoryStore
from offgrid.private import (
    append_private_text,
    create_private_file,
    ensure_private_dir,
    write_private_text,
)
from offgrid.session import SessionLog


@unittest.skipUnless(os.name == "posix", "POSIX permissions only")
class PrivatePermissionTests(unittest.TestCase):
    def test_state_directory_and_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".offgrid"
            ensure_private_dir(state)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)

            text = state / "note.txt"
            write_private_text(text, "private\n")
            self.assertEqual(stat.S_IMODE(text.stat().st_mode), 0o600)

            append_private_text(text, "more\n")
            self.assertEqual(stat.S_IMODE(text.stat().st_mode), 0o600)

            empty = state / "empty"
            create_private_file(empty)
            self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o600)

    def test_memory_and_session_state_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = MemoryStore(root / ".offgrid" / "memory.db")
            self.addCleanup(memory.close)
            self.assertEqual(stat.S_IMODE((root / ".offgrid").stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((root / ".offgrid" / "memory.db").stat().st_mode), 0o600
            )
            session = SessionLog(root, ".offgrid")
            session.event("test", {"ok": True})
            self.assertEqual(stat.S_IMODE(session.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(session.path.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
