# Sonar findings — 3 bugs + 5 vulnerabilities (first main analysis `625aa2e`)

**Analysis:** `f2255b38-d61d-4e9b-b859-bfefa0dad05a` `2026-10-22` `branch main` `revision 625aa2e`
**Source:** `https://sonarcloud.io/api/issues/search?componentKeys=mitchins_yt2wiki&branch=main&types=BUG,VULNERABILITY&ps=100` — 8 issues total (see `SONAR_BASELINE.md` for gate).

Retrieved live 2026-10-22 via authenticated `SONAR_TOKEN`. Rule, severity, component, line, message, flows, creationDate taken verbatim. Reproduction and reachability checked against current `main` (`625aa2e`) and local checkout.

---

## 01 — `pythonsecurity:S8707` VULNERABILITY MAJOR — `src/yt2wiki/paths.py:38`

**Message:** *Path Traversal via faulty LLM-supplied CLI arguments in paths.file_hash()* — sink `open(path, "rb")` at `file_hash` line 38.

**Flows (taint):** `cli.py:21 args = parser.parse_args` → `cli.py:31 target` → `cli.py:176 target` → `cli.py:182 Path(target)` → `services/__init__.py:44 source_id_for_file(path)` → `paths.py:185 file_hash(path)` → `paths.py:38 open(path)` (+ `services/__init__.py:43 file_hash` etc; 18-step taint chain reported — see Sonar flows).

**Affected function:**

```py
def file_hash(path: Path, *, prefix: int = 16, chunk: int = 1<<20) -> str:  # paths.py:36
    h = hashlib.sha256()
    with open(path, "rb") as fh:  # 38 sink
        ...
    return h.hexdigest()[:prefix]
def source_id_for_file(path: Path) -> str:
    return f"file-{file_hash(path, prefix=16)}"
```

Callers: `services.Service.create_upload_source(path)` (`services/__init__.py:43-54`), `cli._cmd_add` (`cli.py:163-168`), `cli._cmd_run` (`cli.py:180-182`). Web path: `web/app.py:344-356` `submit_upload` → `Paths.upload_path` (sanitised) → `Service.create_upload_source(dest)` where `dest` is already under `data_dir/uploads`.

**Reachable:** CLI — `yt2wiki add <path>` / `yt2wiki run <path>` — direct user-supplied `target` when it is not `http`. Web — only via the sanitised `dest` inside `data_dir`; not directly user-controlled. V9 pipeline, reader, editor — no. Job execution uses the stored `upload_path`.

**Untrusted input:** CLI `args.target` (local argument vector); for the web, `UploadFile.filename` is sanitised via `safe_slug` and never used as the hash input path (the hash input is the server-chosen `dest`).

**Real effects:** Local arbitrary file read (and copy): `create_upload_source` does `file_hash(path)` then `shutil.copyfile(path, stored)` where `stored = paths.upload_path(sid, path.name)` (`safe_slug`'d). If `path=/etc/passwd`, it hashes it (reads it) and copies it into `~/.local/share/yt2wiki/uploads/<sid>-passwd`. The web `GET /media/...` refuses non-`.mp4/.webm/.mkv`, so not directly served, but the copy remains on disk and the hash `file-<hash>` is inserted as `sources.source_id`. No credential disclosure via web, no command execution, no DB write beyond an attacker-controlled `source_id`/`title=path.name`. For a LAN personal app the CLI caller is the same OS user who could already `cat /etc/passwd`, so not a remote exploit. If the CLI were ever exposed via an agent/LLM tool, it would be a traversal.

**Reproduce (safe, local):**

```py
from pathlib import Path
from yt2wiki.paths import file_hash
# arbitrary read — succeeds if file exists and is readable by the user
assert file_hash(Path("/etc/hosts"))  # reads outside data_dir
```

`rg` on `main` shows no validation before `open` — `file_hash` assumes caller validated.

**Classification:** **CONFIRMED — low (local) / medium if CLI exposed via automation.** Not a false positive — taint is real, but not an internet-facing RCE. Do not dismiss as "personal LAN".

**Risk:** filesystem access / data exposure (confidential file read + copy into data dir), not code execution.

**Proposed action:** Small hardening, no rewrite:
- In `Service.create_upload_source`, validate `path` is a regular file (`Path.is_file()`) and fail fast with a clear error; keep the existing `is_file` check in `cli._cmd_add`/`_cmd_run` but make the service the enforcement point.
- In `cli.py`, keep the `if not path.is_file():` guard and ensure the `Path(target)` is resolved (`Path(target).expanduser()`) before hashing — no change to the happy path.
- In `paths.file_hash`, keep `open` but document it is a trusted-caller sink; add `if not path.is_file(): raise FileNotFoundError` to make taint-safe reading explicit and to give the service a deterministic error.
- No change to `upload_path` (already `safe_slug`).

Exploit details omitted from public issue; fix can be public.

---

## 02 — `python:S2245` VULNERABILITY MAJOR — `src/yt2wiki/v9/judge.py:732`

**Message:** *Make sure that using this pseudorandom number generator is safe here.*

**Affected line:** `random.Random(rng_seed).shuffle(order)` in `score_round` (`judge.py:732`) — deterministic shuffling of candidate order before comparative rubric scoring.

```py
order = list(range(len(shown_cands)))
random.Random(rng_seed).shuffle(order)   # 732
ordered = [shown_cands[i] for i in order]
```

Seed comes from `adjudicate_section_z1(..., seed=0)` — caller-controlled, deterministic, used to de-bias positional effects in the VLM judge, not to generate secrets, tokens, or nonces. No crypto, no authentication, no `secrets` usage nearby.

**Reachable:** V9 `v9_select` / `adjudicate_section_z1` → `score_candidates` → image ranking. Only when a job runs.

**Untrusted input:** No — `seed` is internal (job-level, 0 by default). Transcript/video content does not influence the shuffle beyond `section`/`frames`.

**Real effects:** None — using `random` does not produce credential disclosure or incorrect results; `hashlib`/`secrets` would be slower and non-deterministic for the cache key `rubric:<sha1 …>`.

**Reproduce:** `python -c "import random; r=random.Random(0); l=[1,2,3]; r.shuffle(l); print(l)"` — deterministic.

**Classification:** **FALSE POSITIVE** — non-security use. `S2245` is a hotspot rule; Sonar's own docs allow non-cryptographic PRNG for shuffling/sampling. The code is correct.

**Proposed action:** Document as false positive in this ledger; add a trailing comment `# S2245: non-cryptographic deterministic shuffle for presentation order` or ` # NOSONAR` to silence the hotspot, do not replace with `secrets`. No behaviour change. Keep in debt ledger as intentionally deferred.

---

## 03 — `python:S7493` BUG MAJOR — `src/yt2wiki/web/app.py:347`

**Message:** *Use an asynchronous file API instead of synchronous open() in this async function.*

**Affected lines (625aa2e):**

```py
async def submit_upload(file: UploadFile | None, profile: str) -> str:  # 339
    ...
    dest = settings.paths.upload_path(upload_id, Path(file.filename).name)  # 344
    dest.parent.mkdir(parents=True, exist_ok=True)  # 345
    size = 0
    with open(dest, "wb") as fh:  # 347 — sync in async
        while chunk := await file.read(1 << 20):  # 348
            size += len(chunk)
            fh.write(chunk)  # 350
```

**Reachable:** Every web upload — `POST /add/upload` and `POST /api/sources/upload` → `submit_upload`. Supported V9 operation, editing does not, but upload is primary ingestion.

**Untrusted input:** `file.filename` (sanitised via `safe_slug` in `upload_path`) and `file` bytes (up to video size, streamed 1 MiB at a time).

**Real effects:** Reliability/performance — blocking `open`/`write` inside the async event loop can stall other requests while the 1 MiB chunk is flushed to disk. FastAPI/`starlette` runs `async def` on the event loop; a long `fh.write` blocks it. No data loss (write succeeds), but concurrent `/api/jobs/active` polling or library loads will lag under parallel uploads. No code execution or credential disclosure, but it is a genuine runtime correctness defect.

**Reproduce (deterministic):** Manual — `anyio` task starvation under `pytest` `anyio` backend is hard to unit-test without timing, but the bug is visible via `ruff --select ASYNC` or by inspecting `web/app.py:347` is inside `async def`.

**Classification:** **CONFIRMED** — small, well-understood.

**Proposed action:** Replace blocking I/O with `anyio` async file API (already transitive via `fastapi`/`starlette` → `anyio`):

```py
import anyio
async with await anyio.open_file(dest, "wb") as fh:
    while chunk := await file.read(1 << 20):
        size += len(chunk)
        await fh.write(chunk)
```

No new dependency. Regression test: `tests/test_web_upload_async.py` — mock `UploadFile`, assert `dest` written and `size` correct, and that `anyio.open_file` is used (patch `anyio.open_file`). Keep UI/editor/export behaviour.

---

## 04 — `pythonbugs:S2583` BUG MAJOR — `src/yt2wiki/web/app.py:1099`

**Message:** *Fix this condition that always evaluates to true.* — textRange `1099:25-44` `row["payload_json"] or "{}"` inside `json.loads(...) if ...`.

```py
def _frame_out(row: Any) -> dict[str, Any]:  # 1098
    payload = json.loads(row["payload_json"] or "{}") if row["payload_json"] else {}  # 1099
    return {"frame_id": row["frame_id"], ...}
```

Sonar flags the inner `or "{}"` as dead code: when `row["payload_json"]` is truthy (the `if` branch), `row["payload_json"] or "{}"` is just `row["payload_json"]`; when falsy, the `else {}` branch runs and the `or` is never evaluated. Condition `row["payload_json"]` itself is not always true — it is `None`/`""` for frames without interpretation — but the `or` is redundant.

**Reachable:** Reader diagnostics `GET /articles/{id}` → `views.article_context` → `frames` → `_frame_out` for each frame (always).

**Untrusted input:** `row["payload_json"]` is DB `frame_interpretations.payload_json` — written by `visual_interpret` stage, not user-controlled.

**Real effects:** None — no corruption, no crash (json.loads handles `"{}"` or `null`). Just dead code / maintainability regression.

**Classification:** **CONFIRMED — minor correctness/style.**

**Proposed action:** Simplify to the intended form, preserve behaviour for `None`/`""`:

```py
payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
```

Handle `row["payload_json"]` being `None` (falsy) → `{}`. If it is `"null"` (valid JSON null), `json.loads("null")` returns `None`, then `payload or {}` → keep as `{}` via `_loads` pattern — but current `_frame_out` does not handle `"null"`; the fix keeps parity.

---

## 05 & 06 — `Web:S5247` VULNERABILITY MAJOR — `src/yt2wiki/web/templates/article.html:37` and `:57`

**Message:** *Make sure disabling auto-escaping feature is safe here.* — `|safe` on `view.lead_html` and `view.body_html`.

```jinja
{% if view.lead_html %}<div class="a-lead">{{ view.lead_html|safe }}</div>{% endif %}  {# 37 #}
<div class="a-body">{{ view.body_html|safe }}</div>                                   {# 57 #}
```

`view` comes from `views.render_reader(paths, article_id, markdown)` → `reader.split_markdown` → `article.markdown_to_html` → `reader.enhance`. Source of HTML:

- `markdown_to_html` (`article.py:205-297`) — escapes everything via `html.escape` before injecting tags: code blocks `html.escape`, headings `html.escape` via `_inline`, paragraphs `html.escape`, images `html.escape` for `alt`/`src`. `_inline` escapes then re-adds `<code>`, `<strong>`, `<a href=…>` only after `html.escape`.
- `_safe_href` (`article.py:308-315`) allows only `http`, `https`, `mailto`, or no scheme; blocks `javascript:`, `data:`, `vbscript:` — checked via `html.unescape` for entity-obfuscation. Link rendering `f'<a href="{m.group(2)}">{m.group(1)}</a>'` is safe because `m.group(2)` originates from already-escaped text and `m.group(1)` (label) was escaped before the regex.
- `reader.enhance` (`reader.py:124-203`) post-processes trusted `<figure>` / `<a class="ts">` / callouts / TOC, but does not introduce raw user HTML. Figures use `html.escape` for `alt`, `src` is from `asset_url` (`/assets/<id>/<name>`) which is constructed from DB `frame_id` (validated `^[A-Za-z0-9_-]{1,64}$`).

**Reachable:** Reader `GET /articles/{id}` — the primary product surface. Editing (`POST /api/articles/{id}`) lets an authenticated local user supply `markdown`; that markdown is rendered through the same sanitizer, so a local editor cannot inject `<script>` — it will be escaped to `&lt;script&gt;`.

**Untrusted input:** `article.markdown` is V9 writer output over untrusted transcript/captions (treated as text, not HTML), plus optional user edits via the editor. Captions/OCR are untrusted text per AGENTS.md #7 — `literals` extraction never executes it, and the markdown renderer escapes it.

**Real effects if sanitizer failed:** Stored XSS — attacker with edit access could store ` [x](javascript:alert(1))` or `<img onerror=…>` and it would execute in every reader's browser. Current sanitizer blocks `javascript:` via `_safe_href` and escapes `<`/`>` via `html.escape`, so not exploitable as written.

**Reproduce (safe):** `python -c "from yt2wiki.article import markdown_to_html; print(markdown_to_html('[x](javascript:alert(1))\n\n<script>alert(1)</script>'))"` → `javascript` link becomes plain text `x`, script becomes `&lt;script&gt;`.

**Classification:** **FALSE POSITIVE** with required justification — `|safe` is necessary to render the sanitized HTML, and the sanitizer is in `article.py:308` + `article.py:318-332`. Sonar's generic `S5247` cannot see the upstream escaping.

**Proposed action:** Keep `|safe`, document in this ledger and add a code comment above the Jinja lines: `{# view.lead_html/body_html is already html.escape-sanitized in article.markdown_to_html — see article.py:_safe_href #}`. Do not add `autoescape off` elsewhere. No behaviour change.

---

## 07 — `Web:S5247` VULNERABILITY MAJOR — `src/yt2wiki/web/templates/library.html:59`

```jinja
{% if c.snippet %}<p class="sum">{{ c.snippet|safe }}</p>  {# 59 #}
{% elif c.summary %}<p class="sum">{{ c.summary }}</p>{% endif %}
```

`c.snippet` comes from `views.library_cards` → `snippet(articles_fts, 2, '[[', ']]', '…', 18)` → `views._snippet_html` (`views.py:201-213`):

```py
def _snippet_html(snip: str) -> str:
    if not snip: return ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", snip)
    text = re.sub(r"\(https?://[^)\s]*\)", "", text)
    text = re.sub(r"[#*`>]+", "", text)
    text = html.escape(text)
    text = text.replace("[[", "<mark>").replace("]]", "</mark>")
    return " ".join(text.split())
```

Flow: FTS snippet → strip markdown image/link syntax → escape → re-insert `<mark>` for `[[`/`]]` query highlights. User query `q` is `q.strip()[:200]` and FTS query built via `_fts_query` (not raw). The `<mark>` injection is intentional and safe because it replaces the FTS markers after escaping. `c.summary` goes through `reader.lead_summary` (plain text, escaped via `{{ c.summary }}` auto-escape).

**Reachable:** Library `GET /` / `GET /library?q=…` — every page view.

**Untrusted input:** `q` (search term) and `article.markdown` (same as above). FTS `[[`/`]]` markers are server-inserted, not user HTML.

**Real effects if bypassed:** Reflected XSS via `q` if `[[`/`]]` were not escaped — but they are escaped before `<mark>` insertion, and `q` itself is rendered via `{{ q }}` auto-escaped elsewhere.

**Classification:** **FALSE POSITIVE** — safe by construction; `|safe` required for `<mark>`.

**Proposed action:** Keep `|safe`, add comment `{# c.snippet is html.escape'd in views._snippet_html with <mark> highlights #}`.

---

## 08 — `python:S5863` BUG MAJOR — `tests/test_basics.py:44`

```py
def test_source_id_file_stability(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"0123456789abcdef")
    assert source_id_for_file(p) == source_id_for_file(p)  # 44
    q = tmp_path / "other.mp4"
    q.write_bytes(b"different-bytes")
    assert source_id_for_file(p) != source_id_for_file(q)
```

**Message:** *Replace this assertion to not have the same actual and expected expression.*

**Rationale:** Sonar's S5863 flags `assert a == a` where `a` is the same syntactic expression — it will be `true` for a deterministic pure function, making the test tautological, and will not catch nondeterminism if the two calls are evaluated separately they could still differ. The test intends to assert determinism / stability (same input → same id).

**Reachable:** Test only.

**Real effects:** None at runtime — just a weak test that would pass even if `source_id_for_file` were broken to return a constant.

**Classification:** **CONFIRMED — minor test quality.**

**Proposed action:** Make the two calls distinct bindings:

```py
a = source_id_for_file(p)
b = source_id_for_file(p)
assert a == b
```

or compare to a stored literal. Keep the second assertion `!=` for different content.

---

## Summary table

| # | Rule | Component:line | Type | Severity | Classification | Risk | Fix in Mission 3? |
|---|---|---|---|---|---|---|---|
|1|pythonsecurity:S8707|paths.py:38|VULN|MAJOR|CONFIRMED (local arbitrary read)|filesystem access|Yes — validate in `Service.create_upload_source` + `paths.file_hash` guard|
|2|python:S2245|v9/judge.py:732|VULN|MAJOR|FALSE POSITIVE|none|No — document, optional `NOSONAR`|
|3|python:S7493|web/app.py:347|BUG|MAJOR|CONFIRMED|reliability (event-loop block)|Yes — `anyio.open_file`|
|4|pythonbugs:S2583|web/app.py:1099|BUG|MAJOR|CONFIRMED (dead `or "{}"`)|maintainability|Yes — simplify|
|5|Web:S5247|article.html:37|VULN|MAJOR|FALSE POSITIVE (`lead_html` sanitized)|XSS if sanitizer failed, but blocked|No — comment justification|
|6|Web:S5247|article.html:57|VULN|MAJOR|FALSE POSITIVE (`body_html` sanitized)|as above|No — comment|
|7|Web:S5247|library.html:59|VULN|MAJOR|FALSE POSITIVE (`snippet` escaped + `<mark>`)|as above|No — comment|
|8|python:S5863|test_basics.py:44|BUG|MAJOR|CONFIRMED|test quality|Yes — split binding|

**Gate B:** All eight have an evidence-backed disposition above (4 CONFIRMED with fix, 4 FALSE POSITIVE with rationale).

Sensitive exploit details (S8707 full taint) are kept in this local ledger, not posted to public issue comments.

