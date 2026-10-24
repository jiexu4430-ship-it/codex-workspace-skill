"""Install only into explicit, isolated test destinations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent


@unittest.skipUnless(os.name == "nt", "The installer targets Windows")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="install_case_", dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.base = self.fixture / "base"
        self.base.mkdir()
        self.config = self.fixture / "local.json"
        self.config.write_text(json.dumps({"schema_version": 1, "allowed_roots": [str(self.base)]}), encoding="utf-8")
        self.destination = self.fixture / "installed" / "workspace"

    def install(self, expected_success=True):
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(REPOSITORY / "install.py"), "--config", str(self.config), "--destination", str(self.destination)],
            cwd=self.fixture, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode == 0, expected_success, result.stdout + result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["ok"], expected_success, response)
        return response

    def snapshot(self):
        return {str(path.relative_to(self.destination)): path.read_bytes() for path in self.destination.rglob("*") if path.is_file()}

    def test_install_and_use_deployed_helper(self):
        response = self.install()
        self.assertEqual(response["status"], "installed")
        self.assertEqual(response["verified_files"], 5)
        self.assertEqual(len(self.snapshot()), 5)
        self.assertEqual(json.loads((self.destination / "config.json").read_text(encoding="utf-8"))["allowed_roots"], [str(self.base)])
        helper = self.destination / "scripts" / "workspace.py"
        thread = str(uuid.uuid4())
        for command, options in (("init", ["--title", "研究报告", "--confirm-new"]), ("rename", ["--title", "研究报告终稿"])):
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(helper), command, "--base", str(self.base), "--thread-id", thread, *options],
                cwd=self.base, capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            response = json.loads(result.stdout)
            self.assertTrue(response["ok"])
        self.assertEqual(Path(response["root"]).name, "研究报告终稿")
        self.assertFalse((self.base / "研究报告").exists())

    def test_repeated_identical_install_preserves_files(self):
        self.install()
        before = self.snapshot()
        self.assertEqual(self.install()["status"], "already_installed")
        self.assertEqual(self.snapshot(), before)

    def test_different_existing_install_is_not_overwritten(self):
        self.install()
        (self.destination / "SKILL.md").write_text("user customization", encoding="utf-8")
        before = self.snapshot()
        self.install(expected_success=False)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_or_missing_configuration_creates_nothing(self):
        self.config.write_text(json.dumps({"schema_version": 1, "allowed_roots": [str(self.fixture / "not-created")]}), encoding="utf-8")
        self.install(expected_success=False)
        self.assertFalse(self.destination.parent.exists())
        self.config.unlink()
        response = self.install(expected_success=False)
        self.assertEqual(response["status"], "configuration_required")
        self.assertFalse(self.destination.parent.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
