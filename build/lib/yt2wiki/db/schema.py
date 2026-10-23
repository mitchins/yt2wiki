"""SQLite database layer (schema + provenance for every derived artifact)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- sources --
CREATE TABLE IF NOT EXISTS sources (
    source_id      TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,              -- youtube | upload | local
    origin_url     TEXT,
    upload_path    TEXT,
    title          TEXT,
    uploader       TEXT,
    duration_s     REAL,
    description    TEXT,
    chapters_json  TEXT,
    language_hint  TEXT,
    retrieved_at   REAL,
    content_hash   TEXT,
    video_path     TEXT,
    audio_path     TEXT,
    fps            REAL,
    width          INTEGER,
    height         INTEGER,
    probe_json     TEXT,
    meta_json      TEXT,
    created_at     REAL NOT NULL
);

-- ------------------------------------------------------------------ jobs --
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    profile          TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed|cancelled
    stage            TEXT NOT NULL DEFAULT '',
    stage_index      INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    article_id       TEXT,
    config_json      TEXT NOT NULL DEFAULT '{}',
    priority         INTEGER NOT NULL DEFAULT 100,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, priority, created_at);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source_id);

CREATE TABLE IF NOT EXISTS stages (
    job_id       TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped
    attempts     INTEGER NOT NULL DEFAULT 0,
    started_at   REAL,
    finished_at  REAL,
    duration_s   REAL,
    artifacts_json TEXT,
    fingerprint  TEXT,
    metrics_json TEXT,
    error        TEXT,
    PRIMARY KEY (job_id, stage)
);

-- ------------------------------------------------------------- transcript --
CREATE TABLE IF NOT EXISTS transcript_segments (
    source_id   TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    text        TEXT NOT NULL,
    avg_logprob REAL,
    no_speech_prob REAL,
    compression_ratio REAL,
    model_key   TEXT,
    PRIMARY KEY (source_id, idx)
);

CREATE TABLE IF NOT EXISTS transcript_words (
    source_id   TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    w_i         INTEGER NOT NULL,
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    word        TEXT NOT NULL,
    probability REAL,
    PRIMARY KEY (source_id, idx, w_i)
);

-- ------------------------------------------------------------------ frames --
CREATE TABLE IF NOT EXISTS frames (
    frame_id    TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    t           REAL NOT NULL,
    path        TEXT NOT NULL,
    width       INTEGER,
    height      INTEGER,
    bytes       INTEGER,
    kind        TEXT NOT NULL,       -- scene|periodic|keyframe|transcript|ocr
    reason      TEXT,
    phash       TEXT,
    scene_score REAL,
    duplicate_of TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS frames_source_t ON frames(source_id, t);

CREATE TABLE IF NOT EXISTS frame_interpretations (
    frame_id       TEXT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    model_key      TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    profile        TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    created_at     REAL NOT NULL,
    PRIMARY KEY (frame_id, model_key, prompt_version)
);

-- --------------------------------------------------------------- evidence --
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id  TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    -- action|prerequisite|tool|material|config|command|code|measurement|
    -- identifier|warning|expected_result|error|correction|alternative|unresolved
    text         TEXT NOT NULL,
    normalized   TEXT,
    literal_json TEXT,               -- exact values with their source spans
    t_start      REAL,
    t_end        REAL,
    frame_ids_json TEXT,
    transcript_spans_json TEXT,
    confidence   REAL,
    model_key    TEXT,
    prompt_version TEXT,
    superseded_by TEXT,
    status       TEXT NOT NULL DEFAULT 'active',   -- active|superseded|rejected
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_source ON evidence(source_id, kind, status);

-- -------------------------------------------------------------- procedures --
CREATE TABLE IF NOT EXISTS procedures (
    procedure_id TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    title        TEXT,
    summary      TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS procedure_steps (
    step_id      TEXT PRIMARY KEY,
    procedure_id TEXT NOT NULL REFERENCES procedures(procedure_id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    text         TEXT NOT NULL,
    detail       TEXT,
    evidence_ids_json TEXT,
    frame_ids_json TEXT,
    warnings_json TEXT,
    expected_result TEXT,
    alternatives_json TEXT,
    created_at   REAL NOT NULL
);

-- --------------------------------------------------------------- articles --
CREATE TABLE IF NOT EXISTS articles (
    article_id     TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    job_id         TEXT,
    title          TEXT NOT NULL,
    markdown       TEXT NOT NULL,
    html           TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'draft',   -- draft|published|archived
    revision_of    TEXT,
    edit_note      TEXT,
    fingerprint    TEXT,
    verification_json TEXT,
    outline_json   TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_source ON articles(source_id, version);

CREATE TABLE IF NOT EXISTS article_revisions (
    revision_id  TEXT PRIMARY KEY,
    article_id   TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    markdown     TEXT NOT NULL,
    html         TEXT,
    edit_note    TEXT,
    origin       TEXT NOT NULL,          -- generated | user | verification-fix
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS article_assets (
    asset_id     TEXT PRIMARY KEY,
    article_id   TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    frame_id     TEXT,
    path         TEXT NOT NULL,
    caption      TEXT,
    alt_text     TEXT,
    created_at   REAL NOT NULL
);

-- ------------------------------------------------------------ verification --
CREATE TABLE IF NOT EXISTS findings (
    finding_id  TEXT PRIMARY KEY,
    article_id  TEXT,
    job_id      TEXT,
    source_id   TEXT,
    direction   TEXT NOT NULL,     -- article->evidence | evidence->article | rules
    severity    TEXT NOT NULL,     -- info|warning|critical
    code        TEXT NOT NULL,
    message     TEXT NOT NULL,
    detail_json TEXT,
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_article ON findings(article_id, severity);

CREATE TABLE IF NOT EXISTS model_loads (
    load_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,
    source_id   TEXT,
    stage       TEXT,
    role        TEXT,
    model_key   TEXT,
    repo        TEXT,
    ok          INTEGER NOT NULL,
    device      TEXT,
    peak_vram_gb REAL,
    load_seconds REAL,
    error       TEXT,
    fallback_of TEXT,
    profile     TEXT,
    created_at  REAL NOT NULL
);

-- --------------------------------------------------------------- FTS5 -----
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    article_id UNINDEXED, title, markdown, tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
    evidence_id UNINDEXED, text, normalized, kind UNINDEXED, tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    source_id UNINDEXED, idx UNINDEXED, text, tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS app_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    level     TEXT NOT NULL,
    logger    TEXT,
    job_id    TEXT,
    source_id TEXT,
    message   TEXT NOT NULL
);
"""


def _stamp_schema_version(conn: sqlite3.Connection) -> None:
    """Record SCHEMA_VERSION, never lowering a newer stamp.

    A library written by a newer build keeps its marker when an older build
    opens it (the additive schema stays readable either way).
    """
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    try:
        current = int(row[0]) if row else 0
    except (TypeError, ValueError):
        current = 0
    if current < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


class Database:
    """Thin synchronous wrapper around SQLite.

    Connections are created per thread: the FastAPI handlers run in a worker
    thread pool, and sharing one connection across threads is not safe.  WAL
    mode keeps concurrent readers cheap.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migrated = False
        self._schema_lock = threading.Lock()
        conn = self.connection()
        conn.executescript(SCHEMA)
        _stamp_schema_version(conn)
        self._migrated = True

    # ------------------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = conn
        return conn

    connect = connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connection().execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self.connection().executemany(sql, [tuple(r) for r in rows])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection().execute(sql, tuple(params)))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    def migrate(self) -> None:
        if self._migrated:
            return
        with self._schema_lock:
            if self._migrated:
                return
            conn = self.connection()
            conn.executescript(SCHEMA)
            _stamp_schema_version(conn)
            self._migrated = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass
            self._local.conn = None

    # ------------------------------------------------------------------
    def log(self, level: str, message: str, *, logger: str = "app",
            job_id: str | None = None, source_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO app_log(ts, level, logger, job_id, source_id, message) VALUES(?,?,?,?,?,?)",
            (time.time(), level, logger[:64], job_id, source_id, message[:4000]),
        )

    def record_model_load(self, entry: dict) -> None:
        self.execute(
            """INSERT INTO model_loads(job_id, source_id, stage, role, model_key, repo, ok,
                                        device, peak_vram_gb, load_seconds, error, fallback_of,
                                        profile, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry.get("job_id"), entry.get("source_id"), entry.get("stage"),
                entry.get("role"), entry.get("model_key"), entry.get("repo"),
                1 if entry.get("ok") else 0, entry.get("device"),
                entry.get("peak_vram_gb"), entry.get("load_seconds"), entry.get("error"),
                entry.get("fallback_of"), entry.get("profile"), time.time(),
            ),
        )

    def now(self) -> float:
        return time.time()


def _json_default(value: Any) -> Any:
    """Serialise dataclasses and Paths instead of stringifying them blindly."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def json_loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default
