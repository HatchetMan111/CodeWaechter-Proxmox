#!/usr/bin/env python3
"""
CodeWächter – SQLite-Schicht.

Alle Zustände (Projekte, Analyse-Läufe, Findings, Plan-Schritte, Einstellungen)
liegen in EINER SQLite-Datei unter DATA_DIR – vollkommen lokal, kein Server.
"""

import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Datenverzeichnis: im LXC /opt/codewaechter/data (Update-sicher – das
# Quellverzeichnis /opt/codewaechter/src wird bei --update getauscht).
DATA_DIR = Path(os.environ.get("CODEWAECHTER_DATA", "/opt/codewaechter/data"))
DB_PATH = DATA_DIR / "codewaechter.db"
PROJECTS_DIR = DATA_DIR / "projects"

APP_VERSION = "1.0.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'path',   -- git | path | upload
    source_ref  TEXT NOT NULL DEFAULT '',       -- Git-URL oder Pfad
    local_path  TEXT NOT NULL DEFAULT '',       -- analysiertes Verzeichnis
    description TEXT NOT NULL DEFAULT '',
    score       INTEGER,                        -- Score des letzten Laufs
    created_at  TEXT NOT NULL,
    last_run_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at   TEXT NOT NULL,
    files        INTEGER NOT NULL DEFAULT 0,
    lines        INTEGER NOT NULL DEFAULT 0,
    score        INTEGER NOT NULL DEFAULT 0,
    plan_source  TEXT NOT NULL DEFAULT 'regeln',  -- regeln | llm
    counts_json  TEXT NOT NULL DEFAULT '{}',
    languages_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS findings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    rule       TEXT NOT NULL,
    severity   TEXT NOT NULL,                   -- hoch | mittel | niedrig | info
    path       TEXT NOT NULL DEFAULT '',
    line       INTEGER,
    title      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'offen'    -- offen | erledigt | ignoriert
);
CREATE INDEX IF NOT EXISTS idx_findings_project ON findings(project_id, severity, status);

CREATE TABLE IF NOT EXISTS plan_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    title      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    priority   TEXT NOT NULL DEFAULT 'P1',      -- P0 | P1 | P2
    done       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plan_project ON plan_items(project_id, run_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_SETTINGS = {
    "llm_base_url": "",      # z. B. http://192.168.1.23:8000/v1 (OpenAI-kompatibel)
    "llm_api_key": "",
    "llm_model": "",
    "exclude_patterns": "",  # Kommagetrennte Glob-Muster, z. B. 'vendor/*,docs/*'
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def conn_scope():
    """Verbindung mit garantiertem Schließen + Commit/Rollback.

    (sqlite3-Verbindungen schließen sich beim `with conn:` NICHT selbst –
    ohne conn_scope würden Handles anfallen, bis der GC sie einsammelt.)
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    with closing(get_conn()) as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
        conn.commit()


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
