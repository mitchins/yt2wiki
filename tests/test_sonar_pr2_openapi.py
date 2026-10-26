"""PR2 (Sonar S8415) regression tests: documented error responses.

Asserts the generated OpenAPI documents the status codes routes actually
emit, and exercises representative 400/401/403/404/409 paths through the
real app. No models, no network.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import login_client, post_json  # noqa: E402,F401
from yt2wiki.config import Settings  # noqa: E402
from yt2wiki.db.schema import Database  # noqa: E402
from yt2wiki.web.app import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    return settings, db, create_app(settings)


def _responses_of(openapi: dict, method: str, path: str) -> dict:
    return openapi["paths"][path][method].get("responses", {})


def test_openapi_documents_flagged_error_codes(app):
    _settings, _db, application = app
    schema = TestClient(application).get("/openapi.json").json()

    assert set(_responses_of(schema, "get", "/jobs/{job_id}")) >= {"404"}
    assert set(_responses_of(schema, "get", "/assets/{article_id}/{filename}")) >= {"400", "404"}
    assert set(_responses_of(schema, "get", "/frames/{source_id}/{filename}")) >= {"400", "404"}
    assert set(_responses_of(schema, "get", "/media/{source_id}/{filename}")) >= {"400", "404"}

    assert set(_responses_of(schema, "post", "/api/auth/login")) >= {"401", "403"}
    assert set(_responses_of(schema, "post", "/api/sources/url")) >= {"400", "401", "403", "409"}
    assert set(_responses_of(schema, "post", "/api/sources/upload")) >= {"400", "401", "403", "409"}
    assert set(_responses_of(schema, "post", "/api/jobs/{job_id}/reprocess")) >= {
        "400", "401", "403", "404", "409"}
    assert set(_responses_of(schema, "post", "/api/settings/inference/test")) >= {
        "400", "401", "403"}
    assert set(_responses_of(schema, "post", "/api/settings/inference/save-test")) >= {
        "400", "401", "403", "409"}
    assert set(_responses_of(schema, "post", "/api/preview")) >= {"400", "404"}
    assert set(_responses_of(schema, "post", "/api/articles/{article_id}")) >= {
        "400", "401", "403", "404"}
    assert set(_responses_of(schema, "post", "/api/articles/{article_id}/images/move")) >= {
        "400", "401", "403", "404"}
    assert set(_responses_of(schema, "post", "/api/articles/{article_id}/images/replace")) >= {
        "400", "401", "403", "404"}
    assert set(_responses_of(schema, "post", "/api/articles/{article_id}/revert")) >= {
        "401", "403", "404"}
    assert set(_responses_of(schema, "get",
                             "/api/articles/{article_id}/revisions/{revision_id}")) >= {"404"}
    assert set(_responses_of(schema, "get",
                             "/api/v9/articles/{article_id}/alternatives")) >= {"404"}

    # The edit *page* has no server-side auth guard (saves are guarded at the
    # API): it must not advertise 401/403 it cannot emit.
    assert not {"401", "403"} & set(_responses_of(schema, "get",
                                                  "/articles/{article_id}/edit"))
    # Form routes whose guards do propagate auth errors declare them.
    assert set(_responses_of(schema, "post", "/jobs/{job_id}/retry")) >= {"401", "403"}
    assert set(_responses_of(schema, "post", "/jobs/{job_id}/cancel")) >= {"401", "403"}


def test_error_descriptions_are_nonempty(app):
    _settings, _db, application = app
    schema = TestClient(application).get("/openapi.json").json()
    for path_item in schema["paths"].values():
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for status, doc in op.get("responses", {}).items():
                assert (doc or {}).get("description"), (path_item, status)


def test_live_error_paths_match_documented_codes(app, operator_password, monkeypatch):
    _settings, _db, application = app
    anon = TestClient(application)
    client, csrf = login_client(application)

    # 401: unauthenticated operator call
    assert anon.post("/api/articles/art-x", json={"markdown": "x" * 20}).status_code == 401
    # 403: authenticated but missing CSRF token
    r = client.post("/api/articles/art-x", json={"markdown": "x" * 20})
    assert r.status_code == 403
    # 401: wrong password on login
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    # 400 / 404
    assert client.post("/api/preview", json={"article_id": "art-x"}).status_code == 400
    assert anon.get("/api/jobs/missing").status_code == 404
    assert anon.get("/assets/art-x/missing.jpg").status_code == 404
    assert anon.get("/assets/art-x/..jpg").status_code == 400
    # 409: save-test against a stale provider identity (endpoint test stubbed:
    # the route's own provider I/O is not under test here)
    from yt2wiki.web import inference as web_inference

    payload = {"name": "stale", "url": "http://inference.invalid:9/v1", "model": "m",
               "api_key_env": "", "role": "writer"}
    saved = web_inference.save_one(_settings.paths, payload)
    seen: dict = {}

    def fake_test_endpoint(url, api_key_env, model=""):
        return {"ok": True, "url": url, "model": model, "models": []}

    def fake_record_qualified(paths, name, result, *, tested):
        seen["name"] = name
        raise web_inference.StaleIdentityError("identity changed; retest")

    monkeypatch.setattr(web_inference, "test_endpoint", fake_test_endpoint)
    monkeypatch.setattr(web_inference, "record_qualified", fake_record_qualified)
    r = post_json(client, csrf, "/api/settings/inference/save-test", payload)
    assert r.status_code == 409
    assert "retest" in r.json()["detail"]
    assert seen["name"] == saved["name"]
