import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "tasks.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                due_text TEXT DEFAULT '',
                url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'detected',
                draft TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Migrasi untuk database lama yang dibuat sebelum kolom ini ada.
        _ensure_column(conn, "tasks", "attachment_text", "TEXT DEFAULT ''")
        _ensure_column(conn, "tasks", "attachment_info", "TEXT DEFAULT ''")
