"""Model backend protocol.

Backends are deliberately thin: they own load/unload and a single generation
entry point.  Everything about *what* to ask lives in the prompt layer so that
ASR/VLM/LLM vendors can be swapped without touching the pipeline.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from ..config import ModelSpec
from ..gpu import BackendReport, peak_vram_gb


@runtime_checkable
class Backend(Protocol):
    report: BackendReport

    def load(self) -> BackendReport: ...

    def unload(self) -> None: ...

    def close(self) -> None: ...


class BaseBackend:
    def __init__(self, spec: ModelSpec, variant: dict[str, Any], device: str, index: int | None):
        self.spec = spec
        self.variant = dict(variant)
        self.device = device
        self.index = index
        self.model: Any = None
        self._load_seconds = 0.0
        self.report = BackendReport(
            model_key=spec.key,
            repo=spec.repo,
            revision=spec.revision,
            engine=spec.engine,
            variant=self.variant,
            device=device,
        )

    # ------------------------------------------------------------------
    def _finish_load(self, started: float, extra: dict[str, Any] | None = None) -> BackendReport:
        self._load_seconds = time.time() - started
        self.report.load_seconds = self._load_seconds
        self.report.peak_vram_gb = peak_vram_gb()
        for k, v in (extra or {}).items():
            setattr(self.report, k, v)
        return self.report

    # ------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self.model is not None

    def unload(self) -> None:
        if self.model is not None:
            del self.model
            self.model = None

    def close(self) -> None:
        self.unload()
