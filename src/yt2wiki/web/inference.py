"""Inference endpoint settings: OpenAI-compatible providers and role assignment.

User configuration (`config/inference.json`) is kept apart from connection
test results (`config/inference_status.json`) so that re-testing never
rewrites what the user chose.  Secrets are never stored: a provider names
the environment variable that holds its API key.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

ROLES = (("writer", "Writing"), ("vision", "Image judging"))
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_ENV = re.compile(r"^YT2WIKI_[A-Z0-9_]{1,56}$")


class InferenceConfigError(ValueError):
    pass


def validate_api_key_env(value: str) -> str:
    key_env = str(value or "").strip()
    if key_env and not _ENV.match(key_env):
        raise InferenceConfigError("The API key setting must use a YT2WIKI_ environment-variable prefix.")
    return key_env


def _config_dir(paths) -> Path:
    return paths.root / "config"


def config_path(paths) -> Path:
    return _config_dir(paths) / "inference.json"


def status_path(paths) -> Path:
    return _config_dir(paths) / "inference_status.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load(paths) -> dict[str, Any]:
    cfg = _read_json(config_path(paths), {}) or {}
    cfg.setdefault("providers", {})
    cfg.setdefault("roles", {})
    return cfg


def load_status(paths) -> dict[str, Any]:
    return _read_json(status_path(paths), {}) or {}


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a submitted configuration or raise InferenceConfigError."""
    providers_in = payload.get("providers") or []
    if isinstance(providers_in, dict):
        providers_in = [{"name": k, **v} for k, v in providers_in.items()]
    providers: dict[str, dict[str, str]] = {}
    for p in providers_in:
        name = str(p.get("name") or "").strip().lower()
        url = str(p.get("url") or "").strip().rstrip("/")
        model = str(p.get("model") or "").strip()
        key_env = validate_api_key_env(p.get("api_key_env"))
        if not _NAME.match(name):
            raise InferenceConfigError(
                "Endpoint names use lowercase letters, digits, dots, dashes or underscores.")
        if name in providers:
            raise InferenceConfigError(f"Two endpoints are both called “{name}”.")
        if not re.match(r"^https?://[^\s/]+", url):
            raise InferenceConfigError(f"“{name}” needs an http:// or https:// base URL.")
        providers[name] = {"url": url, "model": model[:200], "api_key_env": key_env}
    roles = {}
    for role, _label in ROLES:
        target = str((payload.get("roles") or {}).get(role) or "").strip().lower()
        if target and target not in providers:
            raise InferenceConfigError(f"{_label} is assigned to an endpoint that is not listed.")
        roles[role] = target
    return {"providers": providers, "roles": roles}


def save(paths, payload: dict[str, Any]) -> dict[str, Any]:
    cfg = validate(payload)
    cfg["updated_at"] = time.time()
    _write_json(config_path(paths), cfg)
    status = load_status(paths)
    pruned = {k: v for k, v in status.items() if k in cfg["providers"]}
    if pruned != status:
        _write_json(status_path(paths), pruned)
    return cfg


def read_payload_file(paths, path: str | Path) -> dict[str, Any]:
    """Read an operator-supplied inference payload file for the console CLI.

    The path must resolve inside the library's config directory (symlinks are
    resolved, so `../` and link escapes are refused) and be a regular file —
    never a directory or device. The content must parse as a JSON object.
    Failures raise InferenceConfigError so the CLI can report them without a
    traceback. Keep payloads next to the file they produce, e.g.
    `<data>/config/inference-payload.json`.
    """
    config_dir = (Path(paths.root) / "config").resolve()
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError as exc:
        raise InferenceConfigError(f"cannot resolve payload file: {exc}") from exc
    if config_dir not in resolved.parents or not resolved.is_file():
        raise InferenceConfigError(
            f"payload file must be a regular file inside {config_dir}: {path}")
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InferenceConfigError(f"cannot read payload file: {exc}") from exc
    if not isinstance(data, dict):
        raise InferenceConfigError("payload must be a JSON object with providers/roles")
    return data


def test_endpoint(url: str, api_key_env: str = "", *, timeout: float = 6.0,
                model: str = "") -> dict[str, Any]:
    """Reachability + capability qualification.

    First GET <url>/models for reachability and model list, then attempt
    real generation: a trivial JSON writer probe and, if the endpoint
    accepts images, a 2-image comparative judge probe. Basic HTTP
    connectivity is not qualification — an endpoint may be reachable but
    not qualified for a role.
    """
    import httpx

    url = url.rstrip("/")
    headers = {}
    if api_key_env:
        key = os.environ.get(api_key_env, "")
        if not key:
            return {"ok": False, "error": f"Environment variable {api_key_env} is not set.",
                    "checked_at": time.time(), "url": url}
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.time()
    try:
        r = httpx.get(f"{url}/models", headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": _short_error(exc), "checked_at": time.time(), "url": url,
                "qualified_for_writer": False, "qualified_for_vision": False}
    latency = round((time.time() - t0) * 1000)
    if r.status_code in (401, 403):
        return {"ok": False, "error": "The endpoint refused the API key.",
                "status": r.status_code, "checked_at": time.time(), "url": url,
                "qualified_for_writer": False, "qualified_for_vision": False}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code} from {url}/models",
                "status": r.status_code, "checked_at": time.time(), "url": url,
                "qualified_for_writer": False, "qualified_for_vision": False}
    try:
        data = r.json()
        models = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
    except (ValueError, AttributeError):
        return {"ok": False, "error": "The response was not an OpenAI-compatible model list.",
                "checked_at": time.time(), "url": url,
                "qualified_for_writer": False, "qualified_for_vision": False}
    base = {"ok": True, "models": models[:20], "latency_ms": latency,
            "checked_at": time.time(), "url": url,
            "qualified_for_writer": False, "qualified_for_vision": False}
    # --- writer qualification: real JSON generation ---
    try:
        writer_ok = _qualify_writer(url, headers, model or (models[0] if models else ""), timeout)
        base["qualified_for_writer"] = bool(writer_ok)
        base["writer_probe"] = writer_ok
    except Exception as exc:  # noqa: BLE001
        base["writer_probe_error"] = _short_error(exc)
    # --- vision qualification: genuine multi-image judging ---
    try:
        vision_ok = _qualify_vision(url, headers, model or (models[0] if models else ""), timeout)
        base["qualified_for_vision"] = bool(vision_ok)
        base["vision_probe"] = vision_ok
    except Exception as exc:  # noqa: BLE001
        base["vision_probe_error"] = _short_error(exc)
    return base


def _qualify_writer(url: str, headers: dict[str, str], model: str, timeout: float) -> bool:
    import httpx, json as _json
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "Return JSON only: {\"ok\": true, \"text\": \"hello\"}"}],
        "max_tokens": 60,
        "temperature": 0.0,
    }
    if model:
        payload["model"] = model
    # Ask for JSON explicitly when possible
    payload["response_format"] = {"type": "json_object"}
    try:
        r = httpx.post(f"{url}/chat/completions", json=payload, headers=headers, timeout=timeout + 10)
        if r.status_code != 200:
            return False
        data = r.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text","") for p in content if isinstance(p, dict))
        obj = _json.loads(content) if content.strip().startswith("{") else None
        if obj is None:
            # Try to extract json from prose
            import re
            m = re.search(r"\{.*ok.*\}", content, re.S)
            if m:
                obj = _json.loads(m.group(0))
        return isinstance(obj, dict) and obj.get("ok") is True
    except Exception:
        return False


def _qualify_vision(url: str, headers: dict[str, str], model: str, timeout: float) -> bool:
    """Genuine V9 rubric probe: 2 images + full editorial judge prompt.

    Uses the frozen RUBRIC_PROMPT with a realistic section (2-image, 1 current)
    and validates via the same _parse_rubric the publisher uses. This catches
    endpoints that are reachable but cannot produce the required structured
    output (e.g. Gemma 4 26B's 68% invalid rate observed in V9-Z2).
    """
    import httpx, base64, io, json as _json
    from PIL import Image
    try:
        from ..v9.judge import RUBRIC_PROMPT, _parse_rubric
    except Exception:
        return False
    # Two distinguishable frames; content must be real pixels
    ims = [Image.new("RGB", (64, 64), (255, 220, 220)), Image.new("RGB", (64, 64), (220, 220, 255))]
    # Add a tiny label so the model has something to ground
    def b64(im):
        buf = io.BytesIO()
        # Draw a simple marker
        try:
            from PIL import ImageDraw
            d = ImageDraw.Draw(im)
            d.text((4, 4), "V9", fill=(0, 0, 0))
        except Exception:
            pass
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    # Build the same prompt the publisher sends, with 2 images
    sec = {"heading": "Battery and Repair Considerations",
           "body": "Keep the battery below 25% before prying. Use heat to soften adhesive, pry from the bottom edge. The battery is L-shaped, 40 Wh, 5313 mAh.", "t_start": 360.0, "t_end": 400.0}
    prompt = RUBRIC_PROMPT.format(heading=sec["heading"][:150], body=sec["body"][:1200], n=2, current_pos=1)
    content: list[dict[str, Any]] = []
    for i, im in enumerate(ims):
        content.append({"type": "text", "text": f"Image {i+1}:"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64(im)}})
    content.append({"type": "text", "text": prompt})
    payload: dict[str, Any] = {
        "messages": [{"role": "system", "content": "You judge images for technical documentation. Reply JSON only."},
                     {"role": "user", "content": content}],
        "max_tokens": 1100,
        "temperature": 0.0,
    }
    if model:
        payload["model"] = model
    # Try with json_object first; V9-Z2 showed guided_json is ignored for 26B, so raw is the real test
    for attempt in range(2):
        if attempt == 1:
            # Second try: no response_format, plain
            payload.pop("response_format", None)
        else:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post(f"{url}/chat/completions", json=payload, headers=headers, timeout=timeout + 30)
            if r.status_code != 200:
                continue
            data = r.json()
            raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if isinstance(raw, list):
                raw = " ".join(p.get("text","") for p in raw if isinstance(p, dict))
            parsed = _parse_rubric(raw, 2)
            if parsed is not None and parsed.get("scores") and parsed.get("decision") in ("KEEP","REPLACE","EXPAND","USE_PAIR","OMIT"):
                return True
        except Exception:
            continue
    return False


def record_test(paths, name: str, result: dict[str, Any]) -> None:
    status = load_status(paths)
    status[name] = result
    _write_json(status_path(paths), status)


def _short_error(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    if "Connection refused" in text or "ConnectError" in type(exc).__name__:
        return "Could not connect — is the server running and reachable from here?"
    if "timed out" in text.lower() or "Timeout" in type(exc).__name__:
        return "The endpoint did not answer in time."
    return text[:200]
