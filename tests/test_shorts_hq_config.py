import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location(
    'shorts_hq', Path(__file__).resolve().parents[1] / 'scripts/video_generation_hq.py'
)
hq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hq)


class ShortsHQTests(unittest.TestCase):
    def test_full_model_profile_preserves_requested_scene(self):
        order = SimpleNamespace(
            prompt='Grandpa asks, "Are you okay?"', seed=42, frames=433,
            reference_image=Path('/tmp/reference image.png'), output=Path('/tmp/finished clip.mp4'),
        )
        args = hq.hq_arguments(order)
        self.assertEqual(args[args.index('--prompt') + 1], order.prompt)
        self.assertEqual(args[args.index('--num-frames') + 1], '433')
        self.assertEqual(args[args.index('--output-path') + 1], str(order.output))
        self.assertTrue(args[args.index('--checkpoint-path') + 1].endswith('ltx-2.3-22b-dev.safetensors'))
        self.assertNotIn('--distilled-checkpoint-path', args)
        self.assertNotIn('--quantization', args)
        self.assertEqual(args[args.index('--offload') + 1], 'cpu')
        self.assertEqual(args[args.index('--max-batch-size') + 1], '1')

    def test_resolution_meets_two_stage_pipeline_constraints(self):
        self.assertEqual(hq.WIDTH % 64, 0)
        self.assertEqual(hq.HEIGHT % 64, 0)
        self.assertGreater(hq.WIDTH * hq.HEIGHT, 576 * 1024)
