"""Model acquisition: role -> backend, with recorded fallbacks."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Sequence

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

    def _remote_spec(self, role: str) -> tuple[ModelSpec | None, dict[str, Any] | None]:
        """Return (synthetic spec, provider) when this role is assigned to a remote endpoint.

        When a job-captured snapshot is present (web jobs), it is authoritative
        and later live settings changes do not rewrite history. Otherwise the
        live inference config is consulted; CLI jobs without a config simply
        return None and fall through to the local registry.
        """
        try:
            if self.inference_snapshot is not None:
                entry = self.inference_snapshot.get(role)
                if not entry:
                    return None, None
                # Snapshot is already the effective identity at submission time
                if not entry.get("reachable"):
                    raise RuntimeError(
                        f"inference provider '{entry.get('provider')}' for role '{role}' "
                        f"was not reachable at submission time.")
                if role == "vision" and not entry.get("qualified_for_vision"):
                    raise RuntimeError(
                        f"provider '{entry.get('provider')}' was not qualified for vision "
                        f"at submission time.")
                if role == "writer" and not entry.get("qualified_for_writer"):
                    # Writer may be reachable but not yet qualified — fail rather than fall back
                    raise RuntimeError(
                        f"provider '{entry.get('provider')}' was not qualified for writing "
                        f"at submission time.")
                provider = {"url": entry.get("url"), "model": entry.get("model") or "",
                            "api_key_env": entry.get("api_key_env") or ""}
                spec = ModelSpec(
                    key=f"remote-{entry.get('provider')}",
                    role=role,
                    engine="remote-openai",
                    repo=provider.get("model") or entry.get("url", ""),
                    revision="remote",
                    provider=entry.get("provider") or "",
                    notes=f"Remote {provider['url']} via {entry.get('provider')}",
                )
                variant = {"model": provider.get("model") or "", "url": provider["url"],
                           "api_key_env": provider.get("api_key_env") or ""}
                return spec, provider
            from ..web import inference as _inf
            cfg = _inf.load(self.settings.paths)
            provider_name = (cfg.get("roles") or {}).get(role)
            if not provider_name:
                return None, None
            provider = (cfg.get("providers") or {}).get(provider_name)
            if not provider:
                return None, None
            status = _inf.load_status(self.settings.paths).get(provider_name, {})
            if not status.get("ok"):
                raise RuntimeError(
                    f"inference provider '{provider_name}' for role '{role}' is not reachable "
                    f"(test it in Settings). Status: {status.get('error') or 'not tested'}")
            if role == "vision" and not status.get("qualified_for_vision"):
                raise RuntimeError(
                    f"provider '{provider_name}' is not qualified for vision "
                    f"(needs genuine multi-image judging). Qualify it in Settings.")
            if role == "writer" and not status.get("qualified_for_writer"):
                raise RuntimeError(
                    f"provider '{provider_name}' is not qualified for writing "
                    f"(needs real JSON generation). Qualify it in Settings.")
            spec = ModelSpec(
                key=f"remote-{provider_name}",
                role=role,
                engine="remote-openai",
                repo=provider.get("model") or status.get("models", [""])[0] if status.get("models") else provider.get("url", ""),
                revision="remote",
                provider=provider_name,
                notes=f"Remote {provider['url']} via {provider_name}",
            )
            variant = {"model": provider.get("model") or "", "url": provider["url"],
                       "api_key_env": provider.get("api_key_env") or ""}
            return spec, provider
        except RuntimeError:
            raise
        except Exception:
            return None, None

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
        if spec.engine == "llamacpp-server":
            return LlamaServerVLM(spec, variant, self.device, self.index,
                                  self.settings.hf_home,
                                  max_image_tokens=self.settings.tuning.vision_image_tokens)
        if spec.engine == "transformers":
            if role == "vision":
                return TransformersVLM(
                    spec, variant, self.device, self.index, self.settings.hf_home,
                    max_image_tokens=self.settings.tuning.vision_image_tokens,
                )
            return TransformersLLM(spec, variant, self.device, self.index, self.settings.hf_home)
        raise RuntimeError(f"no builder for engine {spec.engine!r}")

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
                try:
                    backend.unload()
                finally:
                    free_cuda_memory()
            return
        chain = self.settings.model_with_fallbacks(role)
        backend: BaseBackend | None = None
        last_error: Exception | None = None
        for i, spec in enumerate(chain):
            fallback_of = chain[0].key if i > 0 else ""
            reset_peak()
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
                        raise
                    excluded.append(info.device_index)
                    self._record(
                        role=role, model_key=spec.key, repo=spec.repo, ok=False,
                        device=f"cuda:{info.device_index}", error=f"OOM on this card; {exc}"[:600],
                        fallback_of=fallback_of,
                    )
                    log.warning("cuda OOM loading %s on device %s; re-planning",
                                spec.key, info.device_index)
                    free_cuda_memory()
                    candidate.unload()
                    continue
            else:  # pragma: no cover - defensive
                raise RuntimeError(f"could not allocate {spec.key}")
            if backend is not None:
                self._record(
                    role=role,
                    model_key=spec.key,
                    repo=spec.repo,
                    revision=spec.revision,
                    engine=spec.engine,
                    variant=candidate.report.variant,
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
            try:
                backend.unload()
            finally:
                free_cuda_memory()

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


@contextmanager
def stage_gpu(label: str) -> Iterator[None]:
    """Wrap a stage so its GPU footprint is bounded and released."""
    with gpu_session(label):
        yield


def _is_cuda_oom(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "out of memory" in text or "cuda_error_out_of_memory" in text
