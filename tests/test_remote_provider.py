"""Remote provider routing and mocked OpenAI execution (no GPU, no network)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.web.app import create_app


# ------------------------------------------------------------------ helpers
def _mock_ok_response(json_data, status_code=200):
    m = mock.Mock()
    m.status_code = status_code
    m.json.return_value = json_data
    m.raise_for_status = mock.Mock()
    if status_code >= 400:
        m.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return m


# ------------------------------------------------------------------ ModelSpec.variants propagation
def test_remote_spec_propagates_variants(tmp_path, monkeypatch):
    from yt2wiki.backends.loader import ModelManager

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    # Simulate inference config with a remote provider
    monkeypatch.setenv("YT2WIKI_TEST_KEY", "dummy")
    cfg = {
        "providers": {"lan": {"url": "http://10.0.0.1:8000/v1", "model": "mymodel", "api_key_env": "YT2WIKI_TEST_KEY"}},
        "roles": {"writer": "lan", "vision": "lan"},
    }
    # Write config and status
    from yt2wiki.web import inference as inf
    inf.save(settings.paths, cfg)
    inf.record_test(settings.paths, "lan", {"ok": True, "models": ["mymodel"], "qualified_for_writer": True, "qualified_for_vision": True, "latency_ms": 5})
    mgr = ModelManager(settings)
    spec, provider = mgr._remote_spec("writer")
    assert spec is not None
    # variants must be populated for the active profile
    var = spec.variant(settings.profile)
    assert var.get("url") == "http://10.0.0.1:8000/v1"
    assert var.get("model") == "mymodel"
    assert var.get("api_key_env") == "YT2WIKI_TEST_KEY"
    # snapshot branch
    snap = {"writer": {"provider": "lan", "url": "http://10.0.0.1:8000/v1", "model": "mymodel", "api_key_env": "YT2WIKI_TEST_KEY", "reachable": True, "qualified_for_writer": True, "models": ["mymodel"]},
            "vision": {"provider": "lan", "url": "http://10.0.0.1:8000/v1", "model": "mymodel", "api_key_env": "YT2WIKI_TEST_KEY", "reachable": True, "qualified_for_vision": True, "models": ["mymodel"]}}
    mgr2 = ModelManager(settings, inference_snapshot=snap)
    spec2, _ = mgr2._remote_spec("writer")
    assert spec2.variant(settings.profile).get("url") == "http://10.0.0.1:8000/v1"
    # precedence: explicit model over status models
    inf.record_test(settings.paths, "lan", {"ok": True, "models": ["other"], "qualified_for_writer": True, "qualified_for_vision": True})
    spec3, _ = mgr._remote_spec("writer")
    assert spec3.repo == "mymodel"  # explicit wins


def test_remote_writer_and_vision_mocked_execution(tmp_path, monkeypatch):
    from yt2wiki.backends.remote_openai import RemoteOpenAIBackend
    from yt2wiki.config import ModelSpec

    monkeypatch.setenv("YT2WIKI_K", "sk-test")
    spec = ModelSpec(key="remote-lan", role="writer", engine="remote-openai", repo="m", variants={"compact": {"url": "http://10.0.0.1:8000/v1", "model": "m", "api_key_env": "YT2WIKI_K", "timeout": 5.0}})
    variant = spec.variant("compact")
    backend = RemoteOpenAIBackend(spec, variant, "cpu", None, base_url=variant["url"], model=variant["model"], api_key_env=variant["api_key_env"], timeout=5.0)
    backend.load()
    # Mock httpx.Client for chat
    with mock.patch("httpx.Client") as MockClient:
        mock_client = mock.Mock()
        MockClient.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = _mock_ok_response({"choices": [{"message": {"content": '{"ok": true, "title": "T"}'}}]})
        out = backend.chat([{"role": "user", "content": "hi"}], json_mode=True)
        assert "ok" in out
        # Vision
        from PIL import Image
        im = Image.new("RGB", (32, 32), "red")
        mock_client.post.return_value = _mock_ok_response({"choices": [{"message": {"content": '{"scores": [{"idx":1,"relevance":2,"clarity":2,"instructional":2,"payoff":2,"veto":false}], "decision":"KEEP", "winner":1}'}}]})
        txt = backend.rank_images([im], "judge prompt")
        assert "KEEP" in txt


def test_remote_provider_error_not_silent(tmp_path, monkeypatch):
    from yt2wiki.backends.remote_openai import RemoteOpenAIBackend
    from yt2wiki.config import ModelSpec

    spec = ModelSpec(key="remote-lan", role="vision", engine="remote-openai", repo="m", variants={"compact": {"url": "http://10.0.0.1:8000/v1", "model": "m", "timeout": 5.0}})
    variant = spec.variant("compact")
    backend = RemoteOpenAIBackend(spec, variant, "cpu", None, base_url=variant["url"], model=variant["model"], timeout=5.0)
    backend.load()
    from PIL import Image
    im = Image.new("RGB", (32, 32))
    # Simulate provider returning 500
    with mock.patch("httpx.Client") as MockClient:
        mock_client = mock.Mock()
        MockClient.return_value.__enter__.return_value = mock_client
        err = mock.Mock()
        err.status_code = 500
        err.raise_for_status.side_effect = Exception("500")
        mock_client.post.return_value = err
        with pytest.raises(Exception, match="500"):
            backend.rank_images([im], "prompt")
        # Simulate empty content -> should raise, not return ""
        mock_client.post.return_value = _mock_ok_response({"choices": [{"message": {"content": ""}}]})
        with pytest.raises(RuntimeError, match="empty content"):
            backend.rank_images([im], "prompt")


def test_job_profile_and_overrides_honoured(tmp_path):
    from yt2wiki.services import Service
    from yt2wiki.config import Settings
    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(settings.paths.db_path)
    db.migrate()
    # Create source
    db.execute("INSERT INTO sources(source_id, kind, created_at) VALUES(?,?,?)", ("s1", "upload", time.time()))
    svc = Service(settings, db)
    # Create job with overridden profile=standard
    job = svc.create_job("s1", profile="standard")
    row = db.query_one("SELECT profile, fingerprint FROM jobs WHERE job_id=?", (job,))
    assert row["profile"] == "standard"
    # run_job should load standard profile
    import json as _json
    cap = {}
    orig_load = Settings.load
    def fake_load(**kw):
        cap.update(kw)
        return orig_load(**kw)
    with mock.patch("yt2wiki.config.Settings.load", side_effect=fake_load):
        # run_job will call Settings.load with profile from job
        # We mock pipeline.run_job to avoid actually running stages
        with mock.patch("yt2wiki.pipeline.run_job") as mock_run:
            mock_run.return_value = {"ok": True}
            svc.run_job(job)
            assert cap.get("profile") == "standard"


def test_model_loads_recorded(tmp_path, monkeypatch):
    from yt2wiki.backends.loader import ModelManager

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute("INSERT INTO sources(source_id, kind, created_at) VALUES(?,?,?)", ("src1", "upload", time.time()))
    job_id = "job-rec"
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
               (job_id, "src1", "compact", settings.fingerprint(), "queued", "{}", time.time(), time.time()))
    # Use a fake backend that fails once then succeeds via fallback? Simpler: test that successful load records.
    # Patch build to return a fake backend
    from yt2wiki.backends.base import BaseBackend
    from yt2wiki.gpu import BackendReport
    class FakeBackend(BaseBackend):
        def load(self):
            self.model = object()
            return self._finish_load(time.time())
        def unload(self): self.model = None
    manager = ModelManager(settings, recorder=lambda e: db.record_model_load({**e, "job_id": job_id, "source_id": "src1", "stage": "test"}))
    # Monkeypatch build to return FakeBackend
    monkeypatch.setattr(manager, "build", lambda role, spec: FakeBackend(spec, spec.variant(settings.profile), "cpu", None))
    # Also need to bypass headroom
    monkeypatch.setattr(manager, "headroom_for", lambda role, exclude=(): type("I", (), {"device_index": None, "capped_gb": 1.0, "reason": "cpu"})())
    with manager.acquire("text") as b:
        assert b is not None
    rows = db.query("SELECT * FROM model_loads WHERE job_id=?", (job_id,))
    assert len(rows) >= 1 and rows[0]["ok"] == 1


def test_loader_second_oom_uses_fallback(tmp_path, monkeypatch):
    """Second CUDA OOM on the same spec must try the configured fallback."""
    from yt2wiki.backends.loader import ModelManager
    from yt2wiki.backends.base import BaseBackend
    from yt2wiki.config import ModelSpec, Settings
    from yt2wiki.gpu import BackendReport

    settings = Settings.load(data_dir=tmp_path, profile="compact")

    # Two specs: primary fails with OOM twice, fallback succeeds
    spec_primary = ModelSpec(key="text-9b", role="text", engine="transformers", repo="repo-9b", fallbacks=["text-4b"])
    spec_fallback = ModelSpec(key="text-4b", role="text", engine="transformers", repo="repo-4b")

    class FailingBackend(BaseBackend):
        def __init__(self, spec, *a, **kw):
            super().__init__(spec, {}, "cuda", 0)
            self._calls = 0

        def load(self):
            self._calls += 1
            raise RuntimeError("CUDA out of memory. Tried to allocate")

    class SuccessBackend(BaseBackend):
        def load(self):
            self.model = object()
            self.report = BackendReport(model_key=self.spec.key, repo=self.spec.repo, revision="main", engine="transformers", variant={}, device="cuda", load_seconds=0.1, peak_vram_gb=1.0)
            return self.report

        def unload(self):
            self.model = None

    manager = ModelManager(settings, recorder=lambda e: None)

    # Mock chain and build
    monkeypatch.setattr(settings, "model_with_fallbacks", lambda role: [spec_primary, spec_fallback])
    def fake_build(role, spec):
        if spec.key == "text-9b":
            return FailingBackend(spec)
        return SuccessBackend(spec, {}, "cuda", 0)

    monkeypatch.setattr(manager, "build", fake_build)
    monkeypatch.setattr(manager, "headroom_for", lambda role, exclude=(): type("I", (), {"device_index": 0, "capped_gb": 4.0, "reason": "ok"})())
    monkeypatch.setattr("yt2wiki.backends.loader.free_cuda_memory", lambda: None)

    with manager.acquire("text") as b:
        assert b.spec.key == "text-4b"
    # Both the two OOM attempts should be recorded as failures, then success
    assert any(r["model_key"] == "text-9b" and r["ok"] == 0 for r in manager.load_log)
    assert any(r["model_key"] == "text-4b" and r["ok"] == 1 for r in manager.load_log)
    assert any(r.get("fallback_of") == "text-9b" for r in manager.load_log if r.get("model_key") == "text-4b")
