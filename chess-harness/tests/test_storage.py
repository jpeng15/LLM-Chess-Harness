import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chess_harness.storage import atomic_write


class AtomicWriteTests(unittest.TestCase):
    def test_transient_windows_lock_preserves_previous_file_until_success(self):
        for code in (5, 32, 33):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "progress.json"
                path.write_text("old")
                replace = Path.replace
                calls = []
                def locked(source, target):
                    self.assertEqual(target.read_text(), "old")
                    self.assertEqual(source.read_text(encoding="utf-8"), "new ♞")
                    calls.append(source)
                    if len(calls) < 3:
                        error = PermissionError("reader holds file")
                        error.winerror = code
                        raise error
                    return replace(source, target)
                with patch.object(Path, "replace", locked), patch("chess_harness.storage.time.sleep") as sleep:
                    atomic_write(path, "new ♞")
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(path.read_text(encoding="utf-8"), "new ♞")
                self.assertEqual(list(Path(root).iterdir()), [path])

    def test_permanent_failure_is_bounded_and_does_not_destroy_previous_file(self):
        error = PermissionError("persistent lock")
        error.winerror = 5
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "manifest.json"
            path.write_text("old")
            with patch.object(Path, "replace", side_effect=error) as replace, patch("chess_harness.storage.time.sleep") as sleep:
                with self.assertRaises(PermissionError):
                    atomic_write(path, "new")
            self.assertEqual(replace.call_count, 6)
            self.assertAlmostEqual(sum(c.args[0] for c in sleep.call_args_list), .775)
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_unrelated_io_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "new.json"
            with patch.object(Path, "replace", side_effect=OSError(errno.ENOSPC, "disk full")), patch("chess_harness.storage.time.sleep") as sleep:
                with self.assertRaises(OSError):
                    atomic_write(path, "new")
                sleep.assert_not_called()
            self.assertEqual(list(Path(root).iterdir()), [])
