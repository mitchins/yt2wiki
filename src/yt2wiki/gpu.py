"""GPU accounting and explicit model lifecycle.

The production budget is a single 16 GB card.  Nothing here assumes two large
models fit at once: each stage owns the GPU, reports peak usage, and releases
deterministically before the next stage loads.
"""

from __future__ import annotations

import gc
import logging
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

log = logging.getLogger("yt2wiki.gpu")

try:  # torch is optional for CPU-only smoke tests
    import torch

    _HAS_TORCH = True
except Exception:  # pragma: no cover
    torch = None
    _HAS_TORCH = False


@dataclass
class GpuSample:
    free_gb: float
    total_gb: float
    used_gb: float
    index: int
    name: str


def nvidia_smi_gpus() -> list[GpuSample]:
    """Parse nvidia-smi.  Returns [] when no NVIDIA stack is present."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    gpus: list[GpuSample] = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            gpus.append(
                GpuSample(
                    index=int(parts[0]),
                    name=parts[1],
                    total_gb=float(parts[2]) / 1024,
                    used_gb=float(parts[3]) / 1024,
                    free_gb=float(parts[4]) / 1024,
                )
            )
        except ValueError:
            continue
    return gpus


def cuda_available() -> bool:
    if not _HAS_TORCH:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:  # pragma: no cover
        return False


def pick_device(preferred: int | None = None) -> tuple[str, int | None]:
    """Return ('cuda' | 'cpu', device_index).

    Honours an explicit choice; otherwise picks the GPU with the most free
    memory so that development work does not collide with unrelated jobs.

    When CUDA_VISIBLE_DEVICES is set, nvidia-smi indices are mapped to the
    visible ordinal so the chosen device is valid inside the container.
    """
    if not cuda_available():
        return "cpu", None
    if preferred is not None:
        try:
            torch.cuda.set_device(preferred)
        except Exception as exc:  # pragma: no cover
            log.warning("cannot select cuda:%s (%s); falling back to auto", preferred, exc)
        return "cuda", preferred
    gpus = nvidia_smi_gpus()
    if gpus:
        import os as _os
        visible = _os.environ.get("CUDA_VISIBLE_DEVICES")
        if visible:
            try:
                order = [int(x.strip()) for x in visible.split(",") if x.strip() != ""]
                # Map PCI index -> visible ordinal
                def _visible_rank(g):
                    return order.index(g.index) if g.index in order else 999
                # Prefer visible GPUs first, by free memory
                visible_gpus = [g for g in gpus if g.index in order]
                if visible_gpus:
                    best = max(visible_gpus, key=lambda g: g.free_gb)
                    return "cuda", order.index(best.index)
            except (ValueError, IndexError):
                pass
        best = max(gpus, key=lambda g: g.free_gb)
        return "cuda", best.index
    return "cuda", None


def torch_device(index: int | None) -> "torch.device | str":
    if index is None:
        return "cuda" if cuda_available() else "cpu"
    return f"cuda:{index}"


def sync() -> None:
    if cuda_available():
        torch.cuda.synchronize()


def free_cuda_memory() -> None:
    """Release everything this process is holding on the GPU."""
    gc.collect()
    if cuda_available():
        try:
            torch.cuda.synchronize()
        except Exception:  # pragma: no cover
            pass
        try:
            torch.cuda.empty_cache()
        except Exception:  # pragma: no cover
            pass
        try:
            torch.cuda.ipc_collect()
        except Exception:  # pragma: no cover
            pass


def peak_vram_gb() -> float:
    if not cuda_available():
        return 0.0
    try:
        return torch.cuda.max_memory_allocated() / 1024**3
    except Exception:  # pragma: no cover
        return 0.0


def reset_peak() -> None:
    if cuda_available():
        try:
            torch.cuda.reset_peak_memory_stats()
        except Exception:  # pragma: no cover
            pass


def current_vram_gb() -> float:
    if not cuda_available():
        return 0.0
    try:
        return torch.cuda.memory_allocated() / 1024**3
    except Exception:  # pragma: no cover
        return 0.0


@dataclass
class LoadEvent:
    model_key: str
    repo: str
    ok: bool
    device: str
    vram_gb: float
    seconds: float
    error: str = ""
    fallback_of: str = ""


@dataclass
class BackendReport:
    """Everything we know about a loaded model instance, for provenance."""

    model_key: str
    repo: str
    revision: str
    engine: str
    variant: dict = field(default_factory=dict)
    device: str = "cpu"
    load_seconds: float = 0.0
    peak_vram_gb: float = 0.0
    fallback_of: str = ""
    load_error: str = ""
    actual_revision: str = ""

    def to_dict(self) -> dict:
        return {
            "model_key": self.model_key,
            "repo": self.repo,
            "revision": self.actual_revision or self.revision,
            "engine": self.engine,
            "variant": self.variant,
            "device": self.device,
            "load_seconds": round(self.load_seconds, 2),
            "peak_vram_gb": round(self.peak_vram_gb, 3),
            "fallback_of": self.fallback_of,
            "load_error": self.load_error,
        }


class ModelNotAvailable(RuntimeError):
    pass


@contextmanager
def gpu_session(label: str = "") -> Iterator[None]:
    """Scope that guarantees the GPU is handed back even on exceptions."""
    reset_peak()
    try:
        yield
    finally:
        free_cuda_memory()
        log.debug("gpu session %s released; peak %.2f GB", label, peak_vram_gb())
