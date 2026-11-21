"""SQLite index: schema and helpers."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    src_path TEXT NOT NULL UNIQUE,
    src_root TEXT,
    filename TEXT,
    ext TEXT,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    rep_id INTEGER,              -- first file with the same sha256 (exact-duplicate representative)
    width INTEGER, height INTEGER, format TEXT,
    taken_at TEXT, date_source TEXT,
    camera_make TEXT, camera_model TEXT, software TEXT,
    lat REAL, lon REAL,
    cc TEXT, country TEXT, state TEXT, county TEXT, city TEXT, place TEXT,
    phash INTEGER, dhash INTEGER,
    type TEXT, type_source TEXT, type_scores TEXT,
    dup_group INTEGER, is_keeper INTEGER,
    dest_path TEXT,
    error TEXT,
    analyzed INTEGER DEFAULT 0, geocoded INTEGER DEFAULT 0, embedded INTEGER DEFAULT 0,
    classified INTEGER DEFAULT 0, faced INTEGER DEFAULT 0, organized INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS ix_files_taken ON files(taken_at);
CREATE INDEX IF NOT EXISTS ix_files_type ON files(type);
CREATE INDEX IF NOT EXISTS ix_files_place ON files(place);
CREATE INDEX IF NOT EXISTS ix_files_group ON files(dup_group);
CREATE TABLE IF NOT EXISTS embeddings (file_id INTEGER PRIMARY KEY, vec BLOB NOT NULL);  -- image AI vectors (model in meta.embedder)
CREATE TABLE IF NOT EXISTS tags (file_id INTEGER, tag TEXT, score REAL, PRIMARY KEY(file_id, tag));
CREATE INDEX IF NOT EXISTS ix_tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    x1 REAL, y1 REAL, x2 REAL, y2 REAL, score REAL,
    embedding BLOB,
    person_id INTEGER,
    locked INTEGER DEFAULT 0,    -- 1 = assigned by the user; re-grouping never moves it
    not_person INTEGER,          -- person the user removed this face from (never put back there)
    face_px REAL,                -- face box short side in pixels, at detection scale (<= 1600 px photo)
    blur REAL,                   -- Laplacian variance of the face crop (low = blurry); NULL = not measured yet
    low_quality INTEGER DEFAULT 0,  -- 1 = too small / blurry / unsure: skipped when grouping
    suggest_person INTEGER,      -- borderline match: "Is this <Name>?" queue on the People page
    suggest_sim REAL
);
CREATE INDEX IF NOT EXISTS ix_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS ix_faces_person ON faces(person_id);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT,
    centroid BLOB,
    face_count INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0,
    refs BLOB,                   -- reference face embeddings (k x d float16): confirmed faces first
    ref_count INTEGER DEFAULT 0,
    negs BLOB                    -- faces the user said are NOT this person (k x d float16)
);
-- Faces the user said are not a given person (removed from them, or "No" in the borderline queue).
CREATE TABLE IF NOT EXISTS face_negatives (
    face_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    created_at TEXT,
    PRIMARY KEY (face_id, person_id)
);
-- Albums (web app). Mirrored as folders of hard links under <output>/Albums/<name>/.
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,         -- folder name under Albums/
    created_at TEXT,
    sync_to_pi INTEGER DEFAULT 0  -- v0.7: 1 = Family sync sends this album to CalendarPeace (only these are sent)
);
CREATE TABLE IF NOT EXISTS album_items (
    album_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    added_at TEXT,
    link_path TEXT,               -- output-relative path of the album's link (or copy), NULL if none
    method TEXT,                  -- 'link' (hard link) or 'copy' (hard links not possible)
    PRIMARY KEY (album_id, file_id)
);
CREATE INDEX IF NOT EXISTS ix_album_items_file ON album_items(file_id);
CREATE TABLE IF NOT EXISTS dup_groups (id INTEGER PRIMARY KEY, kind TEXT, keeper_id INTEGER, size INTEGER,
    suggested_keeper_id INTEGER, note TEXT);
-- Clean-up decisions (web app). One row per file the user (or bulk approve) decided on.
CREATE TABLE IF NOT EXISTS file_review (
    file_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,          -- 'keep' or 'remove'
    action_id INTEGER,
    decided_at TEXT
);
-- Undo log: each applied clean-up action with the file operations done and the prior DB state.
CREATE TABLE IF NOT EXISTS review_actions (
    id INTEGER PRIMARY KEY,
    created_at TEXT,
    label TEXT,
    groups TEXT,                  -- JSON list of group ids
    ops TEXT,                     -- JSON list of file operations (for undo)
    prev TEXT,                    -- JSON prior state of touched rows (for undo)
    undone INTEGER DEFAULT 0
);
-- v0.4: learn from Clean up choices. One row per "kept X over Y" pair (features frozen at decision time).
CREATE TABLE IF NOT EXISTS keep_decisions (
    id INTEGER PRIMARY KEY,
    action_id INTEGER,
    group_id INTEGER,
    kept_id INTEGER,
    removed_id INTEGER,
    kept_feat TEXT,
    removed_feat TEXT,
    weight REAL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_keep_decisions_action ON keep_decisions(action_id);
-- v0.6: per-video extras (files.media='video'): duplicate signature, scrub sprite, browser-playable proxy
CREATE TABLE IF NOT EXISTS videos (
    file_id INTEGER PRIMARY KEY,
    sig TEXT,                     -- JSON list of frame dHashes at fixed positions (near-duplicate check)
    hdr INTEGER DEFAULT 0,
    sprite_n INTEGER,             -- scrub/hover sprite sheet: frames, columns, seconds per frame, tile size
    sprite_cols INTEGER,
    sprite_interval REAL,
    tile_w INTEGER,
    tile_h INTEGER,
    proxy TEXT,                   -- NULL (not made), 'ok', 'failed', 'not_needed'
    proxy_error TEXT
);
"""

SCHEMA_VERSION = "5"

# Columns added after v0.1. connect() adds any that an older index is missing (no data is rewritten).
MIGRATIONS = {
    "dup_groups": [("suggested_keeper_id", "INTEGER"), ("note", "TEXT")],
    "faces": [("locked", "INTEGER DEFAULT 0"), ("not_person", "INTEGER"),
              ("face_px", "REAL"), ("blur", "REAL"), ("low_quality", "INTEGER DEFAULT 0"),
              ("suggest_person", "INTEGER"), ("suggest_sim", "REAL"),
              ("frame_t", "REAL")],          # v0.6: video faces: seconds into the video of the sampled frame
    "people": [("refs", "BLOB"), ("ref_count", "INTEGER DEFAULT 0"), ("negs", "BLOB")],
    "albums": [("sync_to_pi", "INTEGER DEFAULT 0")],   # v0.7: picked for Family sync (Sync with CalendarPeace)
    # v0.6 videos: media='video' (NULL = photo), length, codecs, browser-playable as is, Live Photo clip
    "files": [("media", "TEXT"), ("duration", "REAL"), ("vcodec", "TEXT"), ("acodec", "TEXT"),
              ("playable", "INTEGER"), ("live", "INTEGER DEFAULT 0")],
}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Safe, additive schema upgrade for indexes made by older versions. Returns the columns added."""
    added = []
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(f"{table}.{name}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_review_action ON file_review(action_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_faces_suggest ON faces(suggest_person)")
    # v0.3 performance: keyset paging by date, person galleries, exact-copy lookups
    conn.execute("CREATE INDEX IF NOT EXISTS ix_files_taken_id ON files(taken_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_faces_person_file ON faces(person_id, file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_files_rep ON files(rep_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_files_media ON files(media)")
    # v0.3: a face removed from a person (faces.not_person) becomes a negative example for that person
    conn.execute("INSERT OR IGNORE INTO face_negatives(face_id, person_id, created_at) "
                 "SELECT id, not_person, NULL FROM faces WHERE not_person IS NOT NULL")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    return added


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


class DB:
    """Thin wrapper with a lock so GUI/web threads can share one connection."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = connect(self.path)
        self.lock = threading.RLock()

    def execute(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq):
        with self.lock:
            return self.conn.executemany(sql, seq)

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def commit(self):
        with self.lock:
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    def get_meta(self, key: str, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r[0]) if r else default

    def set_meta(self, key: str, value):
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))
        self.commit()


def vec_to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float16).tobytes()


def blob_to_vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


def to_signed64(x: int) -> int:
    return x - (1 << 64) if x >= (1 << 63) else x


def to_unsigned64(x: int) -> int:
    return x + (1 << 64) if x < 0 else x
