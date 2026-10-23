"""Runtime configuration: hardware profile, model assignments, tuning knobs.

Every knob that influences pipeline output participates in a *fingerprint* so
that cached intermediates can never be reused across a model change.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import Paths

PROFILE_NAMES = ("compact", "standard", "enhanced")
DEFAULT_PROFILE = "standard"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    key: str
    role: str
    engine: str
    repo: str
    revision: str = "main"
    license: str = "unknown"
    params_b: float = 0.0
    provider: str = ""
    notes: str = ""
    vram_gb: dict[str, float] = field(default_factory=dict)
    fallbacks: list[str] = field(default_factory=list)
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)

    def variant(self, profile: str) -> dict[str, Any]:
        raw = self.variants.get(profile)
        return dict(raw) if raw else {}

    @classmethod
    def from_dict(cls, key: str, d: dict[str, Any]) -> "ModelSpec":
        known = {
            "role",
            "engine",
            "repo",
            "revision",
            "license",
            "params_b",
            "provider",
            "notes",
            "vram_gb",
            "fallbacks",
            "variants",
        }
        unknown = set(d) - known
        if unknown:
            raise ConfigError(f"model {key}: unknown keys {sorted(unknown)}")
        return cls(key=key, **{k: d[k] for k in known if k in d})


@dataclass
class Tuning:
    """Behavioural knobs that change output and therefore invalidate caches."""

    # --- audio -----------------------------------------------------------
    asr_language: str = "en"
    asr_word_timestamps: bool = True
    asr_vad_filter: bool = True
    asr_condition_on_previous_text: bool = True

    # --- visual candidate generation ------------------------------------
    scene_threshold: float = 0.30
    periodic_interval_s: float = 4.0
    frame_min_gap_s: float = 1.2
    keyframe_budget: int = 96
    screen_change_detection: bool = True
    transcript_anchors: bool = True
    keyframe_batch: int = 8
    frame_width: int = 1280
    frame_quality: int = 2

    # --- visual interpretation ------------------------------------------
    # vision_image_tokens caps the pixel budget handed to the VLM.  A 1280x720
    # frame becomes roughly 384-640 vision tokens, which is the real cost
    # driver for both prefill and attention memory.
    vision_image_tokens: int = 512
    # vision_max_new_tokens bounds the generated JSON.  The frame schema needs
    # far fewer tokens than a free-form caption.
    vision_max_new_tokens: int = 448
    vision_batch: int = 4
    vision_temperature: float = 0.05
    vision_dense_text_pass: bool = True

    # --- evidence / composition -----------------------------------------
    evidence_use_frames: bool = True
    text_max_tokens: int = 3072
    text_temperature: float = 0.15
    verify_max_tokens: int = 1536
    transcription_chunk_s: int = 220
    text_window_s: float = 90.0
    procedure_digest_limit: int = 110
    evidence_batch: int = 40

    # --- verification ----------------------------------------------------
    enable_llm_verification: bool = True
    enable_rule_verification: bool = True
    enable_contradiction_pass: bool = True

    def fingerprint_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Settings:
    profile: str = DEFAULT_PROFILE
    paths: Paths = field(default_factory=lambda: Paths.resolve())
    device_index: int | None = None
    tuning: Tuning = field(default_factory=Tuning)
    model_overrides: dict[str, str] = field(default_factory=dict)
    registry_path: Path | None = None
    deterministic_seed: int = 1234
    offline_models: bool = False

    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        *,
        data_dir: str | Path | None = None,
        profile: str | None = None,
        device_index: int | None = None,
        registry: str | Path | None = None,
        **overrides: Any,
    ) -> "Settings":
        paths = Paths.resolve(data_dir).ensure()
        profile = profile or os.environ.get("YT2WIKI_PROFILE") or DEFAULT_PROFILE
        if profile not in PROFILE_NAMES:
            raise ConfigError(f"unknown profile {profile!r}; expected one of {PROFILE_NAMES}")
        reg_path = Path(registry) if registry else _find_registry()
        tuning = Tuning()
        settings = cls(
            profile=profile,
            paths=paths,
            device_index=device_index if device_index is not None else _env_int("YT2WIKI_CUDA_DEVICE"),
            registry_path=reg_path,
            tuning=tuning,
        )
        settings._apply_profile_tuning()
        settings._apply_overrides(overrides)
        # Validate the registry resolves for this profile.
        settings.assignments()
        return settings

    def _apply_profile_tuning(self) -> None:
        """Pull the per-profile knobs (vision batching, image budget) from the registry."""
        reg = _registry(self)
        prof = (reg.get("profiles") or {}).get(self.profile) or {}
        for key in ("vision_batch", "vision_image_tokens", "vision_max_new_tokens"):
            value = prof.get(key)
            if value is not None and hasattr(self.tuning, key):
                setattr(self.tuning, key, value)

    def _apply_overrides(self, overrides: dict[str, Any]) -> None:
        for key, value in overrides.items():
            if value is None:
                continue
            if key in ("profile", "device_index", "deterministic_seed"):
                setattr(self, key, value)
            elif key == "model_overrides":
                self.model_overrides.update(value)
            elif hasattr(self.tuning, key):
                setattr(self.tuning, key, value)
            elif key in ("vision_batch", "vision_image_tokens", "vision_max_new_tokens"):
                setattr(self.tuning, key, int(value))
            else:
                raise ConfigError(f"unknown setting {key!r}")

    # ------------------------------------------------------------------
    @property
    def hf_home(self) -> Path:
        env = os.environ.get("HF_HOME")
        if env:
            return Path(env)
        return self.paths.hf_home

    def vram_budget_gb(self) -> float:
        return float(_registry(self)["profiles"][self.profile]["vram_budget_gb"])

    def assignments(self) -> dict[str, str]:
        reg = _registry(self)
        assign = dict(reg["assignments"][self.profile])
        for role, value in self.model_overrides.items():
            if role not in assign:
                raise ConfigError(f"cannot override unknown role {role!r}")
            target = _resolve_alias(reg, value)
            spec = reg["models"].get(target)
            if spec is None:
                raise ConfigError(f"unknown model {value!r}")
            if spec.role != role:
                raise ConfigError(f"model {value!r} has role {spec.role!r}, expected {role!r}")
            assign[role] = target
        return assign

    def model(self, role: str) -> ModelSpec:
        reg = _registry(self)
        return reg["models"][self.assignments()[role]]

    def model_with_fallbacks(self, role: str) -> list[ModelSpec]:
        """Requested model plus its recorded fallback chain."""
        reg = _registry(self)
        out: list[ModelSpec] = []
        seen: set[str] = set()
        key = self.assignments()[role]
        while key and key not in seen:
            seen.add(key)
            spec = reg["models"][key]
            out.append(spec)
            key = spec.fallbacks[0] if spec.fallbacks else ""
        return out

    def asr_identity(self) -> dict[str, str]:
        """Effective ASR identity: the server that will actually transcribe.

        Registry `asr-http` variants carry only `url_env`/`model_env` names,
        so the raw variant cannot fingerprint the server. Resolve with the
        same precedence the loader uses: an assigned inference provider wins,
        otherwise the environment-resolved registry variant.
        """
        try:
            from .web import inference as _inf
            cfg = _inf.load(self.paths)
            name = (cfg.get("roles") or {}).get("asr")
            prov = (cfg.get("providers") or {}).get(name or "", {})
            if name and prov.get("url"):
                return {"url": str(prov["url"]).rstrip("/"),
                        "model": str(prov.get("model") or "")}
        except Exception:  # noqa: BLE001 - fall through to the registry default
            pass
        variant = self.model("asr").variant(self.profile)
        return {
            "url": (os.environ.get(str(variant.get("url_env") or "YT2WIKI_ASR_URL"), "")
                    or "").rstrip("/"),
            "model": os.environ.get(str(variant.get("model_env") or "YT2WIKI_ASR_MODEL"), ""),
        }

    # ------------------------------------------------------------------
    def fingerprint(self) -> str:
        """Stable hash of everything that can change pipeline output."""
        payload = {
            "profile": self.profile,
            "models": {role: spec.repo + "@" + spec.revision for role, spec in
                       ((r, self.model(r)) for r in ("asr", "vision", "text", "verify"))},
            "variants": {role: self.model(role).variant(self.profile) for role in
                         ("asr", "vision", "text", "verify")},
            # The asr-http variant holds env names, not values: hash the
            # resolved server too, so switching ASR servers retranscribes.
            "asr_identity": self.asr_identity(),
            "tuning": self.tuning.fingerprint_dict(),
            "registry_version": _registry(self).get("schema_version", 0),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def describe(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "vram_budget_gb": self.vram_budget_gb(),
            "fingerprint": self.fingerprint(),
            "models": {
                role: {
                    "repo": self.model(role).repo,
                    "revision": self.model(role).revision,
                    "variant": self.model(role).variant(self.profile),
                    "fallbacks": self.model(role).fallbacks,
                }
                for role in ("asr", "vision", "text", "verify")
            },
        }


# ----------------------------------------------------------------------
# registry loading / caching


def _find_registry() -> Path:
    env = os.environ.get("YT2WIKI_MODELS")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for base in [Path.cwd(), *here.parents]:
        candidate = base / "config" / "models.yaml"
        if candidate.is_file():
            return candidate
    return Path("/etc/yt2wiki/models.yaml")


def _resolve_alias(reg: dict[str, Any], value: str) -> str:
    aliases = reg.get("aliases") or {}
    return aliases.get(value, value)


_REGISTRY_CACHE: dict[str, dict[str, Any]] = {}


def _registry(settings: Settings) -> dict[str, Any]:
    path = str(settings.registry_path or _find_registry())
    if path in _REGISTRY_CACHE:
        return _REGISTRY_CACHE[path]
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"model registry not found at {p}")
    raw = yaml.safe_load(p.read_text())
    models = {k: ModelSpec.from_dict(k, v) for k, v in (raw.get("models") or {}).items()}
    for key, spec in models.items():
        for fb in spec.fallbacks:
            if fb not in models:
                raise ConfigError(f"model {key} references unknown fallback {fb}")
    roles = {"asr", "vision", "text", "verify"}
    missing = roles - {m.role for m in models.values()}
    if missing:
        raise ConfigError(f"registry does not define models for roles {sorted(missing)}")
    reg = dict(raw)
    reg["models"] = models
    reg["_path"] = path
    _REGISTRY_CACHE[path] = reg
    return reg


def load_registry(path: str | Path | None = None) -> dict[str, Any]:
    """Public accessor for the CLI and the web UI."""
    dummy = Settings(registry_path=Path(path) if path else None)
    return _registry(dummy)


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None
