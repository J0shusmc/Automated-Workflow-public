import json
from pathlib import Path
import tempfile
import unittest

from automated_workflow.server import work_order_args_for_script


class VideoWorkOrderTests(unittest.TestCase):
    def test_duration_input_and_existing_default(self):
        for extra, expected in [({}, 5), ({"duration_seconds": "18"}, 18)]:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                inputs = {"prompt": "A scene", "reference_image": "/tmp/frame.png", **extra}
                args = work_order_args_for_script("--work-order", inputs, Path(directory), "video")
                order = json.loads(Path(args[1]).read_text())
                self.assertEqual(order["target_duration_seconds"], expected)
                self.assertEqual(order["prompt"], "A scene")

    def test_invalid_duration_does_not_create_work_order(self):
        for value in ["bad", "nan", "inf", 0, -2, True, None]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                inputs = {"prompt": "A scene", "reference_image": "/tmp/frame.png", "duration_seconds": value}
                with self.assertRaisesRegex(ValueError, "Video duration"):
                    work_order_args_for_script("--work-order", inputs, Path(directory), "video")
                self.assertEqual(list(Path(directory).iterdir()), [])
