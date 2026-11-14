-- Index schema shipped in Photo Sorter v0.1 (for the migration test)
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
    person_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS ix_faces_person ON faces(person_id);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT,
    centroid BLOB,
    face_count INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dup_groups (id INTEGER PRIMARY KEY, kind TEXT, keeper_id INTEGER, size INTEGER);
