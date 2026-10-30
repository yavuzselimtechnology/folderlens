import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import build_agent_release as release


class AgentReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = Path(directory.name)
        self.project = self.repository / "FolderLens"
        self.project.mkdir()
        self.output = self.repository / "output"
        self.git("init", "-q")
        (self.project / "folderlens.py").write_bytes(b"print('committed')\n")
        (self.project / "CLIENT_README.md").write_text(
            "# Başlangıç\n", encoding="utf-8"
        )
        self.git("add", "--", "FolderLens/folderlens.py", "FolderLens/CLIENT_README.md")
        self.commit()
        self.allowlist = patch.object(
            release, "PUBLIC_FILES", ("folderlens.py", "CLIENT_README.md")
        )
        self.allowlist.start()
        self.addCleanup(self.allowlist.stop)

    def git(self, *args, data=None):
        return subprocess.check_output(
            ["git", "-C", str(self.repository), *args], input=data
        )

    def commit(self):
        self.git(
            "-c",
            "user.name=Release fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

    def test_nested_repository_uses_exact_committed_blobs_and_reproducible_zip(self):
        (self.project / "folderlens.py").write_bytes(b"uncommitted secret\n")
        (self.project / ".folderlens").mkdir()
        (self.project / ".folderlens/runtime.key").write_text("private test value")
        (self.project / ".env").write_text("PRIVATE=fixture")
        first = release.build(self.project, "HEAD", self.output)
        payload = (self.output / "folderlens-agent.zip").read_bytes()
        with zipfile.ZipFile(self.output / "folderlens-agent.zip") as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "FolderLens/folderlens.py",
                    "FolderLens/README.md",
                    "FolderLens/RELEASE.json",
                },
            )
            self.assertEqual(
                archive.read("FolderLens/folderlens.py"), b"print('committed')\n"
            )
            manifest = json.loads(archive.read("FolderLens/RELEASE.json"))
            for name, digest in manifest["files"].items():
                self.assertEqual(
                    hashlib.sha256(archive.read("FolderLens/" + name)).hexdigest(),
                    digest,
                )
        second = release.build(self.project, "HEAD", self.output)
        self.assertEqual(first, second)
        self.assertEqual((self.output / "folderlens-agent.zip").read_bytes(), payload)
        self.assertEqual(first["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(
            (self.output / "folderlens-agent.zip.sha256").read_text().split()[0],
            first["sha256"],
        )
        self.assertEqual(
            json.loads((self.output / "folderlens-agent.json").read_text()), manifest
        )
        self.assertEqual(len(list(self.output.iterdir())), 3)

    def test_symlink_blob_is_rejected_before_artifacts_are_written(self):
        blob = (
            self.git("hash-object", "-w", "--stdin", data=b"../private.key")
            .decode()
            .strip()
        )
        self.git(
            "update-index",
            "--cacheinfo",
            "120000," + blob + ",FolderLens/folderlens.py",
        )
        self.commit()
        with self.assertRaisesRegex(ValueError, "regular non-executable"):
            release.build(self.project, "HEAD", self.output)
        self.assertFalse(self.output.exists())

    def test_missing_required_source_is_rejected(self):
        with (
            patch.object(release, "PUBLIC_FILES", ("missing.py",)),
            self.assertRaisesRegex(ValueError, "missing"),
        ):
            release.build(self.project, "HEAD", self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
