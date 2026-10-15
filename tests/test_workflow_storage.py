import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automated_workflow import server


class WorkflowStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        self.workflow_dir = self.project_root / "data" / "workflows"
        self.workflow_dir.mkdir(parents=True)
        self.patches = [
            patch.object(server, "PROJECT_ROOT", self.project_root),
            patch.object(server, "DATA_DIR", self.project_root / "data"),
            patch.object(server, "WORKFLOW_DIR", self.workflow_dir),
            patch.object(server, "sync_workflow_service_state"),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temporary_directory.cleanup()

    def write_workflow(self, path: Path, *, updated_at: int, enabled: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "id": "contact-workflow",
                    "name": "Initial Contact",
                    "updatedAt": updated_at,
                    "nodes": [],
                    "edges": [],
                    "recurring": {
                        "enabled": enabled,
                        "frequencySeconds": 60,
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_newer_legacy_stop_beats_stale_canonical_copy(self):
        canonical = self.workflow_dir / "initial-contact-contact" / "workflow.json"
        legacy = self.workflow_dir / "contact-workflow.json"
        self.write_workflow(canonical, updated_at=100, enabled=True)
        self.write_workflow(legacy, updated_at=200, enabled=False)

        workflow = server.read_workflow("contact-workflow")

        self.assertFalse(workflow["recurring"]["enabled"])
        self.assertEqual(server.workflow_path("contact-workflow"), legacy)

    def test_autosave_cannot_reenable_stopped_workflow_and_duplicate_is_archived(self):
        canonical = self.workflow_dir / "initial-contact-contact" / "workflow.json"
        legacy = self.workflow_dir / "contact-workflow.json"
        self.write_workflow(canonical, updated_at=200, enabled=False)
        self.write_workflow(legacy, updated_at=100, enabled=True)
        stale_dashboard_payload = json.loads(legacy.read_text(encoding="utf-8"))
        stale_dashboard_payload["name"] = "Renamed in stale dashboard"

        saved = server.save_workflow(stale_dashboard_payload)

        self.assertFalse(saved["recurring"]["enabled"])
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list((self.workflow_dir / ".legacy-backups").glob("*.json"))), 1)
        self.assertFalse(server.read_workflow("contact-workflow")["recurring"]["enabled"])

    def test_dedicated_recurring_update_can_enable_workflow(self):
        canonical = self.workflow_dir / "initial-contact-contact" / "workflow.json"
        self.write_workflow(canonical, updated_at=200, enabled=False)
        workflow = json.loads(canonical.read_text(encoding="utf-8"))
        workflow["recurring"]["enabled"] = True

        saved = server.save_workflow(workflow, recurring_authoritative=True)

        self.assertTrue(saved["recurring"]["enabled"])
        self.assertIsNotNone(saved["recurring"]["nextRunAt"])


if __name__ == "__main__":
    unittest.main()
