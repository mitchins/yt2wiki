"""Release packaging tests: pull-only Compose, GHCR image, OCI metadata."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose(name: str) -> dict:
    with open(ROOT / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_compose_is_pull_only_ghcr():
    cfg = _compose("docker-compose.yml")
    svc = cfg["services"]["yt2wiki"]
    assert "build" not in svc, "ordinary installs must pull, never build"
    assert svc["image"].startswith("ghcr.io/mitchins/yt2wiki:")
    # Pinning path stays available through the tag variable.
    assert "YT2WIKI_IMAGE_TAG" in svc["image"]
    # The ':' separator lives in the reference, so values carry no leading
    # colon (YT2WIKI_IMAGE_TAG=:sha-x would compose an invalid '::' image).
    assert "::" not in svc["image"]
    for blob_name in ("docker-compose.yml", "docker-compose.portainer.yml",
                      "docs/releases.md"):
        blob = (ROOT / blob_name).read_text(encoding="utf-8")
        assert "YT2WIKI_IMAGE_TAG=:" not in blob
        assert "TAG=:sha" not in blob and "TAG=:v" not in blob
    # Existing operator surface is preserved.
    assert svc["ports"] == ["127.0.0.1:8080:8000"]
    assert "yt2wiki-data:/data" in svc["volumes"]
    env = svc["environment"]
    assert "- YT2WIKI_HOME=/data" in env or "YT2WIKI_HOME=/data" in env
    assert any("YT2WIKI_IMAGE_DIGEST" in e for e in env)
    assert svc.get("healthcheck", {}).get("test") is not None
    assert svc.get("restart") == "unless-stopped"
    # Inference sidecars keep their profiles; nothing is socket-managed.
    assert _compose("docker-compose.yml")["services"]["asr"]["profiles"] == ["asr"]
    assert _compose("docker-compose.yml")["services"]["llama"]["profiles"] == ["llama"]
    blob = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "docker.sock" not in blob
    assert "/var/run/docker" not in blob


def test_compose_build_override_is_developer_only():
    cfg = _compose("docker-compose.build.yml")
    svc = cfg["services"]["yt2wiki"]
    assert svc["build"]["context"] == "."
    assert svc["image"] == "yt2wiki:local"
    # The override must not touch volumes, ports or environment.
    assert set(svc) == {"build", "image"}


def test_portainer_uses_same_ghcr_image():
    main = _compose("docker-compose.yml")["services"]["yt2wiki"]
    port = _compose("docker-compose.portainer.yml")["services"]["yt2wiki"]
    assert "build" not in port
    assert port["image"].startswith("ghcr.io/mitchins/yt2wiki:")
    assert port["ports"] == main["ports"]
    assert port["volumes"] == main["volumes"]
    assert port["healthcheck"]["test"] == main["healthcheck"]["test"]
    assert port["restart"] == "unless-stopped"
    blob = (ROOT / "docker-compose.portainer.yml").read_text(encoding="utf-8")
    assert "docker build" not in blob and "local build required" not in blob


def test_dockerfile_release_metadata():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # Pinned multi-arch-capable base (digest is an OCI image index).
    assert "python:3.12-slim-bookworm@sha256:" in text
    # Build commit injection (the .git directory is excluded from context).
    assert "ARG GIT_COMMIT" in text
    assert "/app/BUILD" in text
    # OCI source/version/revision labels.
    for key in ("org.opencontainers.image.source",
                "org.opencontainers.image.revision",
                "org.opencontainers.image.version"):
        assert key in text
    assert "github.com/mitchins/yt2wiki" in text
    # Thin publisher: no inference runtimes installed.
    for mod in ("torch", "transformers", "faster-whisper", "ctranslate2"):
        assert mod not in text
    assert "USER yt2wiki" in text


def test_ci_gates_and_pins_docker_publish():
    import re

    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    # All Docker actions pinned to immutable SHAs (repo policy).
    for action in ("docker/setup-buildx-action", "docker/setup-qemu-action",
                   "docker/login-action", "docker/build-push-action"):
        m = re.search(rf"uses:\s*{re.escape(action)}@([0-9a-f]{{40}})", text)
        assert m, f"{action} must be SHA-pinned"
    # Publishing only after tests + Sonar, on main or version tags.
    assert "needs: [test, sonar]" in text
    assert "needs.sonar.result == 'success'" in text
    assert "ghcr.io/mitchins/yt2wiki" in text
    assert "push-by-digest=true" in text
    assert "imagetools create" in text
    assert ":latest" not in text
    assert "ubuntu-24.04-arm" in text  # native ARM64, not emulation-only
    # ARMv7 attempt exists but can never silently join the manifest: digest
    # artifacts only exist for platforms that built successfully.
    assert "linux/arm/v7" in text


def test_readme_is_sixty_seconds():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    prose = " ".join(w for w in text.split("\n")
                     if w.strip() and not w.strip().startswith(("```", "git clone",
                                                               "cd ", "docker ")))
    assert len(prose.split()) <= 200, f"README prose too long: {len(prose.split())}"
    for token in ("docker compose pull", "docker compose up -d",
                  "http://localhost:8080", "docs/deploy.md"):
        assert token in text
    for banned in ("feature matrix", "benchmark", "roadmap", "Architecture",
                   "faster-whisper", ":0.1.0"):
        assert banned not in text
