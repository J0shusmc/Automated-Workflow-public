"""Run the existing LTX --work-order worker with memory-efficient Volta attention."""
import os
from pathlib import Path
import runpy
import sys


WORKER = Path(os.environ.get('LTX_ROOT') or Path(__file__).resolve().parents[1] / 'external' / 'LTX-2') / 'video_generation.py'


def enable_volta_attention():
    import torch
    from torch.nn.attention import SDPBackend
    from ltx_core.model.transformer.attention import PytorchAttention

    original = PytorchAttention.__call__

    def attention(self, q, k, v, heads, mask=None):
        if q.is_cuda and q.dtype == torch.bfloat16 and torch.cuda.get_device_capability(q.device)[0] < 8:
            # Volta's efficient SDPA supports FP32/FP16, but not BF16.
            # BF16 dispatch otherwise falls back to an enormous N-by-N matrix.
            # FP32 preserves the BF16 input range and avoids FP16 overflow.
            efficient = PytorchAttention(priority=[SDPBackend.EFFICIENT_ATTENTION])
            if mask is not None and mask.is_floating_point():
                mask = mask.float()
            result = original(efficient, q.float(), k.float(), v.float(), heads, mask)
            return result.to(q.dtype)
        return original(self, q, k, v, heads, mask)

    PytorchAttention.__call__ = attention
    print('LTX attention: memory-efficient FP32 attention for BF16 on Volta; no quadratic-memory fallback.', flush=True)


if __name__ == '__main__':
    enable_volta_attention()
    sys.argv[0] = str(WORKER)
    runpy.run_path(str(WORKER), run_name='__main__')
