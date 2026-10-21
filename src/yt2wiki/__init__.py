"""Local, offline-first pipeline that compiles a video into an illustrated wiki article."""

from __future__ import annotations

import os as _os

__version__ = "0.1.0"

# These must be set before tqdm / huggingface_hub are imported: both read their
# environment at import time.  A long-running service must not write progress
# bars, and a closed stderr pipe otherwise turns a status printer into a
# BrokenPipeError in the middle of a stage.
_os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
_os.environ.setdefault("TQDM_DISABLE", "1")
_os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
_os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
_os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
_os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
