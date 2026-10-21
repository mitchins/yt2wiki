"""Text backend (Qwen3.x, SmolLM3, and any `transformers` causal LM)."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Sequence

from ..config import ModelSpec
from ..gpu import BackendReport
from .base import BaseBackend

log = logging.getLogger("yt2wiki.llm")


class TransformersLLM(BaseBackend):
    """Causal LM with a plain `chat()` entry point.

    `enable_thinking=False` is requested for every task: these prompts need
    deterministic, extractable JSON rather than a free-form rationale, and the
    reasoning budget is a large fraction of per-call latency.
    """

    def __init__(
        self,
        spec: ModelSpec,
        variant: dict[str, Any],
        device: str,
        index: int | None,
        hf_home: Path | None = None,
    ):
        super().__init__(spec, variant, device, index)
        self.hf_home = hf_home
        self.tokenizer = None

    def load(self) -> BackendReport:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        started = time.time()
        if self.hf_home:
            os.environ.setdefault("HF_HOME", str(self.hf_home))

        load_kwargs: dict[str, Any] = {
            "revision": self.spec.revision,
            "trust_remote_code": False,
            "low_cpu_mem_usage": True,
            "_attn_implementation": "sdpa",
        }
        if self.variant.get("load_in_4bit"):
            from transformers import BitsAndBytesConfig

            load_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type=self.variant.get("bnb_4bit_quant_type", "nf4"),
                bnb_4bit_compute_dtype={
                    "bfloat16": torch.bfloat16,
                    "float16": torch.float16,
                }.get(str(self.variant.get("bnb_4bit_compute_dtype", "bfloat16")), torch.bfloat16),
                bnb_4bit_use_double_quant=bool(self.variant.get("bnb_4bit_use_double_quant", True)),
                llm_int8_enable_fp32_cpu_offload=True,
            )
            load_kwargs["device_map"] = "auto"
        else:
            dtype = {
                "bfloat16": torch.bfloat16,
                "float16": torch.float16,
                "float32": torch.float32,
            }.get(str(self.variant.get("dtype", "bfloat16")), torch.bfloat16)
            load_kwargs["dtype"] = dtype
            if self.device == "cuda":
                load_kwargs["device_map"] = self.variant.get("device_map", "auto")

        if self.device == "cuda" and self.index is not None:
            load_kwargs.setdefault("max_memory", {self.index: _budget_gb()})

        self.model = AutoModelForCausalLM.from_pretrained(self.spec.repo, **load_kwargs)
        self.tokenizer = AutoTokenizer.from_pretrained(self.spec.repo, revision=self.spec.revision)
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model.eval()
        report = self._finish_load(started)
        log.info(
            "llm %s loaded on %s in %.1fs (peak %.2f GB)",
            self.spec.key, self.device, report.load_seconds, report.peak_vram_gb,
        )
        return report

    def unload(self) -> None:
        self.tokenizer = None
        super().unload()

    # ------------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_new_tokens: int = 2048,
        temperature: float = 0.1,
        top_p: float = 0.9,
        stop: Sequence[str] = (),
        json_mode: bool = False,
    ) -> str:
        if not self.loaded:
            raise RuntimeError("backend not loaded")
        import torch

        prompt = self.tokenizer.apply_chat_template(
            list(messages),
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self._primary_device())
        pad = self.tokenizer.pad_token_id or self.tokenizer.eos_token_id
        gen: dict[str, Any] = {
            "max_new_tokens": max_new_tokens,
            "pad_token_id": pad,
            "eos_token_id": self.tokenizer.eos_token_id,
        }
        if temperature > 0.02:
            gen.update({"do_sample": True, "temperature": max(temperature, 1e-3), "top_p": top_p})
        else:
            gen["do_sample"] = False
        with torch.inference_mode():
            out = self.model.generate(**inputs, **gen)
        text = self.tokenizer.decode(
            out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
        )
        for s in stop:
            if s and s in text:
                text = text.split(s, 1)[0]
        return text.strip()

    def chat_with_dump(self, messages, *, dump_path=None, **kwargs) -> Any:
        """chat_json that persists the raw reply for later failure analysis."""
        retries = kwargs.pop("retries", 1)
        raw = self.chat(messages, **kwargs)
        if dump_path is not None:
            try:
                with open(dump_path, "a", encoding="utf-8") as fh:
                    fh.write("\n===== RAW REPLY =====\n" + raw + "\n")
            except OSError:
                pass
        try:
            return extract_json(raw)
        except ValueError:
            if retries <= 0:
                raise
            strict = list(messages) + [{
                "role": "user",
                "content": "Your previous reply was not parseable JSON. "
                           "Reply with ONLY a JSON object, no prose and no code fences.",
            }]
            raw2 = self.chat(strict, **kwargs)
            if dump_path is not None:
                try:
                    with open(dump_path, "a", encoding="utf-8") as fh:
                        fh.write("\n===== RAW REPLY (retry) =====\n" + raw2 + "\n")
                except OSError:
                    pass
            return extract_json(raw2)

    def chat_json(self, messages: Sequence[dict[str, str]], *, retries: int = 1, **kwargs) -> Any:
        """Chat and parse the first JSON object/array found in the reply.

        One retry with a stricter instruction is attempted when the reply cannot
        be parsed, because a truncated code fence is a common LLM failure mode.
        """
        raw = self.chat(messages, **kwargs)
        try:
            return extract_json(raw)
        except ValueError:
            if retries <= 0:
                raise
            strict = list(messages) + [
                {
                    "role": "user",
                    "content": "Your previous reply was not parseable JSON. "
                               "Reply with ONLY a JSON object, no prose and no code fences.",
                }
            ]
            raw2 = self.chat(strict, **kwargs)
            return extract_json(raw2)

    def _primary_device(self):
        try:
            return next(self.model.parameters()).device
        except Exception:  # pragma: no cover
            return "cuda" if self.device == "cuda" else "cpu"


_CODE_FENCE = re.compile(r"```(?:json|jsonc|JSON)?\s*(.*?)```", re.DOTALL)


def _strip_fence(text: str) -> str:
    m = _CODE_FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


def _json_span(text: str) -> str | None:
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _scan(text: str) -> tuple[int, list[str], bool]:
    """Return (start, open stack, in-string flag) for a JSON fragment."""
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return -1, [], False
    stack: list[str] = []
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    return start, stack, in_string


def repair_truncated_json(fragment: str) -> str | None:
    """Best-effort repair of JSON cut off by the token limit.

    Tries successively shorter safe cut points (container, separator and string
    boundaries) and appends the closers each one needs.  Returns the first
    candidate that actually parses, or None.
    """
    import json

    start, _, _ = _scan(fragment)
    if start < 0:
        return None
    cuts = [i + 1 for i, ch in enumerate(fragment) if ch in ",}]"]
    cuts.append(len(fragment))
    for cut in reversed(cuts):
        body = fragment[start:cut].rstrip()
        while body and body[-1] == ",":
            body = body[:-1].rstrip()
        if not body:
            continue
        _, stack, in_string = _scan(body)
        if in_string or not stack:
            # cutting inside a string cannot be repaired by closing brackets
            continue
        closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
        candidate = body + closers
        try:
            json.loads(candidate)
        except Exception:
            continue
        return candidate
    return None


def extract_json(text: str) -> Any:
    """Robustly pull a JSON object/array out of a chatty LLM reply."""
    import json

    text = text.strip()
    candidates = [_strip_fence(text), text, text.strip().rstrip(".,;")]
    span = _json_span(_strip_fence(text)) or _json_span(text)
    if span:
        candidates.append(span)
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except Exception:
            continue
    fragment = _json_span(_strip_fence(text))
    if fragment is None:
        fragment = _strip_fence(text)
    for cand in (fragment, text):
        repaired = repair_truncated_json(cand)
        if repaired:
            try:
                return json.loads(repaired)
            except Exception:
                continue
    raise ValueError(f"no JSON found in model reply ({len(text)} chars): {text[:300]}")


def _budget_gb() -> str:
    """Max memory cap for a single GPU, from YT2WIKI_VRAM_BUDGET_GB."""
    raw = os.environ.get("YT2WIKI_VRAM_BUDGET_GB", "")
    try:
        return f"{float(raw):.2f}GiB"
    except ValueError:
        return "15GiB"
