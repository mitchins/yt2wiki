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

log = logging.getLogger("yt2w.remote")

_NOT_LOADED_MSG = "backend not loaded"


class TruncatedResponseError(RuntimeError):
    """The server stopped generation at its token limit (`finish_reason="length"`).

    Carries the partial text for diagnostics. Callers must not publish the
    partial content as a complete answer; the writer treats this as a
    recoverable failure with a bounded retry, then fails the section loudly.
    """

    def __init__(self, partial: str = ""):
        super().__init__(f"remote generation truncated at token limit ({len(partial)} chars partial)")
        self.partial = partial

class RemoteOpenAIBackend(BaseBackend):
    # The server speaks modern structured outputs (json_schema/strict).
    # Callers check this flag before requesting a response schema; backends
    # without it keep today's behaviour and rely on downstream validation.
    supports_structured_schema = True

    def __init__(self, spec: ModelSpec, variant: dict[str, Any], device: str, index: int | None, *, base_url: str, model: str = "", api_key_env: str = "", timeout: float = 60.0):
        super().__init__(spec, variant, device, index)
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self._headers = {}
        # Per-endpoint verdict: does this server accept schemas alongside
        # images? None = untested. Some engines (observed) accept json_schema
        # for text but refuse any schema on image requests; the judge adapts
        # once and remembers, instead of failing every multi-image call.
        self._schema_with_images: bool | None = None
        self.last_finish_reason: str | None = None
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

    def _response_format(self, json_mode: bool,
                           response_schema: dict[str, Any] | None,
                           schema_name: str) -> dict[str, Any] | None:
        # Preferred: OpenAI-standard json_schema with strict enforcement, so
        # the server (not just the prompt) enforces enums, required fields and
        # shapes. vLLM >= 0.32 honours this; the legacy guided_json field is
        # silently ignored there, so it is not used.
        if response_schema is not None:
            return {"type": "json_schema",
                    "json_schema": {"name": schema_name,
                                    "schema": response_schema,
                                    "strict": True}}
        if json_mode:
            return {"type": "json_object"}
        return None

    def chat(self, messages: Sequence[dict[str, str]], *, max_new_tokens: int = 2048, temperature: float = 0.1, top_p: float = 0.9, stop: Sequence[str] = (), json_mode: bool = False, response_schema: dict[str, Any] | None = None, schema_name: str = "response") -> str:
        if not self.loaded:
            raise RuntimeError(_NOT_LOADED_MSG)
        payload: dict[str, Any] = {
            "messages": list(messages),
            "max_tokens": max_new_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if self.model_name:
            payload["model"] = self.model_name
        fmt = self._response_format(json_mode, response_schema, schema_name)
        if fmt is not None:
            payload["response_format"] = fmt
        if stop:
            payload["stop"] = list(stop)
        data = self._post(payload)
        # OpenAI-compatible response
        try:
            choice = data["choices"][0]
            self.last_finish_reason = choice.get("finish_reason")
            if self.last_finish_reason == "length":
                partial = choice.get("message", {}).get("content") or ""
                if isinstance(partial, list):
                    partial = "\n".join(p.get("text", "") for p in partial if isinstance(p, dict))
                raise TruncatedResponseError(str(partial or "").strip())
            content = choice["message"]["content"]
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
        except TruncatedResponseError:
            raise
        except Exception as exc:
            raise RuntimeError(f"remote chat parse failed: {exc} payload={data}") from exc

    def chat_json(self, messages: Sequence[dict[str, str]], *, max_new_tokens: int = 2048, temperature: float = 0.1, response_schema: dict[str, Any] | None = None, schema_name: str = "response", **kw) -> Any:
        raw = self.chat(messages, max_new_tokens=max_new_tokens, temperature=temperature, json_mode=True, response_schema=response_schema, schema_name=schema_name, **kw)
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

    def rank_images(self, images: Sequence[Any], prompt: str, *, max_new_tokens: int = 400, temperature: float = 0.0, system: str | None = None, response_schema: dict[str, Any] | None = None, schema_name: str = "response") -> str:
        if not self.loaded:
            raise RuntimeError(_NOT_LOADED_MSG)
        if not self.loaded:
            raise RuntimeError(_NOT_LOADED_MSG)
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
        want_schema = response_schema is not None and self._schema_with_images is not False
        fmt = self._response_format(False, response_schema if want_schema else None, schema_name)
        if fmt is not None:
            payload["response_format"] = fmt
        try:
            data = self._post(payload)
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = (exc.response.text or "").lower() if exc.response is not None else ""
            except Exception:  # noqa: BLE001 - best-effort body read
                pass
            if (want_schema and response_schema is not None
                    and exc.response is not None and exc.response.status_code == 400
                    and "schema" in body):
                # This endpoint refuses schemas alongside images: retry once
                # plain and remember, so the judge degrades to validated
                # plain JSON instead of failing every multi-image call.
                log.warning("endpoint rejects schemas with images; retrying plain")
                self._schema_with_images = False
                payload.pop("response_format", None)
                data = self._post(payload)
            else:
                raise
        else:
            if want_schema and response_schema is not None:
                self._schema_with_images = True
        try:
            content = data["choices"][0]["message"]["content"]
            if content is None:
                raise RuntimeError(f"remote vision returned no content: {data}")
            if isinstance(content, list):
                content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
            text = (content or "").strip()
            if not text:
                raise RuntimeError(f"remote vision returned empty content: {data}")
            return text
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"remote vision parse failed: {exc} payload={data}") from exc

    def describe_batch(self, images: Sequence[Any], prompt: str, *, max_new_tokens: int = 120, temperature: float = 0.0, system: str | None = None, batch_size: int = 4) -> list[str]:
        # One call per image (simple, handles any batch_size)
        out: list[str] = []
        for im in images:
            out.append(self.rank_images([im], prompt, max_new_tokens=max_new_tokens, temperature=temperature, system=system))
        return out

    def subject_of(self, *a, **kw):
        # Wrapper for judge's subject probe — just describe
        return self.rank_images(*a, **kw)
