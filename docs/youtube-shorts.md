# YouTube Shorts workflow

The saved workflow is `data/workflows/image-generation-36e36cd8/workflow.json`.
The configuration before the October upgrade is in the adjacent
`before-shorts-upgrade.json.bak`.

The image request describes the still frame; the video request describes motion
in that same scene. Connect the generated image to `reference_image` and the
video request to `prompt`. The scene prompt writer improves composition and
clarity; it does not upscale images.

The active scene now includes Grandpa tipping a cane into Grandma's path,
her slapstick fall and slow recovery, and the requested three-line dialogue.
The opening image requests the cane upright beside his chair. The editable
`Clip duration (seconds)` input supplies `duration_seconds` to the video node;
this scene requests 18 seconds. Unconnected workflows retain the five-second
default. The cane scene rendered successfully after the attention memory fix;
the final file is `data/shorts/grandma-cane-scene-1080x1920.mp4` (18.04 seconds,
24 fps, H.264/yuv420p with AAC audio). A sampled frame shows Grandma on the
floor after the fall. Exact dialogue has not been independently transcribed.

`scripts/export_short.py` converts the video to 1080×1920 H.264, yuv420p,
AAC audio when present, and MP4 fast-start. It preserves the frame rate and the
whole picture, padding when needed. This uses Lanczos resizing, not AI detail
reconstruction. Finished files are stored in `data/shorts/`.

## Active image model

`scripts/z_image_short.py` uses local `models/Z-Image-Turbo` weights,
720×1280 portrait output, nine scheduler steps, guidance 0, and seed 42.
It uses CPU model offloading and VAE tiling. On this machine's compute-capability
7.0 GPU it uses FP32; BF16 is selected on compute capability 8.0 and newer.
Generated frames have JSON sidecars recording settings and runtime.

The image node now uses this script. A 720×1280 test completed in 110 seconds
on the installed GPU and visibly included both requested grandparents. The
previous Juggernaut task configuration remains in the workflow backup.

The isolated `.venv-shorts` environment installs `diffusers==0.36.0` and reads
the existing LTX environment's packages through `ltx_shared_runtime.pth`.
This reuses PyTorch 2.9.1+cu128, Transformers 4.57.6, and Accelerate 1.12.0
without changing the LTX environment. If that environment moves, update the
`.pth` file or install those dependencies directly into `.venv-shorts`.

## Video upgrade

The video task launches `scripts/video_generation_hq.py`: full
`ltx-2.3-22b-dev.safetensors` weights, the official
`TI2VidTwoStagesHQPipeline`, and the 384/1.1 refinement adapter. This is the
permanent dashboard profile; there is no automatic distilled-model fallback.
It generates 768×1344 at 24 fps and exports through the existing 1080×1920 node.
The official 15-step Res2S HQ schedule uses video CFG 3, audio CFG 7, and
refinement-adapter strengths 0.25/0.5 across the two stages. Weights remain
BF16 with no FP8 quantization. CPU offloading and sequential guidance
(`max_batch_size=1`) reduce GPU memory requirements at the cost of runtime.
HQ weights are stored in `models/LTX-2.3-HQ`; the text encoder and spatial
upscaler remain in the existing LTX installation. Each completed render writes
a `.render.json` settings sidecar. The duration input still controls clip length.

The HQ worker imports the compatibility fix from
`scripts/video_generation_memory_safe.py`. On Volta only, it converts BF16 attention
inputs to FP32 and explicitly selects PyTorch's memory-efficient attention
kernel, then casts the output back to BF16. Other devices/dtypes retain the
original dispatcher. This prevents the BF16 math fallback from materializing
a quadratic attention matrix: the failed 18-second run requested 119.64 GiB.
Small GPU tests verified numerical agreement with the original calculation
and supported masked attention. The model weights and other computations are
unchanged.

The browser now checks node results before describing a run as successful.
The recovery render's persistent log is `data/shorts/cane-scene-recovery.log`.

The previous cane-scene output used LTX-2.3 Distilled 1.1. LTX-2.5 has gated weights and the
existing Hugging Face account currently receives `GatedRepoError` for them.
Access must be granted at https://huggingface.co/Lightricks/LTX-2.5 before an
upgrade can be tested. LTX-2.5 also requires updated pipeline code and separate
transformer, Gemma 4 encoder, VAE, and upscaler files; changing only the current
checkpoint filename is insufficient.

## Validation

- Saved graph has the image and motion prompt on the correct ports; the video
  node precedes export, which precedes the final display.
- Z-Image produced a valid portrait frame with both grandparents.
- LTX-2.3 successfully rendered that frame and the matching motion prompt into
  a 121-frame clip. A midpoint frame shows Grandma entering the kitchen while
  Grandpa remains seated.
- The final test export is `data/shorts/grandparents-ltx23-test-1080x1920.mp4`.
  FFprobe confirms 1080×1920 H.264/yuv420p video at 24 fps with AAC audio.
- The separate prompt-writer agent was not re-run in this render test; its
  updated instructions are saved in the workflow.

## Full-model HQ validation

The full-model HQ profile completed a 49-frame, 2.04-second render at
768×1344/24 fps with AAC audio on the installed GPU. Model runtime was
1,424 seconds (about 24 minutes), including loading and decoding. The test
uses subtle motion, not the full fall/dialogue scene. The existing 18-second
cane-scene file remains the earlier distilled render; regenerate it from the
dashboard to use HQ. Longer HQ clips have not yet been validated.

The test export is `data/shorts/hq-pipeline-validation-1080x1920.mp4`.
The saved pre-HQ configuration is `before-full-model-hq.json.bak` beside the
workflow file. Refresh the dashboard before running so an open browser does
not save its previous configuration over the update.
