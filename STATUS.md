# yt2wiki — status

Status board (MarkDeck-compatible). Shipped pillars reflect v0.1.0
(`cc0ba96`, GHCR `v0.1.0` = `sha256:ea649cc9…`).

## Done — v0.1.0 public beta

- 🟢 Evidence-grounded article pipeline
    YouTube URL or uploaded video → acquisition → external Whisper ASR →
    sectioned article. Every claim traces to a timestamped transcript span;
    exact values preserved verbatim; a failed section fails loudly (no
    silent transcript fallback, no invented commands).
- 🟢 Editorial integrity
    Subject-first prose with bounded correction; whole-video title context;
    transcript-dump and placeholder detection; reader-safe captions at every
    persistence boundary; genuine code fences and human edits preserved.
- 🟢 Visual selection with grounded judging
    Candidate frames per section, comparative multi-image rubric judge,
    strict `json_schema` outputs, client-side grounding (exact score
    coverage, integer `images_received`), NO_IMAGE fail-closed to the veto
    loop. A malformed model reply can never publish a frame.
- 🟢 Provenance and immutable publishing
    Per-generation manifests (provider identities, prompt hashes, git
    commit, image digest), immutable revisions, staging before commit — a
    failed generation never replaces the last good publication.
- 🟢 Local-first publisher web app
    Library, reader, editor, search, exports (Markdown / self-contained
    HTML / ZIP); operator password auth (fail-closed) with CSRF; guided
    inference setup with per-role qualification and readiness gates.
- 🟢 Release engineering
    Multi-arch GHCR images (amd64 + arm64, anonymous pull), first-build-wins
    `sha-<commit>` tags, version-tag promotion without rebuilds, Apache-2.0,
    deterministic CI (480 tests, Sonar gate).

## In flight

- 🟡 Production upgrade to v0.1.0
    Canary acceptance passed end-to-end (publish, images, restart survival,
    failure preservation); production still pinned to the previous release
    pending operator go (`YT2WIKI_IMAGE_TAG=v0.1.0`).
- 🟡 LAN exposure via authenticated gateway
    Read pages are open by design, so LAN access currently runs through a
    basic-auth reverse proxy (one master password). Native proxy auth lands
    with the reverse-proxy roadmap item below.

## Roadmap

- 🔵 Major claim extractor
    Structured extraction of the actionable claims (commands, settings,
    values, prerequisites) behind each article section.
- 🔵 Fact checker
    Depends on the claim extractor: verify extracted claims against the
    source evidence before publication.
- 🔵 "Blink"-style short format
    Short-format rendering of the same evidence base (quick-reference
    summary cards).
- 🔵 Reverse proxy auth support
    `REMOTE_USER` header + `TRUSTED_PROXY` configuration so nginx/authelia
    (or any authenticating proxy) can front the publisher; replaces the
    basic-auth gateway stopgap.
- 🔵 Multi-user mode
    Optional user accounts that can submit jobs and share the library;
    operator becomes admin; guest read mode possible.
- 🔵 Meaningful captions and alt text
    Replace "Illustration for <heading>" placeholders with real per-step
    visual descriptions; proper alt text for accessibility (screen readers,
    empty-alt rules).
