"""Deterministic tests for the CI tag/promotion shell scripts.

No network, no docker daemon: a stub DOCKER binary emulates the registry
so tag-computation, immutability and version-promotion conditions are
exercised exactly as the workflow invokes them.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "ci"

# A minimal imagetools-inspect payload with both release platforms.
MANIFEST_BODY = """Name:      ghcr.io/mitchins/yt2wiki:sha-abc1234
MediaType: application/vnd.oci.image.index.v1+json
Digest:    sha256:0000000000000000000000000000000000000000000000000000000000000000

Manifests:
  Name:   ghcr.io/mitchins/yt2wiki@sha256:1111
  Platform: linux/amd64

  Name:   ghcr.io/mitchins/yt2wiki@sha256:2222
  Platform: linux/arm64
"""


def _write_stub_docker(registry: Path) -> str:
    """Stub docker: registry state lives in files named tag -> content."""
    stub = registry / "docker-stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'REG="${FAKE_REGISTRY:?}"\n'
        'sub="$1"; shift          # buildx\n'
        'sub2="$1"; shift         # imagetools\n'
        'op="$1"; shift           # inspect | create\n'
        'case "$op" in\n'
        "  inspect)\n"
        '    ref="$1"\n'
        '    name="${ref##*/}"; name="${name//:/__}"\n'
        '    if [[ -f "$REG/$name" ]]; then cat "$REG/$name"; exit 0; fi\n'
        '    echo "no such tag: $ref" >&2\n'
        "    exit 1 ;;\n"
        "  create)\n"
        '    dest=""; src=""\n'
        '    while [[ $# -gt 0 ]]; do\n'
        '      case "$1" in\n'
        '        -t) dest="$2"; shift 2 ;;\n'
        '        *) src="$1"; shift ;;\n'
        "      esac\n"
        "    done\n"
        '    [[ -n "$dest" && -n "$src" ]] || { echo "bad create args" >&2; exit 2; }\n'
        '    sname="${src##*/}"; sname="${sname//:/__}"\n'
        '    dname="${dest##*/}"; dname="${dname//:/__}"\n'
        '    [[ -f "$REG/$sname" ]] || { echo "source missing: $src" >&2; exit 1; }\n'
        '    cp "$REG/$sname" "$REG/$dname"\n'
        '    echo "created"\n'
        "    exit 0 ;;\n"
        "esac\n"
        'echo "unsupported invocation" >&2\n'
        "exit 2\n",
        encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return str(stub)


def _run(script: Path, args: list[str], registry: Path, docker: str):
    env = dict(os.environ, DOCKER=docker, FAKE_REGISTRY=str(registry))
    return subprocess.run(["bash", str(script), *args], env=env,
                          capture_output=True, text=True)


def _pub(registry: Path, tag: str, body: str = MANIFEST_BODY) -> None:
    (registry / f"yt2wiki__{tag}").write_text(body, encoding="utf-8")


def test_compute_tags_main_pushes_sha_and_main():
    out = subprocess.run(
        ["bash", str(SCRIPTS / "compute-tags.sh"), "refs/heads/main", "abc1234def5678"],
        capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [
        "ghcr.io/mitchins/yt2wiki:sha-abc1234",
        "ghcr.io/mitchins/yt2wiki:main"]


def test_compute_tags_version_never_moves_main():
    out = subprocess.run(
        ["bash", str(SCRIPTS / "compute-tags.sh"), "refs/tags/v0.1.0", "abc1234def5678"],
        capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [
        "ghcr.io/mitchins/yt2wiki:sha-abc1234",
        "ghcr.io/mitchins/yt2wiki:v0.1.0"]
    assert "main" not in out


def test_check_immutable_fails_when_tag_exists(tmp_path):
    registry = tmp_path / "reg"
    registry.mkdir()
    _pub(registry, "sha-abc1234")
    r = _run(SCRIPTS / "check-immutable.sh",
             ["ghcr.io/mitchins/yt2wiki:sha-abc1234"], registry,
             _write_stub_docker(registry))
    assert r.returncode == 1
    assert "Refusing to move immutable tag" in r.stderr


def test_check_immutable_passes_when_tag_free(tmp_path):
    registry = tmp_path / "reg"
    registry.mkdir()
    r = _run(SCRIPTS / "check-immutable.sh",
             ["ghcr.io/mitchins/yt2wiki:sha-abc1234"], registry,
             _write_stub_docker(registry))
    assert r.returncode == 0
    assert "free to publish" in r.stdout


def test_promote_refuses_existing_version_tag(tmp_path):
    registry = tmp_path / "reg"
    registry.mkdir()
    _pub(registry, "sha-abc1234")
    _pub(registry, "v0.1.0")
    r = _run(SCRIPTS / "promote-version.sh",
             ["ghcr.io/mitchins/yt2wiki:sha-abc1234",
              "ghcr.io/mitchins/yt2wiki:v0.1.0"], registry,
             _write_stub_docker(registry))
    assert r.returncode == 1
    assert "Refusing to overwrite existing tag" in r.stderr
    assert "immutable" in r.stderr


def test_promote_refuses_unqualified_commit(tmp_path):
    registry = tmp_path / "reg"
    registry.mkdir()
    r = _run(SCRIPTS / "promote-version.sh",
             ["ghcr.io/mitchins/yt2wiki:sha-abc1234",
              "ghcr.io/mitchins/yt2wiki:v0.1.0"], registry,
             _write_stub_docker(registry))
    assert r.returncode == 1
    assert "no qualified image to promote" in r.stderr


def test_promote_refuses_single_platform_manifest(tmp_path):
    registry = tmp_path / "reg"
    registry.mkdir()
    _pub(registry, "sha-abc1234",
         MANIFEST_BODY.replace("  Name:   ghcr.io/mitchins/yt2wiki@sha256:2222\n"
                               "  Platform: linux/arm64\n", ""))
    r = _run(SCRIPTS / "promote-version.sh",
             ["ghcr.io/mitchins/yt2wiki:sha-abc1234",
              "ghcr.io/mitchins/yt2wiki:v0.1.0"], registry,
             _write_stub_docker(registry))
    assert r.returncode == 1
    assert "linux/arm64 member" in r.stderr
    assert not (registry / "yt2wiki__v0.1.0").exists()


def test_promote_relables_qualified_manifest(tmp_path):
    registry = tmp_path / "reg"
    registry.mkdir()
    _pub(registry, "sha-abc1234")
    report = tmp_path / "report.txt"
    r = _run(SCRIPTS / "promote-version.sh",
             ["ghcr.io/mitchins/yt2wiki:sha-abc1234",
              "ghcr.io/mitchins/yt2wiki:v0.1.0", str(report)], registry,
             _write_stub_docker(registry))
    assert r.returncode == 0, r.stderr
    assert "promoted" in r.stdout
    promoted = (registry / "yt2wiki__v0.1.0").read_text(encoding="utf-8")
    assert promoted == MANIFEST_BODY  # same manifest content, same digest
    assert "linux/amd64" in report.read_text(encoding="utf-8")
    assert "linux/arm64" in report.read_text(encoding="utf-8")
