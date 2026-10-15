from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automated_workflow.server import codex_command


class CodexLauncherTests(unittest.TestCase):
    def test_service_path_can_use_node_beside_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "codex"
            launcher.write_text("#!/usr/bin/env node\n")
            node = Path(directory) / "node"
            node.write_text("#!/bin/sh\n")
            node.chmod(0o755)
            with patch("automated_workflow.server.shutil.which", side_effect=lambda name: str(launcher) if name == "codex" else None):
                self.assertEqual(codex_command(), [str(node), str(launcher)])

    def test_native_launcher_does_not_require_node(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "codex"
            launcher.write_bytes(b"\x7fELF")
            with patch("automated_workflow.server.shutil.which", return_value=str(launcher)):
                self.assertEqual(codex_command(), [str(launcher)])

    def test_missing_node_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "codex"
            launcher.write_text("#!/usr/bin/env node\n")
            with patch("automated_workflow.server.shutil.which", side_effect=lambda name: str(launcher) if name == "codex" else None):
                with self.assertRaisesRegex(FileNotFoundError, "requires Node.js"):
                    codex_command()
