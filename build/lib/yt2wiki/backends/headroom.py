"""GPU headroom awareness for the model loader.

A model is only loaded when the requested card actually has room for the
footprint the registry declares.  When it does not, the loader either moves to
a card that does, or retries with a per-process cap that forces CPU offload -
and records whichever it did in `model_loads`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..config import Settings
from ..gpu import GpuSample, cuda_available, nvidia_smi_gpus

log = logging.getLogger("yt2wiki.headroom")

# Fraction of the card we allow one model to claim.  Leaving headroom matters:
# activations and KV cache are not in the declared footprint.
UTILISATION_TARGET = 0.88


@dataclass(frozen=True)
class Headroom:
    ok: bool
    device_index: int | None
    free_gb: float
    total_gb: float
    required_gb: float
    capped_gb: float
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "device_index": self.device_index,
            "free_gb": round(self.free_gb, 2),
            "total_gb": round(self.total_gb, 2),
            "required_gb": round(self.required_gb, 2),
            "capped_gb": round(self.capped_gb, 2),
            "reason": self.reason,
        }


def _usable(gpu: GpuSample) -> float:
    return max(0.0, gpu.total_gb * UTILISATION_TARGET - gpu.used_gb)


def plan(
    settings: Settings,
    role: str,
    *,
    preferred: int | None = None,
    exclude: list[int] | None = None,
) -> Headroom:
    """Decide where a model for `role` should go and how much of it to claim."""
    if not cuda_available():
        return Headroom(True, None, 0.0, 0.0, 0.0, 0.0, "cpu")
    required = float(settings.model(role).vram_gb.get(settings.profile, 0.0) or 0.0)
    gpus = nvidia_smi_gpus()
    if exclude:
        gpus = [g for g in gpus if g.index not in set(exclude)]
    if not gpus:
        return Headroom(True, preferred, 0.0, 0.0, required, 0.0, "nvidia-smi unavailable")

    if preferred is not None:
        chosen = next((g for g in gpus if g.index == preferred), None)
        if chosen is None:
            return Headroom(True, preferred, 0.0, 0.0, required, 0.0,
                            f"requested device {preferred} not visible")
        if required <= 0 or _usable(chosen) >= required:
            return Headroom(True, preferred, _usable(chosen), chosen.total_gb, required,
                            min(required, _usable(chosen)), "ok")
        # the requested card is busy: fall back to the emptiest one that fits
        fallback = _best_fit(gpus, required, exclude=preferred)
        if fallback is not None:
            return Headroom(True, fallback.index, _usable(fallback), fallback.total_gb,
                            required, min(required, _usable(fallback)),
                            f"requested device {preferred} has {_usable(chosen):.1f} GB free; "
                            f"moved to device {fallback.index}")
        return Headroom(
            True, preferred, _usable(chosen), chosen.total_gb, required,
            max(1.0, _usable(chosen)),
            f"no other card fits {required:.1f} GB; capping to {_usable(chosen):.1f} GB "
            "and offloading the rest to CPU",
        )

    best = _best_fit(gpus, required)
    if best is not None:
        return Headroom(True, best.index, _usable(best), best.total_gb, required,
                        min(required, _usable(best)), "ok")
    emptiest = max(gpus, key=_usable)
    return Headroom(
        True, emptiest.index, _usable(emptiest), emptiest.total_gb, required,
        max(1.0, _usable(emptiest)),
        f"card with the most headroom has only {_usable(emptiest):.1f} GB free for a "
        f"{required:.1f} GB model; capping and offloading to CPU",
    )


def _best_fit(gpus: list[GpuSample], required: float, *, exclude: int | None = None) -> GpuSample | None:
    candidates = [g for g in gpus if g.index != exclude] if exclude is not None else gpus
    if not candidates:
        return None
    if required <= 0:
        return max(candidates, key=_usable)
    fitting = [g for g in candidates if _usable(g) >= required]
    if fitting:
        return min(fitting, key=_usable)
    return None


def vram_budget_bytes(capped_gb: float) -> int:
    """Per-process memory cap for accelerate device_map loading."""
    return int(max(0.25, capped_gb) * 1024 ** 3)
