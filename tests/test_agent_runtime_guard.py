"""A write-only workspace must not be able to replace trusted runtime code."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentarm.workspace import AgentPolicy, Workspace, digest


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "installed"
        self.package = self.install / "agentarm"
        self.package.mkdir(parents=True)
        self.worker = self.package / "pdf_worker.py"
        self.original = b"# trusted helper\n"
        self.worker.write_bytes(self.original)
        module = patch(
            "agentarm.workspace.__file__", str(self.package / "workspace.py")
        )
        module.start()
        self.addCleanup(module.stop)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))

    def test_write_only_cannot_replace_trusted_helper(self):
        with self.assertRaisesRegex(PermissionError, "runtime"):
            self.workspace.write_file(
                "installed/agentarm/pdf_worker.py",
                "# replacement",
                digest(self.original),
            )
        self.assertEqual(self.worker.read_bytes(), self.original)

    def test_new_shadow_modules_and_packages_are_blocked(self):
        with self.assertRaisesRegex(PermissionError, "runtime"):
            self.workspace.write_file("installed/pypdf.py", "# shadow module", None)
        with self.assertRaisesRegex(PermissionError, "runtime"):
            self.workspace.create_directory("installed/openpyxl")
        self.assertFalse((self.install / "pypdf.py").exists())
        self.assertFalse((self.install / "openpyxl").exists())

    def test_delete_move_and_copy_destination_are_blocked(self):
        plain = self.workspace.write_file("plain.py", "# project code", None)
        operations = [
            lambda: self.workspace.delete_file(
                "installed/agentarm/pdf_worker.py", digest(self.original)
            ),
            lambda: self.workspace.transfer_file(
                "installed/agentarm/pdf_worker.py",
                "moved.py",
                digest(self.original),
                True,
            ),
            lambda: self.workspace.transfer_file(
                "plain.py", "installed/new.py", plain["sha256"]
            ),
            lambda: self.workspace.transfer_file(
                "installed", "relocated", "unused", True
            ),
        ]
        for operation in operations:
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(PermissionError, "runtime"),
            ):
                operation()
        self.assertEqual(self.worker.read_bytes(), self.original)
        self.assertFalse((self.root / "moved.py").exists())
        self.assertFalse((self.root / "relocated").exists())
        self.assertEqual(len(self.workspace.undo), 1)

    def test_ancestor_rename_is_rejected_before_scanning_or_mutating(self):
        container = self.root / "container"
        container.mkdir()
        with patch(
            "agentarm.workspace.__file__", str(container / "app/agentarm/workspace.py")
        ):
            workspace = Workspace(AgentPolicy(self.root, allow_write=True))
        with self.assertRaisesRegex(PermissionError, "runtime"):
            workspace.transfer_file("container", "renamed", "unused", True)
        self.assertTrue(container.is_dir())

    def test_runtime_read_preview_and_copy_out_remain_available(self):
        result = self.workspace.read_file("installed/agentarm/pdf_worker.py")
        self.assertEqual(result["content"], self.original.decode())
        preview = self.workspace.write_file(
            "installed/agentarm/pdf_worker.py", "# preview", result["sha256"], True
        )
        self.assertTrue(preview["preview"])
        copied = self.workspace.transfer_file(
            "installed/agentarm/pdf_worker.py", "copy.py", result["sha256"]
        )
        self.assertEqual((self.root / "copy.py").read_bytes(), self.original)
        self.workspace.undo_change(copied["undo_id"])
        self.assertFalse((self.root / "copy.py").exists())

    def test_exec_flag_does_not_expand_file_tool_runtime_authority(self):
        workspace = Workspace(AgentPolicy(self.root, allow_write=True, allow_exec=True))
        with self.assertRaisesRegex(PermissionError, "runtime"):
            workspace.write_file(
                "installed/agentarm/pdf_worker.py", "# changed", digest(self.original)
            )

    def test_interpreter_environment_is_protected(self):
        for prefix in ("prefix", "base_prefix"):
            with (
                self.subTest(prefix=prefix),
                patch("agentarm.workspace.sys." + prefix, str(self.root / "python")),
            ):
                workspace = Workspace(AgentPolicy(self.root, allow_write=True))
                with self.assertRaisesRegex(PermissionError, "runtime"):
                    workspace.create_directory("python/Lib")

    def test_runtime_undo_is_rechecked_and_retained_on_denial(self):
        key = self.workspace._remember(
            "installed/agentarm/pdf_worker.py", b"# older", digest(self.original)
        )
        with self.assertRaisesRegex(PermissionError, "runtime"):
            self.workspace.undo_change(key)
        self.assertIn(key, self.workspace.undo)
        self.assertEqual(self.worker.read_bytes(), self.original)

    def test_other_project_can_edit_files_with_identical_runtime_names(self):
        self.workspace.create_directory("project/agentarm")
        written = self.workspace.write_file(
            "project/agentarm/pdf_worker.py", "# data", None
        )
        self.workspace.delete_file("project/agentarm/pdf_worker.py", written["sha256"])
        self.assertFalse((self.root / "project/agentarm/pdf_worker.py").exists())


if __name__ == "__main__":
    unittest.main()
