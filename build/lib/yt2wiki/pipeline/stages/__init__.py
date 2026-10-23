"""Pipeline stage implementations.

Each module exposes a single ``run(ctx)`` function; the runner discovers them
through :func:`yt2wiki.pipeline.load_stage_functions`.
"""

from . import acquisition, compose, evidence, frame_candidates, inspect
from . import procedures, publish, transcribe, verify, visual_interpret

STAGE_MODULES = {
    "acquisition": acquisition,
    "inspect": inspect,
    "transcribe": transcribe,
    "frame_candidates": frame_candidates,
    "visual_interpret": visual_interpret,
    "evidence": evidence,
    "procedures": procedures,
    "compose": compose,
    "verify": verify,
    "publish": publish,
}

__all__ = [
    "acquisition",
    "compose",
    "evidence",
    "frame_candidates",
    "inspect",
    "procedures",
    "publish",
    "transcribe",
    "verify",
    "visual_interpret",
    "STAGE_MODULES",
]
