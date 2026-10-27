"""Model acquisition: role -> backend, with recorded fallbacks."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from ..config import ModelSpec, Settings
from ..gpu import (
    BackendReport,
    cuda_available,
    free_cuda_memory,
    gpu_session,
    peak_vram_gb,
    pick_device,
    reset_peak,
    torch_device,
)
from .asr import WhisperBackend
from .asr_http import HttpAsrBackend, resolve_env_variant
from .base import BaseBackend
from .llama_server import LlamaServerVLM
from .llm import TransformersLLM
from .remote_openai import RemoteOpenAIBackend
from .vlm import TransformersVLM

log = logging.getLogger("yt2wiki.loader")


class ModelManager:
    """Owns the registry lookups and hands out short-lived backends."""

    def __init__(self, settings: Settings, recorder=None, inference_snapshot: dict[str, Any] | None = None):
        self.settings = settings
        self.recorder = recorder  # callable(dict) for provenance/fallback records
        self.inference_snapshot = inference_snapshot  # job-captured provider identities, if any
        self.device, self.index = pick_device(settings.device_index)
        os.environ.setdefault("HF_HOME", str(settings.hf_home))
        # No progress bars: this runs as a service, and a closed stderr pipe
        # turns a tqdm status printer into a BrokenPipeError mid-stage.
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TQDM_DISABLE", "1")
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
        os.environ["YT2WIKI_VRAM_BUDGET_GB"] = f"{settings.vram_budget_gb():.2f}"
        self.loaded: dict[str, BaseBackend] = {}
        self.load_log: list[dict] = []
        self.headroom_log: list[dict] = []

    # ------------------------------------------------------------------
    def headroom_for(self, role: str, *, exclude: Sequence[int] = ()) -> "Any":
        """Where and how much this role's model will claim on the GPU."""
        from .headroom import plan

        info = plan(self.settings, role, preferred=self.settings.device_index,
                    exclude=list(exclude))
        if info.reason not in ("ok", "cpu"):
            self.headroom_log.append({"role": role, **info.to_dict()})
            log.warning("headroom for role %s: %s", role, info.reason)
        if info.device_index is not None and info.device_index != self.index:
            self.index = info.device_index
        os.environ["YT2WIKI_VRAM_BUDGET_GB"] = (
            f"{info.capped_gb:.2f}" if info.capped_gb else f"{self.settings.vram_budget_gb():.2f}"
        )
        return info

    # ------------------------------------------------------------------
    def _record(self, **fields) -> None:
        entry = {"profile": self.settings.profile, **fields}
        self.load_log.append(entry)
        if self.recorder is not None:
            try:
                self.recorder(entry)
            except Exception:  # pragma: no cover - logging must never break a job
                log.exception("model recorder failed")

    def _release(self, backend: BaseBackend) -> None:
        """Unload then free CUDA memory (unload errors must not mask job errors)."""
        try:
            backend.unload()
        finally:
            free_cuda_memory()

    def _discard(self, candidate: BaseBackend) -> None:
        """Best-effort unload of a failed load attempt, then free CUDA memory."""
        try:
            candidate.unload()
        except Exception:
            pass
        free_cuda_memory()

    def _snapshot_role_check(self, role: str, entry: dict[str, Any],
                             lookup: str) -> None:
        """Fail rather than fall back when a job-captured provider is unusable."""
        if not entry.get("reachable"):
            raise RuntimeError(
                f"inference provider '{entry.get('provider')}' for role '{lookup}' "
                f"was not reachable at submission time.")
        if role == "vision" and not entry.get("qualified_for_vision"):
            raise RuntimeError(
                f"provider '{entry.get('provider')}' was not qualified for vision "
                f"at submission time.")
        if role == "text" and not entry.get("qualified_for_writer"):
            # Writer may be reachable but not yet qualified — fail rather than fall back
            raise RuntimeError(
                f"provider '{entry.get('provider')}' was not qualified for writing "
                f"at submission time.")
        if role == "asr" and not entry.get("qualified_for_asr"):
            raise RuntimeError(
                f"provider '{entry.get('provider')}' was not qualified for "
                f"transcription at submission time.")

    def _snapshot_spec(self, role: str, entry: dict[str, Any]) -> tuple[ModelSpec, dict[str, Any]]:
        """Synthetic (spec, provider) from a job-captured inference snapshot."""
        provider = {"url": entry.get("url"), "model": entry.get("model") or "",
                    "api_key_env": entry.get("api_key_env") or "",
                    "timeout": _role_timeout(role, entry.get("timeout"))}
        variant = {"model": provider.get("model") or "", "url": provider["url"],
                   "api_key_env": provider.get("api_key_env") or "",
                   "timeout": float(provider.get("timeout", 90.0))}
        engine = "asr-http" if role == "asr" else "remote-openai"
        spec = ModelSpec(
            key=f"remote-{entry.get('provider')}",
            role=role,
            engine=engine,
            repo=provider.get("model") or entry.get("url", ""),
            revision="remote",
            provider=entry.get("provider") or "",
            notes=f"Remote {provider['url']} via {entry.get('provider')}",
            variants={self.settings.profile: dict(variant)},
        )
        return spec, provider

    def _config_role_check(self, role: str, provider_name: str,
                           status: dict[str, Any]) -> None:
        """Fail rather than fall back when the live provider is unusable."""
        if not status.get("ok"):
            raise RuntimeError(
                f"inference provider '{provider_name}' for role '{self._lookup_role(role)}' is not reachable "
                f"(test it in Settings). Status: {status.get('error') or 'not tested'}")
        if role == "vision" and not status.get("qualified_for_vision"):
            raise RuntimeError(
                f"provider '{provider_name}' is not qualified for vision "
                f"(needs genuine multi-image judging). Qualify it in Settings.")
        if role == "text" and not status.get("qualified_for_writer"):
            raise RuntimeError(
                f"provider '{provider_name}' is not qualified for writing "
                f"(needs real JSON generation). Qualify it in Settings.")
        if role == "asr" and not status.get("qualified_for_asr"):
            raise RuntimeError(
                f"provider '{provider_name}' is not qualified for transcription "
                f"(needs the transcription protocol probe). Qualify it in Settings.")

    def _lookup_role(self, role: str) -> str:
        """Pipeline role → operator-console assignment name (text → writer)."""
        return {"text": "writer"}.get(role, role)

    def _config_spec(self, role: str, provider_name: str,
                     provider: dict[str, Any], status: dict[str, Any]) -> tuple[ModelSpec, dict[str, Any]]:
        """Synthetic (spec, provider) from the live inference config."""
        # Explicit model takes precedence over discovered models, then URL.
        repo = provider.get("model") or (status.get("models") or [provider.get("url", "")])[0]
        variant = {"model": provider.get("model") or "", "url": provider["url"],
                   "api_key_env": provider.get("api_key_env") or "",
                   "timeout": _role_timeout(role, provider.get("timeout"))}
        engine = "asr-http" if role == "asr" else "remote-openai"
        spec = ModelSpec(
            key=f"remote-{provider_name}",
            role=role,
            engine=engine,
            repo=repo,
            revision="remote",
            provider=provider_name,
            notes=f"Remote {provider['url']} via {provider_name}",
            variants={self.settings.profile: dict(variant)},
        )
        return spec, provider

    def _remote_spec(self, role: str) -> tuple[ModelSpec | None, dict[str, Any] | None]:
        """Return (synthetic spec, provider) when this role is assigned to a remote endpoint.

        When a job-captured snapshot is present (web jobs), it is authoritative
        and later live settings changes do not rewrite history. Otherwise the
        live inference config is consulted; CLI jobs without a config simply
        return None and fall through to the local registry.

        Role mapping: the pipeline asks for ``text`` (the writing role) while
        the operator console names that assignment ``writer`` — one provider
        entry serves V9 writing (and, when also assigned, vision) without
        duplicate configuration. ``asr`` maps to the ``asr`` (transcription)
        assignment.
        """
        lookup = self._lookup_role(role)
        try:
            if self.inference_snapshot is not None:
                entry = self.inference_snapshot.get(lookup)
                if not entry:
                    return None, None
                # Snapshot is already the effective identity at submission time
                self._snapshot_role_check(role, entry, lookup)
                return self._snapshot_spec(role, entry)
            from ..web import inference as _inf
            cfg = _inf.load(self.settings.paths)
            provider_name = (cfg.get("roles") or {}).get(lookup)
            if not provider_name:
                return None, None
            provider = (cfg.get("providers") or {}).get(provider_name)
            if not provider:
                return None, None
            status = _inf.load_status(self.settings.paths).get(provider_name, {})
            self._config_role_check(role, provider_name, status)
            return self._config_spec(role, provider_name, provider, status)
        except RuntimeError:
            raise
        except Exception:
            return None, None

    def _build_asr_http(self, spec: ModelSpec) -> BaseBackend:
        """Registry-default HTTP ASR: resolve env indirection, require a URL."""
        variant = spec.variant(self.settings.profile)
        # Variant carries url_env/model_env (resolved here); inference-assigned
        # providers arrive with url already set.
        resolved = resolve_env_variant(variant) if "url_env" in variant else dict(variant)
        if not (resolved.get("url") or "").strip():
            raise RuntimeError(
                "no ASR server configured: assign the transcription role to a "
                "provider (Settings → Inference) or set YT2WIKI_ASR_URL")
        return HttpAsrBackend(
            spec, variant, self.device, self.index,
            base_url=resolved.get("url") or "",
            model=resolved.get("model") or "",
            api_key_env=resolved.get("api_key_env") or "",
            timeout=float(resolved.get("timeout", 300.0)),
        )

    def _build_transformers(self, role: str, spec: ModelSpec) -> BaseBackend:
        variant = spec.variant(self.settings.profile)
        if role == "vision":
            return TransformersVLM(
                spec, variant, self.device, self.index, self.settings.hf_home,
                max_image_tokens=self.settings.tuning.vision_image_tokens,
            )
        return TransformersLLM(spec, variant, self.device, self.index, self.settings.hf_home)

    def build(self, role: str, spec: ModelSpec) -> BaseBackend:
        variant = spec.variant(self.settings.profile)
        if spec.engine == "remote-openai":
            # Synthetic remote spec: variant carries url/model/api_key_env
            return RemoteOpenAIBackend(
                spec, variant, self.device, self.index,
                base_url=variant.get("url") or variant.get("base_url") or "",
                model=variant.get("model") or "",
                api_key_env=variant.get("api_key_env") or "",
                timeout=float(variant.get("timeout", 90.0)),
            )
        if spec.engine == "faster-whisper":
            return WhisperBackend(spec, variant, self.device, self.index, self.settings.hf_home)
        if spec.engine == "asr-http":
            return self._build_asr_http(spec)
        if spec.engine == "llamacpp-server":
            return LlamaServerVLM(spec, variant, self.device, self.index,
                                  self.settings.hf_home,
                                  max_image_tokens=self.settings.tuning.vision_image_tokens)
        if spec.engine == "transformers":
            return self._build_transformers(role, spec)
        raise RuntimeError(f"no builder for engine {spec.engine!r}")

    @contextmanager
    def _load_http_asr(self, role: str, spec: ModelSpec) -> Iterator[BaseBackend]:
        """HTTP transcription session: no GPU headroom, no OOM fallback.

        A failing server is a loud job error, not a retry; failures are
        recorded like local load failures, then re-raised.
        """
        try:
            backend = self.build(role, spec)
            report = backend.load()
        except Exception as exc:  # noqa: BLE001 - record, then fail loud
            self._record(
                role=role, model_key=spec.key, repo=spec.repo,
                revision=spec.revision, engine=spec.engine,
                ok=False, device="http", device_index=None,
                error=str(exc)[:600], fallback_of="",
            )
            raise
        self._record(
            role=role, model_key=spec.key, repo=spec.repo,
            revision=spec.revision, engine=spec.engine,
            variant=backend.report.variant, ok=True,
            device="http", device_index=None,
            vram_budget_gb=0, headroom_reason="http-asr",
            load_seconds=report.load_seconds, peak_vram_gb=0,
            fallback_of="", note=f"http {getattr(backend, 'server', '')}",
        )
        try:
            yield backend
        finally:
            self._release(backend)

    def _load_local_attempt(self, role: str, spec: ModelSpec,
                            fallback_of: str) -> tuple[BaseBackend | None, Any, BaseBackend | None, Exception | None]:
        """One spec, up to two OOM re-plans; (backend, info, candidate, error)."""
        backend: BaseBackend | None = None
        last_error: Exception | None = None
        candidate: BaseBackend | None = None
        info: Any = None
        excluded: list[int] = []
        for attempt in range(2):
            info = self.headroom_for(role, exclude=excluded)
            candidate = self.build(role, spec)
            try:
                report = candidate.load()
                backend = candidate
                break
            except Exception as exc:  # noqa: BLE001
                if not _is_cuda_oom(exc) or attempt == 1:
                    last_error = exc
                    self._record(
                        role=role, model_key=spec.key, repo=spec.repo, ok=False,
                        device=f"cuda:{info.device_index}", error=str(exc)[:600],
                        fallback_of=fallback_of,
                    )
                    self._discard(candidate)
                    # Try the next fallback model (smaller) instead of
                    # retrying the same spec — this is where fallbacks
                    # like text-9b → text-4b are exercised.
                    break
                excluded.append(info.device_index)
                self._record(
                    role=role, model_key=spec.key, repo=spec.repo, ok=False,
                    device=f"cuda:{info.device_index}", error=f"OOM on this card; {exc}"[:600],
                    fallback_of=fallback_of,
                )
                log.warning("cuda OOM loading %s on device %s; re-planning",
                            spec.key, info.device_index)
                self._discard(candidate)
                continue
        else:  # pragma: no cover - defensive
            raise RuntimeError(f"could not allocate {spec.key}")
        return backend, info, candidate, last_error

    @contextmanager
    def acquire(self, role: str) -> Iterator[BaseBackend]:
        """Load the requested model (or its recorded fallback) and release it.

        If the inference config assigns this role to a remote provider, that
        provider is used (or the job fails) — no silent fallback to local.
        Otherwise the local registry chain is tried as before.

        On a CUDA out-of-memory at load time the attempt is re-planned on a
        different card (one load) before giving up, because this box is shared
        and a card can fill up between the headroom check and the allocation.
        """
        # Remote provider takes precedence when configured for this role
        remote_spec, provider = self._remote_spec(role)
        if remote_spec is not None and provider is not None:
            # Remote has no GPU headroom and no OOM fallback
            backend = self.build(role, remote_spec)
            report = backend.load()
            self._record(
                role=role, model_key=remote_spec.key, repo=remote_spec.repo,
                revision=remote_spec.revision, engine=remote_spec.engine,
                variant=backend.report.variant, ok=True,
                device="remote", device_index=None,
                vram_budget_gb=0, headroom_reason="remote",
                load_seconds=report.load_seconds, peak_vram_gb=0,
                fallback_of="", note=f"remote {provider['url']}",
            )
            try:
                yield backend
            finally:
                self._release(backend)
            return
        chain = self.settings.model_with_fallbacks(role)
        if chain and chain[0].engine == "asr-http":
            with self._load_http_asr(role, chain[0]) as backend:
                yield backend
            return
        backend: BaseBackend | None = None
        last_error: Exception | None = None
        for i, spec in enumerate(chain):
            fallback_of = chain[0].key if i > 0 else ""
            reset_peak()
            loaded, info, candidate, error = self._load_local_attempt(
                role, spec, fallback_of)
            last_error = error
            if loaded is not None:
                backend = loaded
                self._record(
                    role=role,
                    model_key=spec.key,
                    repo=spec.repo,
                    revision=spec.revision,
                    engine=spec.engine,
                    variant=loaded.report.variant,
                    ok=True,
                    device=self.device,
                    device_index=self.index,
                    vram_budget_gb=info.capped_gb,
                    headroom_reason=info.reason,
                    load_seconds=candidate.report.load_seconds,
                    peak_vram_gb=candidate.report.peak_vram_gb,
                    fallback_of=fallback_of,
                    note=spec.notes,
                )
                if fallback_of:
                    log.warning(
                        "role %s fell back from %s to %s (%s)",
                        role, fallback_of, spec.key, spec.notes.strip()[:120],
                    )
                break

        if backend is None:
            raise RuntimeError(
                f"could not load any model for role {role!r}: {last_error}"
            )
        try:
            yield backend
        finally:
            self._release(backend)

    def acquire_all(self, *roles: str) -> dict[str, BaseBackend]:
        """Eagerly load several roles (used by the API when a worker is idle)."""
        out: dict[str, BaseBackend] = {}
        for role in roles:
            ctx = self.acquire(role)
            backend = ctx.__enter__()
            out[role] = backend
            out[role + ".__ctx__"] = ctx  # type: ignore[assignment]
        return out

    def release_all(self, loaded: dict[str, BaseBackend]) -> None:
        for key, value in list(loaded.items()):
            if key.endswith(".__ctx__"):
                try:
                    value.__exit__(None, None, None)  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover
                    log.exception("release failed")
                loaded.pop(key, None)

    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "device": self.device,
            "device_index": self.index,
            "cuda": cuda_available(),
            "current_vram_gb": round(peak_vram_gb(), 3),
            "vram_budget_gb": self.settings.vram_budget_gb(),
            "load_log": self.load_log,
            "headroom": self.headroom_log,
        }


def _role_timeout(role: str, raw: Any) -> float:
    """Per-role default timeout: transcription of long audio needs minutes."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) \
            and math.isfinite(raw) and raw > 0:
        return float(raw)
    if role == "asr":
        try:
            return max(60.0, float(os.environ.get("YT2WIKI_ASR_TIMEOUT") or 300.0))
        except ValueError:
            return 300.0
    if role == "text":
        # Long structured sections can take a minute on a shared server; the
        # operator may tune this, but it stays a bounded per-request timeout.
        try:
            value = float(os.environ.get("YT2WIKI_WRITER_TIMEOUT") or 90.0)
        except ValueError:
            return 90.0
        return value if math.isfinite(value) and value > 0 else 90.0
    return 90.0


@contextmanager
def stage_gpu(label: str) -> Iterator[None]:
    """Wrap a stage so its GPU footprint is bounded and released."""
    with gpu_session(label):
        yield


def _is_cuda_oom(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "out of memory" in text or "cuda_error_out_of_memory" in text
