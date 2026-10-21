"""Vision backend (Qwen3-VL and other `transformers` VLMs).

Frames are decoded one at a time by the caller; the backend deliberately takes a
list of *already decoded* PIL images so that no stage ever holds a whole video
in memory.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from ..config import ModelSpec
from ..gpu import BackendReport, free_cuda_memory as free_cuda
from .base import BaseBackend

log = logging.getLogger("yt2wiki.vlm")

# Stand-in used while building the prompt; the processor replaces it with real pixels.
_PLACEHOLDER = "placeholder.png"

try:  # torch raises a class, but importing it at module scope is not possible
    import torch as _torch

    torch_OutOfMemory = _torch.OutOfMemoryError
except Exception:  # pragma: no cover - torch optional at import time
    torch_OutOfMemory = RuntimeError


class TransformersVLM(BaseBackend):
    def __init__(
        self,
        spec: ModelSpec,
        variant: dict[str, Any],
        device: str,
        index: int | None,
        hf_home: Path | None = None,
        max_image_tokens: int = 1024,
    ):
        super().__init__(spec, variant, device, index)
        self.hf_home = hf_home
        self.processor = None
        self.max_image_tokens = max_image_tokens
        self.batch_degradations = 0

    # ------------------------------------------------------------------
    def load(self) -> BackendReport:
        import torch
        from transformers import AutoConfig, AutoModelForImageTextToText, AutoProcessor

        started = time.time()
        if self.hf_home:
            os.environ.setdefault("HF_HOME", str(self.hf_home))
            os.environ.setdefault("HF_HUB_OFFLINE", "0")

        cfg = AutoConfig.from_pretrained(
            self.spec.repo, revision=self.spec.revision, trust_remote_code=False
        )
        dtype_name = str(self.variant.get("dtype", "bfloat16"))
        dtype = _resolve_dtype(dtype_name, torch)

        load_kwargs: dict[str, Any] = {
            "revision": self.spec.revision,
            "trust_remote_code": False,
            "low_cpu_mem_usage": True,
        }
        quant = None
        if self.variant.get("load_in_4bit"):
            from transformers import BitsAndBytesConfig

            quant = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type=self.variant.get("bnb_4bit_quant_type", "nf4"),
                bnb_4bit_compute_dtype=_resolve_dtype(
                    str(self.variant.get("bnb_4bit_compute_dtype", "bfloat16")), torch
                ),
                bnb_4bit_use_double_quant=bool(self.variant.get("bnb_4bit_use_double_quant", True)),
            )
        if dtype_name == "float8_e4m3fn":
            # Native FP8 checkpoint: transformers dequantises block-wise on the
            # fly.  Keep activations in bf16.
            load_kwargs["dtype"] = torch.float8_e4m3fn
            load_kwargs["_attn_implementation"] = self.variant.get("attn", "sdpa")
        else:
            load_kwargs["dtype"] = dtype
            load_kwargs["_attn_implementation"] = self.variant.get("attn", "sdpa")
        if quant is not None:
            load_kwargs["quantization_config"] = quant

        if self.device == "cuda":
            load_kwargs["device_map"] = self.variant.get("device_map", "auto")
            if self.index is not None:
                load_kwargs["max_memory"] = {self.index: _budget_gb()}

        self.model = AutoModelForImageTextToText.from_pretrained(self.spec.repo, **load_kwargs)
        self.processor = AutoProcessor.from_pretrained(self.spec.repo, revision=self.spec.revision)
        self.model.eval()
        extra: dict[str, Any] = {}
        try:
            self.processor.image_processor.min_pixels = 32 * 32
            self.processor.image_processor.max_pixels = self.max_image_tokens * 32 * 32
        except Exception:  # pragma: no cover - processor without knobs
            pass
        report = self._finish_load(started, extra)
        log.info(
            "vlm %s loaded on %s in %.1fs (peak %.2f GB)",
            self.spec.key, self.device, report.load_seconds, report.peak_vram_gb,
        )
        return report

    def unload(self) -> None:
        self.processor = None
        super().unload()

    # ------------------------------------------------------------------
    def describe_batch(
        self,
        images: Sequence[Image.Image],
        prompt: str,
        *,
        max_new_tokens: int = 768,
        temperature: float = 0.05,
        system: str | None = None,
        batch_size: int = 4,
    ) -> list[str]:
        """Run one prompt per image, packing several images per forward pass.

        Returns one string per input image, in input order.
        """
        if not self.loaded:
            raise RuntimeError("backend not loaded")
        if not images:
            return []
        results: list[str] = []
        bs = max(1, int(batch_size))
        start = 0
        while start < len(images):
            chunk = list(images[start:start + bs])
            try:
                results.extend(
                    self._describe_chunk(chunk, prompt, max_new_tokens, temperature, system)
                )
                start += len(chunk)
            except torch_OutOfMemory:
                if bs > 1:
                    # Inference-time OOM (a shared card filled up, or the frames
                    # are larger than the budget assumed).  Halve the batch and
                    # record the degradation rather than failing the stage.
                    self.batch_degradations += 1
                    bs = max(1, bs // 2)
                    free_cuda()
                    continue
                raise
        return results

    def _describe_chunk(
        self,
        images: Sequence[Image.Image],
        prompt: str,
        max_new_tokens: int,
        temperature: float,
        system: str | None,
    ) -> list[str]:
        import torch

        conversations: list[list[dict[str, Any]]] = []
        for _ in images:
            items: list[dict[str, Any]] = []
            if system:
                items.append({"type": "text", "text": system})
            items.append({"type": "image", "image": _PLACEHOLDER})
            items.append({"type": "text", "text": prompt})
            conversations.append([{"role": "user", "content": items}])

        texts = [
            self.processor.apply_chat_template(c, tokenize=False, add_generation_prompt=True)
            for c in conversations
        ]
        inputs = self.processor(
            text=texts,
            images=list(images),
            padding=True,
            return_tensors="pt",
        )
        device = self._primary_device()
        inputs = {k: (v.to(device) if hasattr(v, "to") else v) for k, v in inputs.items()}
        gen_kwargs: dict[str, Any] = {"max_new_tokens": max_new_tokens}
        if temperature >= 0.05:
            gen_kwargs.update({"do_sample": True, "temperature": temperature, "top_p": 0.9})
        else:
            gen_kwargs["do_sample"] = False
        with torch.inference_mode():
            out = self.model.generate(**inputs, **gen_kwargs)
        prompt_len = inputs["input_ids"].shape[1]
        trimmed = out[:, prompt_len:]
        return [
            t.strip()
            for t in self.processor.batch_decode(
                trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
            )
        ]

    def rank_images(
        self,
        images: Sequence["Image.Image"],
        prompt: str,
        *,
        max_new_tokens: int = 768,
        temperature: float = 0.05,
        system: str | None = None,
    ) -> str:
        """Comparative ranking: all images in ONE forward pass, JSON ranked list out.

        Falls back to an empty string on any failure so callers can degrade to
        independent scoring instead of failing the job.
        """
        import torch

        if not self.loaded or not images:
            return ""
        items: list[dict[str, Any]] = []
        if system:
            items.append({"type": "text", "text": system})
        for i, _ in enumerate(images):
            items.append({"type": "text", "text": f"Image {i + 1}:"})
            items.append({"type": "image", "image": _PLACEHOLDER})
        items.append({"type": "text", "text": prompt})
        try:
            convo = [[{"role": "user", "content": items}]]
            texts = [
                self.processor.apply_chat_template(
                    c, tokenize=False, add_generation_prompt=True
                )
                for c in convo
            ]
            inputs = self.processor(
                text=texts, images=list(images), padding=True, return_tensors="pt"
            )
            device = self._primary_device()
            inputs = {k: (v.to(device) if hasattr(v, "to") else v) for k, v in inputs.items()}
            gen_kwargs: dict[str, Any] = {"max_new_tokens": max_new_tokens}
            if temperature >= 0.05:
                gen_kwargs.update({"do_sample": True, "temperature": temperature, "top_p": 0.9})
            else:
                gen_kwargs["do_sample"] = False
            with torch.inference_mode():
                out = self.model.generate(**inputs, **gen_kwargs)
            trimmed = out[:, inputs["input_ids"].shape[1]:]
            return self.processor.batch_decode(
                trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
            )[0].strip()
        except Exception:  # noqa: BLE001 - ranking is advisory; never crash selection
            log.warning("rank_images failed; caller should use independent scoring", exc_info=True)
            return ""

    def _primary_device(self):
        try:
            return next(self.model.parameters()).device
        except Exception:  # pragma: no cover
            return "cuda" if self.device == "cuda" else "cpu"


def _resolve_dtype(name: str, torch) -> "torch.dtype":
    table = {
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float16": torch.float16,
        "fp16": torch.float16,
        "float32": torch.float32,
        "fp32": torch.float32,
        "float8_e4m3fn": getattr(torch, "float8_e4m3fn", torch.bfloat16),
    }
    return table.get(name.lower(), torch.bfloat16)


def _budget_gb() -> str:
    raw = os.environ.get("YT2WIKI_VRAM_BUDGET_GB", "")
    try:
        return f"{float(raw)}GiB"
    except ValueError:
        return "15GiB"
