"""llama.cpp-server vision backend (GGUF multimodal models).

Same call surface as TransformersVLM (`rank_images`, `describe_batch`)
so pipeline stages work unmodified. The server runs as a child process
for the duration of one acquisition: load() starts it, unload() stops
it. Exactly one VLM is therefore resident at any time.

GGUF weights + projector must be staged under HF_HOME/gguf (or be
fetchable from the Hub); a missing file is a loud, recorded load
failure, never a silent downgrade.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from ..config import ModelSpec
from ..gpu import BackendReport, nvidia_smi_gpus
from .base import BaseBackend

log = logging.getLogger("yt2wiki.llama_server")

_DEFAULT_TIMEOUT_S = 600


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _reasoning_args(variant: dict[str, Any]) -> list[str]:
    """Extra llama-server argv to disable hybrid thinking.

    YAML 1.1 parses `reasoning: off` as boolean False, so both forms are
    accepted. Without these flags a hybrid thinker (Qwen3.5) burns the whole
    generation budget on thought tokens: empty content, finish=length.
    """
    reasoning = (variant or {}).get("reasoning", "")
    if reasoning is False or str(reasoning).lower() in ("off", "false", "0", "no"):
        return ["--reasoning", "off", "--reasoning-budget", "0"]
    return []


def _device_env_for_child(index: int | None) -> dict[str, str]:
    """Pin the server child to the manager-chosen GPU.

    If the operator already restricted visibility, map the torch index
    back through the parent's own CUDA_VISIBLE_DEVICES list (which may
    hold indices or UUIDs); otherwise pass the index straight through.
    """
    env = dict(os.environ)
    parent = env.get("CUDA_VISIBLE_DEVICES")
    if parent:
        entries = [e.strip() for e in parent.split(",") if e.strip()]
        if index is not None and 0 <= index < len(entries):
            env["CUDA_VISIBLE_DEVICES"] = entries[index]
        # else: keep the operator's restriction untouched
    elif index is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(index)
    return env


def _server_vram_used_gb(index: int | None) -> float | None:
    for g in nvidia_smi_gpus():
        if g.index == index:
            return g.used_gb
    return None


class _ServerHandle:
    def __init__(self, proc: subprocess.Popen, url: str, log_path: Path):
        self.proc = proc
        self.url = url
        self.log_path = log_path


class LlamaServerVLM(BaseBackend):
    """VLM served by a local llama-server child process.

    Variant keys (passed verbatim from the model registry):
      gguf_file, mmproj_file, repo (default: spec.repo),
      ctx_size, ubatch, n_gpu_layers, reasoning (on|off),
      port (0 = auto), timeout_s, jpeg_quality, server_bin, server_dir.
    """

    def __init__(self, spec: ModelSpec, variant: dict[str, Any], device: str,
                 index: int | None, hf_home=None, max_image_tokens: int = 1024):
        super().__init__(spec, variant, device, index)
        self.hf_home = Path(hf_home) if hf_home else None
        self.handle: _ServerHandle | None = None
        # Per-image token budget, honoured the same way as the transformers
        # backend's max_pixels cap: thumbnails are scaled so each image costs
        # roughly max_image_tokens, keeping multi-image prompts inside ctx.
        self.max_image_tokens = max(64, int(max_image_tokens or 1024))

    # ------------------------------------------------------------- files
    def _resolve(self, filename: str) -> Path:
        base = (self.hf_home / "gguf") if self.hf_home else Path(tempfile.gettempdir())
        path = base / filename
        if path.exists():
            return path
        repo = self.variant.get("repo", self.spec.repo)
        try:
            from huggingface_hub import hf_hub_download
            got = hf_hub_download(repo, filename=filename, local_dir=str(base))
            return Path(got)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"llama-server model file missing: {filename} (repo {repo} "
                f"under {base}); stage it before running: {exc}") from exc

    def _server_binary(self) -> tuple[str, str]:
        """(executable, lib dir for LD_LIBRARY_PATH)."""
        explicit = self.variant.get("server_bin")
        if explicit and Path(explicit).exists():
            return str(explicit), str(Path(explicit).parent)
        env_dir = os.environ.get("LLAMA_SERVER_DIR")
        if env_dir and (Path(env_dir) / "llama-server").exists():
            return str(Path(env_dir) / "llama-server"), env_dir
        found = shutil.which("llama-server")
        if found:
            return found, str(Path(found).parent)
        raise RuntimeError(
            "llama-server binary not found: set variant server_bin, "
            "LLAMA_SERVER_DIR, or put llama-server on PATH")

    # ---------------------------------------------------------------- load
    def load(self) -> BackendReport:
        import torch  # noqa: F401  (device sanity only)

        started = time.time()
        gguf = self._resolve(str(self.variant.get("gguf_file", "")))
        mmproj = self._resolve(str(self.variant.get("mmproj_file", "")))
        binary, libdir = self._server_binary()
        port = int(self.variant.get("port", 0) or 0) or _free_port()
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        log_path = ((self.hf_home / f"llama-server-{self.spec.key}-{port}.log")
                    if self.hf_home else Path(tempfile.gettempdir()) / f"llama-server-{port}.log")
        vram_before = _server_vram_used_gb(self.index)
        env = _device_env_for_child(self.index)
        env["LD_LIBRARY_PATH"] = libdir + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        cmd = [binary, "-m", str(gguf), "--mmproj", str(mmproj),
               "-c", str(int(self.variant.get("ctx_size", 8192))),
               "-ub", str(int(self.variant.get("ubatch", 2048))),
               "--n-gpu-layers", str(int(self.variant.get("n_gpu_layers", 99))),
               "--host", "127.0.0.1", "--port", str(port)]
        cmd += _reasoning_args(self.variant)
        logf = open(log_path, "w")
        try:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env,
                                    cwd=str(Path(binary).parent))
        except Exception as exc:  # noqa: BLE001
            logf.close()
            raise RuntimeError(f"could not start llama-server: {exc}") from exc
        self.handle = _ServerHandle(proc, url, log_path)
        self.model = self.handle
        try:
            self._wait_ready(url, timeout_s=300)
        except Exception:
            self.unload()
            raise
        used = _server_vram_used_gb(self.index)
        extra = {"server_log": str(log_path)}
        if used is not None and vram_before is not None:
            extra["server_vram_gb"] = round(max(0.0, used - vram_before), 2)
            extra["server_vram_total_gb"] = round(used, 2)
        report = self._finish_load(started, extra)
        log.info("llamacpp %s resident on cuda:%s (%s GB), load %.1fs",
                 self.spec.key, self.index, extra.get("server_vram_total_gb"), report.load_seconds)
        return report

    def _wait_ready(self, url: str, timeout_s: float = 300) -> None:
        health = url.rsplit("/v1/chat/completions", 1)[0] + "/health"
        deadline = time.time() + timeout_s
        last: Exception | None = None
        while time.time() < deadline:
            if self.handle is not None and self.handle.proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited during load (see {self.handle.log_path})")
            try:
                with urllib.request.urlopen(health, timeout=5) as r:
                    if r.status == 200:
                        return
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(2)
        raise RuntimeError(f"llama-server not ready after {timeout_s}s: {last}")

    def unload(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.proc.terminate()
                handle.proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                try:
                    handle.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.model = None
        super().unload()

    # -------------------------------------------------------------- infer
    def _post(self, messages: list[dict[str, Any]], max_tokens: int,
              temperature: float, system: str | None) -> dict[str, Any]:
        assert self.handle is not None, "backend not loaded"
        if system and messages and messages[0].get("role") != "system":
            messages = [{"role": "system", "content": system}] + messages
        body = {"messages": messages, "max_tokens": max_tokens,
                "temperature": temperature}
        timeout = float(self.variant.get("timeout_s", _DEFAULT_TIMEOUT_S))
        t0 = time.time()
        req = urllib.request.Request(self.handle.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception as exc:  # noqa: BLE001
            log.warning("llama-server call failed: %s", exc)
            return {"content": "", "reasoning": "", "usage": {},
                    "error": str(exc)[:200], "dt": time.time() - t0}
        msg = (resp.get("choices") or [{}])[0].get("message", {})
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        return {"content": content.strip(),
                "reasoning": (msg.get("reasoning_content") or "")[:200],
                "usage": resp.get("usage", {}),
                "finish": (resp.get("choices") or [{}])[0].get("finish_reason"),
                "dt": round(time.time() - t0, 2)}

    @staticmethod
    def _fit(image: Image.Image, budget: int) -> Image.Image:
        """Scale an image to cost roughly `budget` vision tokens.

        Assumes ~1024 px per token (Qwen-style patch accounting). Mirrors
        the transformers backend's max_pixels cap so multi-image prompts
        stay inside the context window at any configured budget.
        """
        w, h = image.size
        if w <= 0 or h <= 0:
            return image
        scale = min(1.0, (max(64, budget) * 1024 / (w * h)) ** 0.5)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        if (nw, nh) == (w, h):
            return image
        return image.resize((nw, nh), Image.LANCZOS)

    @staticmethod
    def _b64(images: Sequence[Image.Image], quality: int,
             budget: int) -> list[str]:
        out = []
        for im in images:
            buf = io.BytesIO()
            LlamaServerVLM._fit(im.convert("RGB"), budget).save(
                buf, format="JPEG", quality=quality)
            out.append(base64.b64encode(buf.getvalue()).decode())
        return out

    def rank_images(self, images: Sequence[Image.Image], prompt: str, *,
                    max_new_tokens: int = 768, temperature: float = 0.05,
                    system: str | None = None) -> str:
        """Comparative ranking: all images in ONE request, JSON ranked list out.

        Falls back to an empty string on any failure so callers can degrade to
        independent scoring instead of failing the job.
        """
        if not self.loaded or not self.handle or not images:
            return ""
        try:
            quality = int(self.variant.get("jpeg_quality", 85))
        except (TypeError, ValueError):
            quality = 85
        parts: list[dict[str, Any]] = []
        for i, b in enumerate(self._b64(images, quality, self.max_image_tokens)):
            parts.append({"type": "text", "text": f"Image {i + 1}:"})
            parts.append({"type": "image_url",
                          "image_url": {"url": "data:image/jpeg;base64," + b}})
        parts.append({"type": "text", "text": prompt})
        try:
            rec = self._post([{"role": "user", "content": parts}],
                             max_new_tokens, temperature, system)
        except Exception:  # noqa: BLE001
            log.warning("rank_images failed; caller should use independent scoring",
                        exc_info=True)
            return ""
        return rec["content"]

    def describe_batch(self, images: Sequence[Image.Image], prompt: str, *,
                       max_new_tokens: int = 768, temperature: float = 0.05,
                       system: str | None = None,
                       batch_size: int = 4) -> list[str]:
        """One prompt per image; the server parallelises across its slots."""
        if not self.loaded:
            raise RuntimeError("backend not loaded")
        quality = int(self.variant.get("jpeg_quality", 85))
        out = []
        for im in images:
            b = self._b64([im], quality, self.max_image_tokens)[0]
            rec = self._post(
                [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + b}},
                    {"type": "text", "text": prompt}]}],
                max_new_tokens, temperature, system)
            out.append(rec["content"])
        return out
