"""Render a Shorts --work-order with the full LTX-2.3 model and HQ pipeline."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LTX_ROOT = Path(os.environ.get('LTX_ROOT') or Path(__file__).resolve().parents[1] / 'external' / 'LTX-2')
MODEL_DIR = ROOT / 'models' / 'LTX-2.3-HQ'
CHECKPOINT = MODEL_DIR / 'ltx-2.3-22b-dev.safetensors'
REFINEMENT = MODEL_DIR / 'ltx-2.3-22b-distilled-lora-384-1.1.safetensors'
UPSCALER = LTX_ROOT / 'models/ltx-2.3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors'
GEMMA = LTX_ROOT / 'models/gemma-3-12b'
WIDTH, HEIGHT, FPS = 768, 1344, 24


def load_work_order(path):
    # Reuse the existing worker's validated order format and path resolution.
    spec = importlib.util.spec_from_file_location('shorts_work_order', LTX_ROOT / 'video_generation.py')
    worker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = worker
    spec.loader.exec_module(worker)
    return worker.load_work_order(path)


def hq_arguments(order):
    return [
        '--checkpoint-path', str(CHECKPOINT),
        '--distilled-lora', str(REFINEMENT), '1.0',
        '--spatial-upsampler-path', str(UPSCALER),
        '--gemma-root', str(GEMMA),
        '--prompt', order.prompt,
        '--negative-prompt', 'subtitles, captions, text overlays, watermarks, distorted anatomy, '
        'extra limbs, unnatural body movement, flickering, blurry faces',
        '--seed', str(order.seed), '--width', str(WIDTH), '--height', str(HEIGHT),
        '--num-frames', str(order.frames), '--frame-rate', str(FPS),
        '--image', str(order.reference_image), '0', '0.9',
        '--output-path', str(order.output), '--offload', 'cpu', '--max-batch-size', '1',
        # Keep the official LTX_2_3_HQ_PARAMS schedule and guidance settings.
        # No --quantization: use the full BF16 weights rather than FP8 casting.
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-order', required=True, type=Path)
    args = parser.parse_args()
    order = load_work_order(args.work_order)
    for path in (CHECKPOINT, REFINEMENT, UPSCALER):
        if not path.is_file():
            raise SystemExit(f'Missing HQ model file: {path}')
    if not GEMMA.is_dir():
        raise SystemExit(f'Missing text encoder: {GEMMA}')
    order.output.parent.mkdir(parents=True, exist_ok=True)
    from video_generation_memory_safe import enable_volta_attention
    from ltx_pipelines.utils.constants import LTX_2_3_HQ_PARAMS
    enable_volta_attention()
    print(f'LTX-2.3 FULL MODEL HQ | BF16 weights | {WIDTH}x{HEIGHT} | '
          f'{order.frames} frames at {FPS} fps | CPU offload | sequential guidance', flush=True)
    started = time.monotonic()
    command_args = hq_arguments(order)
    sys.argv = ['ltx_pipelines.ti2vid_two_stages_hq', *command_args]
    runpy.run_module('ltx_pipelines.ti2vid_two_stages_hq', run_name='__main__')
    if not order.output.is_file() or order.output.stat().st_size == 0:
        raise RuntimeError('HQ pipeline returned without creating a video.')
    order.output.with_suffix('.render.json').write_text(json.dumps({
        'model': CHECKPOINT.name, 'pipeline': 'TI2VidTwoStagesHQPipeline',
        'precision': 'bf16', 'quantization': None,
        'hq_sampler_steps': LTX_2_3_HQ_PARAMS.num_inference_steps,
        'video_cfg': LTX_2_3_HQ_PARAMS.video_guider_params.cfg_scale,
        'audio_cfg': LTX_2_3_HQ_PARAMS.audio_guider_params.cfg_scale,
        'width': WIDTH, 'height': HEIGHT, 'fps': FPS, 'frames': order.frames,
        'prompt': order.prompt, 'seed': order.seed,
        'elapsed_seconds': round(time.monotonic() - started, 2),
    }, indent=2) + '\n')
    print(f'video is ready: {order.output}', flush=True)


if __name__ == '__main__':
    main()
