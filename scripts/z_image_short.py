"""Generate a portrait reference frame with local Z-Image-Turbo weights."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "Z-Image-Turbo"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0 or args.width % 16 or args.height % 16:
        parser.error("Image dimensions must be positive multiples of 16.")

    import torch
    from diffusers import ZImagePipeline

    if not torch.cuda.is_available():
        raise RuntimeError("Z-Image requires the NVIDIA GPU to be available.")
    if not (MODEL / "model_index.json").is_file():
        raise RuntimeError(f"Z-Image weights have not been installed at {MODEL}.")
    # Volta (the installed GPU) needs FP32 for this model's large activation
    # range; BF16 is used only on hardware with native support.
    dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float32
    print(f"Loading Z-Image-Turbo using {dtype}; CPU offloading enabled.", flush=True)
    started = time.monotonic()
    pipe = ZImagePipeline.from_pretrained(str(MODEL), torch_dtype=dtype, local_files_only=True)
    pipe.enable_model_cpu_offload()
    pipe.vae.enable_tiling()
    image = pipe(
        prompt=args.prompt, width=args.width, height=args.height,
        num_inference_steps=9, guidance_scale=0.0,
        generator=torch.Generator("cpu").manual_seed(args.seed),
    ).images[0]
    folder = ROOT / "data" / "shorts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("z-image-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".png")
    image.save(path)
    path.with_suffix(".json").write_text(json.dumps({
        "model": "Tongyi-MAI/Z-Image-Turbo", "prompt": args.prompt,
        "width": args.width, "height": args.height, "seed": args.seed,
        "dtype": str(dtype), "elapsed_seconds": round(time.monotonic() - started, 2),
    }, indent=2) + "\n")
    print(f"Saved {path}", flush=True)
    print("@@WORKFLOW_RESULT@@" + json.dumps({"image": {"type": "image", "path": str(path)}}), flush=True)


if __name__ == "__main__":
    main()
