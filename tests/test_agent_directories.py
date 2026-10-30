import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentarm.directory_tools import inspect_tree, rename_no_replace
from agentarm.workspace import AgentPolicy, Workspace


@unittest.skipUnless(
    os.name == "nt" or sys.platform == "linux",
    "Atomic directory moves support Windows/Linux",
)
class DirectoryMoveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))
        source = self.root / "source"
        (source / "nested/empty").mkdir(parents=True)
        (source / "nested/a.txt").write_bytes("Türkçe\r\nsecond\n".encode("utf-16"))
        (source / "b.bin").write_bytes(bytes(range(16)))

    def snapshot(self):
        return self.workspace.info("source", inspect_tree=True)

    def test_directory_rename_and_single_undo_preserve_nested_and_empty_content(self):
        before = self.snapshot()
        self.assertEqual(before["files"], 2)
        self.assertEqual(before["directories"], 2)
        self.assertEqual(before["hash_kind"], "directory_tree_v1")
        file_bytes = (self.root / "source/nested/a.txt").read_bytes()
        moved = self.workspace.transfer_file(
            "source", "renamed", before["sha256"], move=True
        )
        self.assertFalse((self.root / "source").exists())
        self.assertTrue((self.root / "renamed/nested/empty").is_dir())
        self.assertEqual((self.root / "renamed/nested/a.txt").read_bytes(), file_bytes)
        self.assertEqual(
            self.workspace.info("renamed", True)["sha256"], before["sha256"]
        )
        self.assertEqual(self.workspace.undo_bytes, 0)
        restored = self.workspace.undo_change(moved["undo_id"])
        self.assertTrue(restored["restored"])
        self.assertFalse((self.root / "renamed").exists())
        self.assertEqual(self.snapshot()["sha256"], before["sha256"])
        self.assertFalse(self.workspace.undo)

    def test_changed_tree_and_recreated_source_prevent_undo(self):
        before = self.snapshot()
        moved = self.workspace.transfer_file(
            "source", "renamed", before["sha256"], move=True
        )
        (self.root / "renamed/new.txt").write_text("external")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.workspace.undo_change(moved["undo_id"])
        (self.root / "renamed/new.txt").unlink()
        (self.root / "source").mkdir()
        with self.assertRaises(FileExistsError):
            self.workspace.undo_change(moved["undo_id"])
        self.assertTrue((self.root / "renamed/nested/empty").is_dir())

    def test_preflight_rejects_changed_source_and_invalid_destinations(self):
        before = self.snapshot()
        (self.root / "source/b.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.workspace.transfer_file(
                "source", "renamed", before["sha256"], move=True
            )
        self.assertFalse((self.root / "renamed").exists())
        current = self.snapshot()
        for destination in (
            "source",
            "source/child",
            "missing/renamed",
            "../outside",
            ".git/renamed",
        ):
            with (
                self.subTest(destination=destination),
                self.assertRaises((ValueError, PermissionError)),
            ):
                self.workspace.transfer_file(
                    "source", destination, current["sha256"], move=True
                )
        (self.root / "existing").mkdir()
        with self.assertRaises(FileExistsError):
            self.workspace.transfer_file(
                "source", "existing", current["sha256"], move=True
            )
        self.assertTrue((self.root / "source").is_dir())
        self.assertFalse(self.workspace.undo)

    def test_file_changed_after_its_hash_is_rejected_before_directory_rename(self):
        before = self.snapshot()
        original_scandir = os.scandir

        def concurrent_change(directory):
            if Path(directory).name == "nested":
                (self.root / "source/b.bin").write_bytes(
                    b"external change after this file was scanned"
                )
            return original_scandir(directory)

        with (
            patch("agentarm.directory_tools.os.scandir", side_effect=concurrent_change),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            self.workspace.transfer_file(
                "source", "renamed", before["sha256"], move=True
            )
        self.assertFalse((self.root / "renamed").exists())
        self.assertTrue((self.root / "source").is_dir())
        self.assertFalse(self.workspace.undo)

    def test_protected_descendants_are_not_silently_moved(self):
        before = self.snapshot()
        (self.root / "source/.env").write_text("private fixture")
        with self.assertRaises(PermissionError):
            self.workspace.transfer_file(
                "source", "renamed", before["sha256"], move=True
            )
        self.assertTrue((self.root / "source/.env").is_file())
        self.assertFalse((self.root / "renamed").exists())

    def test_link_descendant_prevents_move(self):
        before = self.snapshot()
        target = self.root / "other"
        target.mkdir()
        link = self.root / "source/link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                self.skipTest("OS disallows both symlink and junction fixture creation")
            self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        with self.assertRaises(PermissionError):
            self.workspace.transfer_file(
                "source", "renamed", before["sha256"], move=True
            )
        self.assertFalse((self.root / "renamed").exists())

    def test_scan_and_aggregate_byte_limits_are_pre_mutation(self):
        small = Workspace(AgentPolicy(self.root, allow_write=True, max_file_bytes=1))
        with self.assertRaisesRegex(ValueError, "byte budget"):
            small.info("source", True)
        small = Workspace(AgentPolicy(self.root, allow_write=True, max_scan_files=1))
        with self.assertRaisesRegex(ValueError, "entry scan budget"):
            small.info("source", True)
        with (
            patch("agentarm.directory_tools.time.monotonic", side_effect=[0, 11]),
            self.assertRaises(TimeoutError),
        ):
            inspect_tree(self.workspace, "source")
        self.assertTrue((self.root / "source").is_dir())

    def test_undo_budgets_mixed_records_and_readonly(self):
        before = self.snapshot()
        readonly = Workspace(AgentPolicy(self.root))
        self.assertEqual(readonly.info("source", True)["sha256"], before["sha256"])
        with self.assertRaises(PermissionError):
            readonly.transfer_file("source", "renamed", before["sha256"], move=True)
        moved = self.workspace.transfer_file(
            "source", "renamed", before["sha256"], move=True
        )
        for key in ("max_file_bytes", "max_scan_files"):
            with self.assertRaisesRegex(ValueError, "existing undo"):
                self.workspace.reduce_config(key, 1)
        added = self.workspace.write_file("new.txt", "new", None)
        self.workspace.undo_change(added["undo_id"])
        self.workspace.undo_change(moved["undo_id"])
        bounded = Workspace(
            AgentPolicy(self.root, allow_write=True, max_undo_records=1)
        )
        moved = bounded.transfer_file("source", "renamed", before["sha256"], move=True)
        bounded.write_file("one.txt", "one", None)
        self.assertNotIn(moved["undo_id"], bounded.undo)
        self.assertEqual(bounded.undo_bytes, 0)

    @unittest.skipUnless(
        os.name == "nt" or sys.platform == "linux", "atomic rename platform"
    )
    def test_target_created_after_preflight_is_never_replaced(self):
        before = self.snapshot()

        def competing_target(source, destination):
            destination.mkdir()
            rename_no_replace(source, destination)

        with (
            patch(
                "agentarm.directory_tools.rename_no_replace",
                side_effect=competing_target,
            ),
            self.assertRaises(FileExistsError),
        ):
            self.workspace.transfer_file(
                "source", "renamed", before["sha256"], move=True
            )
        self.assertTrue((self.root / "source/nested/empty").is_dir())
        self.assertEqual(list((self.root / "renamed").iterdir()), [])
        self.assertFalse(self.workspace.undo)

    def test_metadata_line_counts_permissions_and_binary_detection(self):
        info = self.workspace.info("source/nested/a.txt")
        self.assertEqual(info["line_count"], 2)
        self.assertEqual(info["last_line"], 1)
        self.assertEqual(info["encoding"], "utf-16")
        self.assertIn("mode", info)
        self.assertIn("created_ns", info)
        self.assertNotIn("line_count", self.workspace.info("source/b.bin"))


if __name__ == "__main__":
    unittest.main()
