"""Remote OpenAI-compatible backend for inference providers.

Used when InferenceConfig assigns a role (writer/vision) to a provider.
No local weights, no GPU load — the model lives in a vLLM or llama.cpp
server elsewhere. The job records the provider URL and model id so that
later settings changes do not rewrite history.
"""
from __future__ import annotations
import base64, io, json, logging, os, time
from pathlib import Path
from typing import Any, Sequence
import httpx
from ..config import ModelSpec
from ..gpu import BackendReport
from .base import BaseBackend

log = logging.getLogger("yt2wiki.remote")

class RemoteOpenAIBackend(BaseBackend):
    def __init__(self, spec: ModelSpec, variant: dict[str, Any], device: str, index: int | None, *, base_url: str, model: str = "", api_key_env: str = "", timeout: float = 60.0):
        super().__init__(spec, variant, device, index)
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self._headers = {}
        if api_key_env:
            key = os.environ.get(api_key_env, "")
            if key:
                self._headers["Authorization"] = f"Bearer {key}"

    def load(self) -> BackendReport:
        started = time.time()
        # No local load — just verify endpoint reachable and record
        # Actual qualification (real generation) is done by inference.qualify
        self.model = object()  # marker for loaded
        report = self._finish_load(started, extra={"remote_url": self.base_url, "remote_model": self.model_name})
        log.info("remote %s (%s) ready at %s", self.spec.key, self.model_name or "default", self.base_url)
        return report

    def unload(self) -> None:
        self.model = None

    # ------------------------------------------------------------------
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        # Use sync httpx for simplicity; timeout covers both connect and read
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

    def chat(self, messages: Sequence[dict[str, str]], *, max_new_tokens: int = 2048, temperature: float = 0.1, top_p: float = 0.9, stop: Sequence[str] = (), json_mode: bool = False) -> str:
        if not self.loaded:
            raise RuntimeError("backend not loaded")
        payload: dict[str, Any] = {
            "messages": list(messages),
            "max_tokens": max_new_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if self.model_name:
            payload["model"] = self.model_name
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if stop:
            payload["stop"] = list(stop)
        data = self._post(payload)
        # OpenAI-compatible response
        try:
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                # multimodal content list
                parts = [p.get("text","") for p in content if isinstance(p, dict)]
                content = "\n".join(parts)
            text = (content or "").strip()
            # For some providers reasoning_content is separate
            reasoning = data["choices"][0]["message"].get("reasoning_content") or ""
            if reasoning and not text:
                # If model put answer in reasoning (rare), fall back
                text = reasoning.strip()
            return text
        except Exception as exc:
            raise RuntimeError(f"remote chat parse failed: {exc} payload={data}") from exc

    def chat_json(self, messages: Sequence[dict[str, str]], *, max_new_tokens: int = 2048, temperature: float = 0.1, **kw) -> Any:
        raw = self.chat(messages, max_new_tokens=max_new_tokens, temperature=temperature, json_mode=True, **kw)
        from .llm import extract_json
        return extract_json(raw)

    # Vision: same chat but with images encoded as data urls
    def _encode_image(self, im) -> str:
        buf = io.BytesIO()
        # Use same thumbnail logic as local: cap via max_image_tokens? For remote, send as-is but capped at 1024 longest
        try:
            w, h = im.size
            if max(w, h) > 1024:
                scale = 1024 / max(w, h)
                im = im.resize((int(w*scale), int(h*scale)))
        except Exception:
            pass
        im.convert("RGB").save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()

    def rank_images(self, images: Sequence[Any], prompt: str, *, max_new_tokens: int = 400, temperature: float = 0.0, system: str | None = None) -> str:
        if not self.loaded:
            raise RuntimeError("backend not loaded")
        content: list[dict[str, Any]] = []
        for i, im in enumerate(images):
            content.append({"type":"text","text":f"Image {i+1}:"})
            try:
                b64 = self._encode_image(im)
                content.append({"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+b64}})
            except Exception as exc:
                log.warning("remote encode failed for image %d: %s", i, exc)
        content.append({"type":"text","text":prompt})
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role":"system","content":system})
        msgs.append({"role":"user","content":content})
        payload: dict[str, Any] = {"messages": msgs, "max_tokens": max_new_tokens, "temperature": temperature}
        if self.model_name:
            payload["model"] = self.model_name
        try:
            data = self._post(payload)
            return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception:  # noqa: BLE001 - ranking is advisory
            log.warning("remote rank_images failed", exc_info=True)
            return ""

    def describe_batch(self, images: Sequence[Any], prompt: str, *, max_new_tokens: int = 120, temperature: float = 0.0, system: str | None = None, batch_size: int = 4) -> list[str]:
        # One call per image (simple, handles any batch_size)
        out: list[str] = []
        for im in images:
            out.append(self.rank_images([im], prompt, max_new_tokens=max_new_tokens, temperature=temperature, system=system))
        return out

    def subject_of(self, *a, **kw):
        # Wrapper for judge's subject probe — just describe
        return self.rank_images(*a, **kw)
