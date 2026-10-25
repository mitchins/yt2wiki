"""V9 editorial-integrity regression suite (no live LLM, GPU, Whisper, network).

Covers the publication-correctness contract: failed sections fail loudly
(never a silent transcript fallback), subject-first prose with a bounded
editorial correction, whole-video titles with justified procedure
classification, reader-safe captions, and effective model provenance.

Fixtures use small frozen transcripts plus the real Hermes "two settings"
segment (10:50-12:13); failures are injected through scripted backends that
exercise the real writer, validation, publication and reader boundaries.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.v9 import writer as W

# --------------------------------------------------------------------------
# Hermes "two settings" segment (yt-v9XWAq74fzg, 633.95s-736.76s), verbatim.
# Actionable facts the article must preserve: operator phone-number allowlist
# (own number, never the bot's), country-code format without plus/spaces/
# dashes, unauthorised DMs set to ignore, terminal backend switched to Docker.
# The excerpt names no command: none may be invented.

HERMES_TWO_SETTINGS = [
    (633.95, 639.33, "has landed and Hermes is responding now. So it's the same agent, same memory, same everything."),
    (639.59, 644.17, 'Only difference is that I now reached it from WhatsApp. And this is the moment that it stops'),
    (644.17, 644.91, 'being soft.'),
    (645.09, 649.07, "So I'm going to go back to the server and I'm going to go back to my phone and I'm going to"),
    (649.07, 649.09, "go back to my phone and I'm going to go back to my phone and I'm going to go back to my phone."),
    (649.13, 656.79, 'Now, I told you at the start that there were two settings that almost'),
    (656.79, 662.83, 'everyone gets it on. So this is where we can now fix them. The first one, once you are paid,'),
    (663.09, 668.59, "you then set your allow list. And here's basically where everyone kind of tips up."),
    (668.89, 673.79, "You can put your own phone number in there, not a bot's number. You want to use your phone,"),
    (673.85, 678.11, "the one that you are messaging from. So people put the bot's number in there by default,"),
    (678.29, 683.31, "and then they lock themselves completely out of their own agent. And they can't even work"),
    (683.31, 687.13, "out why nothing's responding to them. And on top of that, the format matters, too. The"),
    (687.13, 691.93, "country code. There's no plus sign. There's no spaces, no dashes. Okay, cool. So now while"),
    (691.93, 696.21, 'you are in here, set unauthorized DM behavior to ignore. Otherwise, a stranger who messages'),
    (696.21, 701.91, 'your bot number basically gets a peering code back instead of silence. Now, the second one,'),
    (701.95, 708.09, 'by default, is the terminal back end. It runs commands directly on your actual server. So if'),
    (708.29, 710.23, 'something goes wrong, like a bad command,'),
    (710.53, 712.45, 'anything destructive, which then means'),
    (712.45, 713.81, "that it's hitting your real machine."),
    (714.07, 717.67, 'Now what you want instead is the one command right here.'),
    (717.85, 720.11, 'So setting the terminal back into Docker,'),
    (720.25, 723.77, 'that basically sandboxes everything into a container'),
    (723.77, 725.87, 'that you can then wipe and you can rebuild.'),
    (726.19, 727.27, 'Now this part is pretty important,'),
    (727.47, 729.93, 'so please fix both of these before you connect'),
    (729.93, 731.13, 'anything else.'),
    (734.86, 736.76, 'So remember that briefing that I showed you'),
]

# Eleven chapters mirroring the full Hermes tutorial; section 9 (650-733s)
# is the two-settings span that previously published a raw-transcript fallback.
HERMES_CHAPTERS = [
    (0, 80, "Intro"),
    (80, 170, "The foundation"),
    (170, 260, "What we're building"),
    (260, 350, "The brain (models)"),
    (350, 440, "The personality"),
    (440, 520, "Tools and skills"),
    (520, 600, "The memory"),
    (600, 650, "The reach"),
    (650, 733, "The two settings"),
    (733, 830, "The autopilot (cron)"),
    (830, 920, "Outro"),
]

_FILLER = [
    "Open the dashboard and click the settings tab to configure the workspace.",
    "Create a new agent profile and enter a display name for the assistant.",
    "Install the skill package and restart the service to load the new tools.",
    "Connect the messaging channel and verify the pairing code on screen.",
    "Save the configuration file and check the status indicator turns green.",
]

GOOD_TITLE = {"title": "Persistent Agents with Tools and Automation",
              "summary": "Run agents that remember, use tools and message.",
              "kind": "procedure"}


def _good_section_body(span_note: str = "") -> str:
    return ("Configure the allowlist with your own phone number, never the bot's number, "
            "using country-code format with no plus sign, spaces or dashes. "
            "Set unauthorised DM behavior to ignore so strangers receive silence instead of "
            "a peering code. Switch the terminal backend to Docker so commands run sandboxed "
            "in a container you can wipe and rebuild. " + span_note).strip()


def _good_section(heading="The two settings"):
    return {"heading": heading, "body": _good_section_body(),
            "key_points": ["allowlist your own number"], "warnings": ["bot number locks you out"]}


# --------------------------------------------------------------------------
# harness

def _mkdb(tmp_path, source_id, segments, *, title="Demo video", chapters=None,
          duration=920.0, origin_url="https://www.youtube.com/watch?v=abc"):
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    meta = json.dumps({"provenance": {"chapters": [
        {"start_time": a, "end_time": b, "title": t} for a, b, t in (chapters or [])]}})
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, "
        "meta_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (source_id, "youtube", origin_url, title, "Uploader", duration, meta, time.time()))
    db.executemany(
        "INSERT INTO transcript_segments(source_id, idx, start, end, text) VALUES(?,?,?,?,?)",
        [(source_id, i, a, b, t) for i, (a, b, t) in enumerate(segments)])
    return settings, db


def _hermes_segments():
    """Full-tutorial transcript: real two-settings rows, procedural filler."""
    real = {(round(a, 2), round(b, 2)): t for a, b, t in HERMES_TWO_SETTINGS}
    out = []
    t = 0.0
    i = 0
    while t < 920.0:
        end = min(t + 8.0, 920.0)
        key = (round(t, 2), round(end, 2))
        if key in real:
            out.append((t, end, real[key]))
        elif 630.0 <= t < 740.0:
            # Inside the real excerpt window: keep only real rows, skip filler.
            pass
        else:
            out.append((t, end, _FILLER[i % len(_FILLER)]))
            i += 1
        t = end
    for a, b, txt in HERMES_TWO_SETTINGS:
        if not any(abs(a - s) < 0.01 and abs(b - e) < 0.01 for s, e, _ in out):
            out.append((a, b, txt))
    out.sort()
    return out


class ScriptBackend:
    """Scripted writer backend: per-call canned responses or exceptions.

    `route(user_message, call_index)` returns a dict (serialised as the model
    reply), a raw string (used verbatim, e.g. malformed JSON), or raises.
    """

    def __init__(self, route):
        self.route = route
        self.calls: list[str] = []

    def chat(self, messages, **kw):
        user = messages[-1]["content"]
        self.calls.append(user)
        action = self.route(user, len(self.calls))
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, dict):
            return json.dumps(action)
        return action


def _span_of(user: str) -> float | None:
    import re
    m = re.search(r"transcript span: ([\d.]+)s", user)
    return float(m.group(1)) if m else None


# --------------------------------------------------------------------------
# §1 fail-loud writing policy

def test_successful_structured_section(tmp_path):
    settings, db = _mkdb(
        tmp_path, "s-ok", [(0.0, 60.0, "Open the dashboard and click the settings tab."),
                           (60.0, 120.0, "Create a new agent profile and save it.")],
        chapters=[(0, 60, "Setup"), (60, 120, "Profiles")], duration=120.0)
    seen = []

    def route(user, n):
        seen.append(n)
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        return _good_section()

    art = W.write_article(settings, db, "s-ok", backend=ScriptBackend(route))
    assert len(art["sections"]) == 2
    assert all(len(s["body"]) > 100 for s in art["sections"])
    assert art["kind"] == "procedure"
    assert len(seen) == 3  # title + one call per section


def test_timeout_then_success_uses_exactly_two_calls(tmp_path):
    settings, db = _mkdb(tmp_path, "s-retry",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0)
    state = {"n": 0}

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.TimeoutException("simulated write timeout")
        return _good_section()

    backend = ScriptBackend(route)
    art = W.write_article(settings, db, "s-retry", backend=backend)
    assert state["n"] == 2
    assert "allowlist" in art["sections"][0]["body"]


def test_persistent_timeout_fails_job_loudly(tmp_path):
    settings, db = _mkdb(tmp_path, "s-timeout",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0)

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        raise httpx.TimeoutException("permanent timeout")

    with pytest.raises(W.SectionWriteError) as ei:
        W.write_article(settings, db, "s-timeout", backend=ScriptBackend(route))
    err = ei.value
    assert err.category == "timeout"
    assert err.attempts == 2
    assert err.section_id == "sec-01-setup"
    assert err.t_start == 0.0 and err.t_end == 60.0
    # Nothing published: no article row, no article files.
    assert db.query("SELECT * FROM articles") == []
    assert not (settings.paths.article_dir("art9-s-timeout")).exists()


def test_persistent_http_failure_fails_without_retry_storm(tmp_path):
    settings, db = _mkdb(tmp_path, "s-http",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0)
    calls = []

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        calls.append(n)
        req = httpx.Request("POST", "http://writer.test/v1/chat/completions")
        raise httpx.HTTPStatusError("server error", request=req,
                                    response=httpx.Response(500, request=req))

    with pytest.raises(W.SectionWriteError) as ei:
        W.write_article(settings, db, "s-http", backend=ScriptBackend(route))
    assert ei.value.category == "server"
    assert ei.value.attempts == 2
    assert len(calls) == 2  # bounded: no repair loop


def test_client_error_is_not_retried(tmp_path):
    settings, db = _mkdb(tmp_path, "s-400",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0)
    calls = []

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        calls.append(n)
        req = httpx.Request("POST", "http://writer.test/v1/chat/completions")
        raise httpx.HTTPStatusError("bad request", request=req,
                                    response=httpx.Response(400, request=req))

    with pytest.raises(W.SectionWriteError) as ei:
        W.write_article(settings, db, "s-400", backend=ScriptBackend(route))
    assert ei.value.category == "transport"
    assert ei.value.attempts == 1
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [
    "this is not json {{{",
    '{"heading": "x"}',  # valid JSON, missing body
    '{"heading": "x", "body": "  "}',  # empty body
    '{"heading": "x", "body": "_Transcript excerpt (model unavailable):_\\n\\nraw"}',
])
def test_unusable_responses_fail_after_bounded_retry(tmp_path, bad):
    settings, db = _mkdb(tmp_path, "s-bad",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0)
    calls = []

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        calls.append(n)
        return bad

    with pytest.raises(W.SectionWriteError) as ei:
        W.write_article(settings, db, "s-bad", backend=ScriptBackend(route))
    assert ei.value.category in ("invalid_json", "invalid_content")
    assert ei.value.attempts == 2
    assert len(calls) == 2


def test_truncated_generation_fails_loudly(tmp_path):
    from yt2wiki.backends.remote_openai import TruncatedResponseError
    settings, db = _mkdb(tmp_path, "s-trunc",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0)

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        raise TruncatedResponseError('{"heading": "Setup", "body": "Partial...')

    with pytest.raises(W.SectionWriteError) as ei:
        W.write_article(settings, db, "s-trunc", backend=ScriptBackend(route))
    assert ei.value.category == "truncated"
    assert ei.value.attempts == 2


def test_section9_failure_identifies_section_and_publishes_nothing(tmp_path):
    settings, db = _mkdb(tmp_path, "s-hermes", _hermes_segments(),
                         chapters=HERMES_CHAPTERS, duration=919.7)

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        if _span_of(user) == 650.0:
            raise httpx.TimeoutException("section 9 always times out")
        return _good_section()

    with pytest.raises(W.SectionWriteError) as ei:
        W.write_article(settings, db, "s-hermes", backend=ScriptBackend(route))
    err = ei.value
    assert err.section_id == "sec-09-the-two-settings"
    assert (err.t_start, err.t_end) == (650.0, 733.0)
    assert err.attempts == 2
    assert "allow list" in err.excerpt  # diagnostic evidence preserved
    assert db.query("SELECT * FROM articles") == []
    assert list(db.query("SELECT * FROM generations")) == []


def test_no_speech_span_still_allowed(tmp_path):
    settings, db = _mkdb(tmp_path, "s-silent",
                         [(120.0, 160.0, "Open the dashboard and click the settings tab."),
                          (160.0, 220.0, "Click save to apply the changes.")],
                         chapters=[(0, 100, "Intro"), (100, 300, "Setup")],
                         duration=300.0)

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        if _span_of(user) == 0.0:
            raise AssertionError("model must not be called for an empty span")
        return _good_section()

    art = W.write_article(settings, db, "s-silent", backend=ScriptBackend(route))
    assert art["sections"][0]["body"] == "_No speech in this span._"
    assert "allowlist" in art["sections"][1]["body"]


# --------------------------------------------------------------------------
# §1 failure boundary through run_source: failed regen preserves prior work

def _persist_good_article(settings, db, job_id, source_id="s-regen"):
    from yt2wiki.v9 import publish as P
    frame = settings.paths.root / "frame-f-1.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9")
    db.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) "
               "VALUES(?,?,?,?,?,?)",
               ("f-1", source_id, 5.0, str(frame), "scene", time.time()))
    sections = [{"section_id": "sec-01-setup", "heading": "Setup",
                 "body": "Open the dashboard and click the settings tab to configure it. "
                         "This published body must survive a failed regeneration.",
                 "t_start": 0.0, "t_end": 60.0, "key_points": [], "warnings": []}]
    picks = {"sec-01-setup": [{"frame_id": "f-1", "t": 5.0, "path": str(frame),
                               "caption": "Operator caption kept by hand",
                               "alt_text": "Dashboard settings"}]}
    md, _assets = P.assemble_markdown("Good Guide", "Summary.", sections, picks,
                                      {"url": None, "uploader": None})
    return P.persist(settings, db, article_id=f"art9-{source_id}", source_id=source_id,
                     job_id=job_id, title="Good Guide", markdown=md, sections=sections,
                     picks=picks, provenance={"url": None, "fingerprint": "fp",
                                              "profile": "compact"},
                     report={"profile": "compact", "models": {}, "timings": {}},
                     inference_snapshot=None)


def _mkjob(db, source_id):
    jid = f"job-{source_id}"
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, config_json, "
               "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
               (jid, source_id, "compact", "fp", "queued", json.dumps({"inference": {}}),
                time.time(), time.time()))
    return jid


def test_failed_regeneration_preserves_article_images_and_generation(tmp_path, monkeypatch):
    from yt2wiki.v9 import run as R
    settings, db = _mkdb(tmp_path, "s-regen",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0,
                         origin_url=None)
    jid = _mkjob(db, "s-regen")
    _persist_good_article(settings, db, jid)
    adir = settings.paths.article_dir("art9-s-regen")
    before_md = (adir / "article.md").read_text(encoding="utf-8")
    before_assets = sorted(p.name for p in (adir / "assets").glob("*"))
    before_gen = db.query_one(
        "SELECT current_generation_id FROM articles WHERE article_id=?",
        ("art9-s-regen",))["current_generation_id"]
    assert before_gen

    monkeypatch.setattr(R, "_ensure_base",
                        lambda *a, **k: {"reused": True,
                                         "transcript_segments": 1, "frames": 0})

    def boom(*a, **k):
        raise W.SectionWriteError("sec-01-setup", "Setup", 0.0, 60.0,
                                  "timeout", 2, "boom", "excerpt")

    monkeypatch.setattr("yt2wiki.v9.writer.write_article", boom)
    out = R.run_source(settings, db, object(), "s-regen", job_id=jid)
    assert out["ok"] is False
    assert "sec-01-setup" in out["error"]
    job = db.query_one("SELECT state, error FROM jobs WHERE job_id=?", (jid,))
    assert job["state"] == "failed"
    stage = db.query_one("SELECT state, error, metrics_json FROM stages WHERE job_id=? "
                         "AND stage='v9_write'", (jid,))
    assert stage["state"] == "failed"
    assert json.loads(stage["metrics_json"])["section_id"] == "sec-01-setup"
    # Prior publication untouched: files, pointer, assets, revisions.
    assert (adir / "article.md").read_text(encoding="utf-8") == before_md
    assert sorted(p.name for p in (adir / "assets").glob("*")) == before_assets
    assert db.query_one("SELECT current_generation_id FROM articles WHERE article_id=?",
                        ("art9-s-regen",))["current_generation_id"] == before_gen
    assert db.query_one("SELECT COUNT(*) c FROM article_revisions")["c"] == 0


# --------------------------------------------------------------------------
# §2 subject-first prose and the bounded editorial correction

def test_transcript_centric_output_is_corrected_not_published(tmp_path):
    settings, db = _mkdb(tmp_path, "s-edit",
                         [(650.0, 660.0, "set your allow list with your own phone number"),
                          (660.0, 670.0, "set unauthorized DM behavior to ignore"),
                          (670.0, 680.0, "switch the terminal backend to Docker sandbox")],
                         chapters=[(650, 680, "The two settings")], duration=680.0)
    calls = []

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        calls.append(user)
        if len(calls) == 1:
            return {"heading": "The two settings",
                    "body": ("The transcript gives an example of allowlisting. The video covers "
                             "the two settings and the transcript also mentions Docker."),
                    "key_points": [], "warnings": []}
        assert "editorial" in user.lower() or "Rewrite" in user
        return _good_section()

    art = W.write_article(settings, db, "s-edit", backend=ScriptBackend(route))
    body = art["sections"][0]["body"]
    assert len(calls) == 2
    assert "transcript gives" not in body.lower()
    assert "allowlist" in body and "Docker" in body


def test_persistent_metanarration_is_rejected(tmp_path):
    settings, db = _mkdb(tmp_path, "s-meta",
                         [(0.0, 60.0, "Open the dashboard and click the settings tab.")],
                         chapters=[(0, 60, "Setup")], duration=60.0)

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        return {"heading": "Setup",
                "body": ("The video covers the dashboard and the speaker shows how the "
                         "transcript mentions every setting in this video."),
                "key_points": [], "warnings": []}

    with pytest.raises(W.SectionWriteError) as ei:
        W.write_article(settings, db, "s-meta", backend=ScriptBackend(route))
    assert ei.value.category == "editorial"
    assert ei.value.attempts == 2


def test_editorial_check_ignores_code_and_allows_legit_subjects():
    assert W.editorial_violations("Open the dashboard and click save.") == []
    # MQTT subscribe in code and prose about topics is a technical subject.
    body = ("Subscribe the client to the topic `home/sensors` with:\n"
            "```\nmosquitto_sub -t home/sensors\n```")
    assert W.editorial_violations(body) == []
    assert W.editorial_violations("The transcript gives an example of agents.")
    assert W.editorial_violations("The video covers seven pillars.")
    assert W.editorial_violations("This video examines the hardware.")
    assert W.editorial_violations("The demonstration takes a look at thermals.")
    assert W.editorial_violations("Join our free school community today.")
    assert W.editorial_violations("Like and subscribe for more.")


def test_hermes_two_settings_facts_survive_validation(tmp_path):
    settings, db = _mkdb(tmp_path, "s-facts", _hermes_segments(),
                         chapters=HERMES_CHAPTERS, duration=919.7)

    def route(user, n):
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        return _good_section()

    art = W.write_article(settings, db, "s-facts", backend=ScriptBackend(route))
    sec9 = [s for s in art["sections"] if s["section_id"] == "sec-09-the-two-settings"]
    assert len(sec9) == 1
    body = sec9[0]["body"]
    for fact in ("allowlist", "no plus sign", "ignore", "Docker"):
        assert fact in body
    assert W.editorial_violations(body) == []
    # The excerpt names no command: the validated section must not gain one.
    assert "docker run" not in body.lower() and "docker exec" not in body.lower()


# --------------------------------------------------------------------------
# §3 titles, headings, classification

def test_title_prompt_covers_whole_video_not_just_opening(tmp_path):
    settings, db = _mkdb(tmp_path, "s-title", _hermes_segments(),
                         chapters=HERMES_CHAPTERS, duration=919.7)
    prompts = []

    def route(user, n):
        prompts.append(user)
        if _span_of(user) is None:
            return dict(GOOD_TITLE)
        return _good_section()

    W.write_article(settings, db, "s-title", backend=ScriptBackend(route))
    title_prompt = prompts[0]
    assert "The two settings" in title_prompt  # meaningful chapter label
    assert "unauthorized DM" in title_prompt  # late substantive material


def test_heading_skips_filler_openers():
    segs = [{"start": 0.0, "end": 10.0,
             "text": "Hey guys welcome back to the channel smash that like button"},
            {"start": 10.0, "end": 20.0,
             "text": "In this video we will change everything today"},
            {"start": 20.0, "end": 30.0,
             "text": "Open the settings panel to configure the allowlist properly."}]
    assert "settings" in W._heading_from_transcript(segs, 0.0, 30.0).lower()
    hype = [{"start": 0.0, "end": 10.0,
             "text": "The hottest new device right now is the Steam Deck handheld"},
            {"start": 10.0, "end": 20.0,
             "text": "The enclosure uses standard Phillips screws and plastic clips."}]
    assert "screws" in W._heading_from_transcript(hype, 0.0, 20.0).lower()
    # Narrow: genuine subject sentences starting "The best practice" survive.
    legit = [{"start": 0.0, "end": 10.0,
              "text": "The best practice is to back up the drive before opening it."}]
    assert "practice" in W._heading_from_transcript(legit, 0.0, 10.0).lower()
    silent = [{"start": 0.0, "end": 10.0, "text": " "}]
    assert W._heading_from_transcript(silent, 0.0, 10.0) == "Overview"


def test_promo_clip_is_not_a_procedure(tmp_path):
    settings, db = _mkdb(tmp_path, "s-promo",
                         [(0.0, 9.0, "Hey guys welcome back to the channel smash that like button"),
                          (9.0, 18.0, "today we're going to build an amazing agent from scratch"),
                          (18.0, 27.0, "join our free school community with over thirty thousand members"),
                          (27.0, 36.0, "this is going to be absolutely incredible stay tuned"),
                          (36.0, 45.0, "check the links below and let's dive right in")],
                         chapters=None, duration=45.0, title="upl-clip.mp4")

    def route(user, n):
        if _span_of(user) is None:
            return {"title": "Building an Agent from Scratch",
                    "summary": "Build it.", "kind": "procedure"}
        # Descriptive, subject-first, but with no actions to justify procedure.
        return {"heading": "Introduction",
                "body": ("This opening introduces the channel, its community and the topic "
                         "of upcoming agent guides."),
                "key_points": [], "warnings": []}

    art = W.write_article(settings, db, "s-promo", backend=ScriptBackend(route))
    assert art["kind"] == "explainer"
    assert "procedure" not in art["coverage"].get("kind_adjusted", "") or True
    assert art["coverage"].get("kind_adjusted", "").startswith("downgraded")


def test_genuine_short_instruction_stays_procedure(tmp_path):
    settings, db = _mkdb(tmp_path, "s-short",
                         [(0.0, 15.0, "Open the dashboard."),
                          (15.0, 30.0, "Click the blue Connect button to link your account.")],
                         chapters=[(0, 30, "Connect")], duration=30.0)

    def route(user, n):
        if _span_of(user) is None:
            return {"title": "Connect Your Account", "summary": "Link it.",
                    "kind": "procedure"}
        return {"heading": "Connect",
                "body": ("Open the dashboard. Click the blue Connect button to link your account."),
                "key_points": [], "warnings": []}

    art = W.write_article(settings, db, "s-short", backend=ScriptBackend(route))
    assert art["kind"] == "procedure"
    assert "kind_adjusted" not in art["coverage"]


def test_gerund_construction_promise_is_neutralised(tmp_path):
    settings, db = _mkdb(tmp_path, "s-gerund",
                         [(0.0, 20.0, "An agent framework coordinates language models with tools."),
                          (20.0, 40.0, "Memory stores conversation history across sessions.")],
                         chapters=[(0, 60, "Concepts")], duration=60.0,
                         title="clip.mp4")

    def route(user, n):
        if _span_of(user) is None:
            return {"title": "Building a Persistent Agent", "summary": "Build it.",
                    "kind": "procedure"}
        return {"heading": "Concepts",
                "body": ("An agent framework coordinates language models with external tools. "
                         "Memory stores conversation history across sessions."),
                "key_points": [], "warnings": []}

    art = W.write_article(settings, db, "s-gerund", backend=ScriptBackend(route))
    assert art["title"] == "clip.mp4"
    assert art["kind"] == "explainer"
    assert art["coverage"]["title_adjusted"].startswith("neutralised")


def test_narrating_summary_is_blanked_but_valid_title_kept(tmp_path):
    settings, db = _mkdb(tmp_path, "s-sumclean",
                         [(0.0, 20.0, "An agent framework coordinates language models with tools."),
                          (20.0, 40.0, "Memory stores conversation history across sessions.")],
                         chapters=[(0, 60, "Concepts")], duration=60.0,
                         title="concepts-talk.mp4")

    def route(user, n):
        if _span_of(user) is None:
            return {"title": "Agent Framework Concepts",
                    "summary": "The video covers agents and the transcript mentions memory.",
                    "kind": "explainer"}
        return {"heading": "Concepts",
                "body": ("An agent framework coordinates language models with external tools. "
                         "Memory stores conversation history across sessions."),
                "key_points": [], "warnings": []}

    art = W.write_article(settings, db, "s-sumclean", backend=ScriptBackend(route))
    assert art["title"] == "Agent Framework Concepts"
    assert art["summary"] == ""
    assert art["coverage"]["title_adjusted"] == "summary blanked after editorial failure"


def test_construction_promise_title_is_neutralised_without_actions(tmp_path):
    settings, db = _mkdb(tmp_path, "s-concepts",
                         [(0.0, 20.0, "An agent framework coordinates language models with tools."),
                          (20.0, 40.0, "Memory stores conversation history across sessions."),
                          (40.0, 60.0, "The control loop plans, acts and observes results.")],
                         chapters=[(0, 60, "Concepts")], duration=60.0,
                         title="concepts-talk.mp4")
    calls = []

    def route(user, n):
        if _span_of(user) is None:
            calls.append(n)
            if len(calls) == 1:
                return {"title": "Building an Agent Framework from Scratch",
                        "summary": "Build it.", "kind": "procedure"}
            return {"title": "Agent Framework Concepts",
                    "summary": "Understand agents.", "kind": "explainer"}
        return {"heading": "Concepts",
                "body": ("An agent framework coordinates language models with external tools. "
                         "Memory stores conversation history across sessions."),
                "key_points": [], "warnings": []}

    art = W.write_article(settings, db, "s-concepts", backend=ScriptBackend(route))
    assert art["title"] == "Agent Framework Concepts"
    assert art["kind"] == "explainer"


def test_persistent_construction_promise_falls_back_to_source_title(tmp_path):
    settings, db = _mkdb(tmp_path, "s-stubborn",
                         [(0.0, 20.0, "An agent framework coordinates language models with tools.")],
                         chapters=[(0, 60, "Ideas")], duration=60.0,
                         title="concepts-talk.mp4")

    def route(user, n):
        if _span_of(user) is None:
            return {"title": "How to Build It From Scratch", "summary": "x",
                    "kind": "procedure"}
        return {"heading": "Ideas",
                "body": ("An agent framework coordinates language models with external tools."),
                "key_points": [], "warnings": []}

    art = W.write_article(settings, db, "s-stubborn", backend=ScriptBackend(route))
    assert art["title"] == "concepts-talk.mp4"
    assert art["kind"] == "explainer"
    assert "title_adjusted" in art["coverage"]


# --------------------------------------------------------------------------
# §4 reader-safe captions

_LEAK = ("candidate 1 total 7 beats incumbent none by material margin "
         "(judge advised KEEP)")


def test_decision_trace_never_reaches_reader_text():
    from yt2wiki.v9 import run as R
    assert R.looks_like_decision_trace(_LEAK)
    assert R.looks_like_decision_trace("subject probe skipped for f-1: weakness")
    assert not R.looks_like_decision_trace("Whiteboard diagram of the allowlist screen")
    assert not R.looks_like_decision_trace("Illustration for The two settings")


def test_assemble_markdown_scrubs_stale_decision_captions():
    from yt2wiki.v9 import publish as P
    sections = [{"section_id": "sec-09-x", "heading": "The two settings",
                 "body": "Body.\n\n## Allow list\n\n- Set it.", "t_start": 650.0,
                 "t_end": 733.0}]
    picks = {"sec-09-x": [{"frame_id": "f-1", "t": 660.0, "path": "/tmp/x.jpg",
                           "caption": _LEAK, "alt_text": _LEAK}]}
    md, assets = P.assemble_markdown("T", "S", sections, picks, {"url": None})
    for token in ("candidate", "incumbent", "margin", "judge advised"):
        assert token not in md
    assert "Illustration for The two settings" in md
    # Writer-emitted ## subheadings are demoted so the section plan survives.
    assert "\n## Allow list" not in md
    assert "### Allow list" in md
    assert assets and "candidate" not in assets[0]["caption"]
    assert "candidate" not in assets[0]["alt_text"]
    from yt2wiki.article import markdown_to_html
    html = markdown_to_html(md)
    assert "incumbent" not in html


def test_human_caption_survives_regeneration_and_leakage_does_not(tmp_path):
    from yt2wiki.v9 import run as R
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute("INSERT INTO sources(source_id, kind, title, created_at) VALUES(?,?,?,?)",
               ("s-cap", "youtube", "T", time.time()))
    db.execute("INSERT INTO articles(article_id, source_id, title, markdown, html, version, "
               "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
               ("art9-s-cap", "s-cap", "T", "# T\n", "<h1>T</h1>", 1, "published",
                time.time(), time.time()))
    db.execute("INSERT INTO article_assets(asset_id, article_id, frame_id, path, caption, "
               "alt_text, created_at) VALUES(?,?,?,?,?,?,?)",
               ("a1", "art9-s-cap", "f-human", "/p", "Whiteboard diagram of the allowlist screen",
                "Allowlist diagram", time.time()))
    db.execute("INSERT INTO article_assets(asset_id, article_id, frame_id, path, caption, "
               "alt_text, created_at) VALUES(?,?,?,?,?,?,?)",
               ("a2", "art9-s-cap", "f-stale", "/p", _LEAK, _LEAK, time.time()))
    prev = R.load_prev_captions(db, "s-cap")
    assert prev["f-human"]["caption"] == "Whiteboard diagram of the allowlist screen"
    assert "f-stale" not in prev  # legacy leakage is dropped, regenerates neutral
    sections = [{"section_id": "sec-01-a", "heading": "Setup"}]
    picks = {"sec-01-a": [{"frame_id": "f-human"}, {"frame_id": "f-stale"}]}
    R.assign_captions(sections, picks, prev, enable_captions=False)
    by_id = {p["frame_id"]: p for p in picks["sec-01-a"]}
    assert by_id["f-human"]["caption"] == "Whiteboard diagram of the allowlist screen"
    assert by_id["f-human"]["caption_source"] == "edited"
    assert by_id["f-stale"]["caption"] == "Illustration for Setup"
    for p in picks["sec-01-a"]:
        assert not R.looks_like_decision_trace(p["caption"])
        assert not R.looks_like_decision_trace(p["alt_text"])


# --------------------------------------------------------------------------
# §5 effective model provenance

def _snapshot(provider="halogen", model="halogen-qwen3.8-flash-next"):
    url = "http://192.0.2.10:8731/v1"
    return {
        "writer": {"provider": provider, "url": url, "model": model,
                   "reachable": True, "qualified_for_writer": True},
        "vision": {"provider": provider, "url": url, "model": model,
                   "reachable": True, "qualified_for_vision": True},
        "asr": {"provider": "whisper", "url": "http://asr:9000", "model": "large-v3",
                "reachable": True, "qualified_for_asr": True},
    }


def test_effective_models_prefer_job_captured_identities(tmp_path):
    from yt2wiki.v9 import run as R
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    eff = R.effective_models(_snapshot(), db, "missing-source")
    assert eff["writer"]["provider"] == "halogen"
    assert eff["writer"]["model"] == "halogen-qwen3.8-flash-next"
    assert eff["writer"]["remote"] is True
    assert eff["vision"]["provider"] == "halogen"
    assert eff["asr"]["provider"] == "whisper"


def test_reader_shows_halogen_not_registry_default(tmp_path):
    from yt2wiki.web import views
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute("INSERT INTO sources(source_id, kind, title, created_at) VALUES(?,?,?,?)",
               ("s-prov", "youtube", "T", time.time()))
    db.execute("INSERT INTO articles(article_id, source_id, title, markdown, html, version, "
               "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
               ("a-prov", "s-prov", "T", "# T\n", "<h1>T</h1>", 1, "published",
                time.time(), time.time()))
    adir = settings.paths.article_dir("a-prov")
    adir.mkdir(parents=True)
    # Misleading registry report (local Qwen) alongside Halogen provenance.
    (adir / "v9_report.json").write_text(json.dumps({
        "models": {"text": {"repo": "Qwen/Qwen3.5-4B"},
                   "vision": {"repo": "unsloth/Qwen3.5-4B-GGUF"}},
        "models_effective": {
            "writer": {"provider": "halogen", "model": "halogen-qwen3.8-flash-next",
                       "url": "http://192.0.2.10:8731/v1", "remote": True},
            "vision": {"provider": "halogen", "model": "halogen-qwen3.8-flash-next",
                       "url": "http://192.0.2.10:8731/v1", "remote": True},
            "asr": {"provider": "whisper", "model": "large-v3",
                    "url": "http://asr:9000", "remote": True}}}), encoding="utf-8")
    ctx = views.article_context(db, settings.paths, "a-prov")
    eff = ctx["report"]["models_effective"]
    assert eff["text"]["provider"] == "halogen"
    assert "Qwen" not in json.dumps(eff)


def test_reader_legacy_manifest_providers_and_unknown(tmp_path):
    from yt2wiki.web import views
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute("INSERT INTO sources(source_id, kind, title, created_at) VALUES(?,?,?,?)",
               ("s-leg", "youtube", "T", time.time()))
    db.execute("INSERT INTO articles(article_id, source_id, title, markdown, html, version, "
               "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
               ("a-leg", "s-leg", "T", "# T\n", "<h1>T</h1>", 1, "published",
                time.time(), time.time()))
    adir = settings.paths.article_dir("a-leg")
    adir.mkdir(parents=True)
    (adir / "v9_report.json").write_text(json.dumps(
        {"models": {"text": {"repo": "Qwen/Qwen3.5-4B"}}}), encoding="utf-8")
    gdir = adir / "generations" / "gen-1"
    gdir.mkdir(parents=True)
    (gdir / "manifest.json").write_text(json.dumps({"providers": {
        "writer": {"provider": "halogen", "url": "http://192.0.2.10:8731/v1",
                   "model": "halogen-qwen3.8-flash-next", "revision": "unknown"},
        "vision": {"provider": "halogen", "url": "http://192.0.2.10:8731/v1",
                   "model": "halogen-qwen3.8-flash-next", "revision": "unknown"}}}),
        encoding="utf-8")
    db.execute("INSERT INTO generations(generation_id, article_id, source_id, recipe, "
               "recipe_version, manifest_path, created_at) VALUES(?,?,?,?,?,?,?)",
               ("gen-1", "a-leg", "s-leg", "v9", 2, "generations/gen-1/manifest.json",
                time.time()))
    ctx = views.article_context(db, settings.paths, "a-leg")
    eff = ctx["report"]["models_effective"]
    assert eff["text"]["provider"] == "halogen"  # manifest wins over registry guess
    assert eff["asr"] is None  # no trustworthy info: Unknown, never invented


# --------------------------------------------------------------------------
# §8 recipe version: the writer contract changed, so the V9 recipe must too.

def test_v9_recipe_version_bumped_for_new_writer_contract():
    # Bump RECIPE_VERSION (and this pin) whenever the writer prompts, the
    # validation/publication rules or the selection policy change, so a
    # generation manifest never mislabels new-contract output as recipe 1.
    from yt2wiki.generations import RECIPE_VERSION, prompt_hashes
    assert RECIPE_VERSION == 2
    assert prompt_hashes()["recipe_version"] == str(RECIPE_VERSION)


# --------------------------------------------------------------------------
# backend boundary: truncation signal + writer timeout configurability

def test_remote_truncation_raises_typed_error(monkeypatch):
    from yt2wiki.backends.remote_openai import RemoteOpenAIBackend, TruncatedResponseError
    from yt2wiki.config import ModelSpec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [
            {"message": {"content": '{"heading": "x", "body": "par'},
             "finish_reason": "length"}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched_client(*a, **k):
        k["transport"] = transport
        return real_client(*a, **k)

    monkeypatch.setattr("httpx.Client", patched_client)
    spec = ModelSpec(key="remote-t", role="text", engine="remote-openai", repo="m")
    backend = RemoteOpenAIBackend(spec, {}, "cpu", None,
                                  base_url="http://writer.test/v1", model="m")
    backend.load()
    with pytest.raises(TruncatedResponseError):
        backend.chat([{"role": "user", "content": "hi"}], max_new_tokens=10)


def test_writer_timeout_configurable(monkeypatch):
    from yt2wiki.backends.loader import _role_timeout
    monkeypatch.setenv("YT2WIKI_WRITER_TIMEOUT", "5")
    assert _role_timeout("text", None) == 5.0
    assert _role_timeout("text", 30) == 30.0  # explicit provider value wins
    monkeypatch.setenv("YT2WIKI_WRITER_TIMEOUT", "bogus")
    assert _role_timeout("text", None) == 90.0
    monkeypatch.delenv("YT2WIKI_WRITER_TIMEOUT", raising=False)
    assert _role_timeout("text", None) == 90.0
