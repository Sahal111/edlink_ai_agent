import hashlib
import sqlite3
from urllib.parse import urlsplit, urlunsplit

from app.database import get_connection

# Status tugas. Alur: detected -> draft_ready -> approved -> filled
# (atau cancelled). 'filled' = draf sudah ditempel ke Teks Editor EdLink;
# tombol Simpan/Kumpulkan di EdLink tetap ditekan sendiri oleh pengguna.
DETECTED = "detected"
DRAFT_READY = "draft_ready"
APPROVED = "approved"
FILLED = "filled"
CANCELLED = "cancelled"
# Dipakai robot browser: SUBMITTING = robot sedang bekerja di EdLink,
# SUBMITTED = robot sudah menekan Simpan setelah pengguna menyetujui.
SUBMITTING = "submitting"
SUBMITTED = "submitted"

# Status yang tidak boleh ditimpa oleh pembuatan draf baru.
LOCKED_STATUSES = (APPROVED, FILLED, CANCELLED, SUBMITTING, SUBMITTED)


def clean_url(value: str) -> str:
    """Buang query/fragment: URL EdLink bisa memuat token."""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def make_source_key(url: str, title: str) -> str:
    return hashlib.sha256(
        f"{clean_url(url)}|{title.strip().casefold()}".encode("utf-8")
    ).hexdigest()


def task_exists_for_url(url: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE url = ? LIMIT 1", (clean_url(url),)
        ).fetchone()
    return row is not None


def create_task(
    title: str,
    description: str,
    due_text: str,
    url: str,
    attachment_text: str = "",
    attachment_info: str = "",
) -> int | None:
    """Simpan tugas baru. Mengembalikan id, atau None bila sudah ada."""
    url = clean_url(url)
    key = make_source_key(url, title)
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    source_key, title, description, due_text, url,
                    attachment_text, attachment_info
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, title.strip(), description.strip(), due_text.strip(), url,
                 attachment_text, attachment_info),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_task(task_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, title, description, due_text, url, status, draft,
                   attachment_text, attachment_info
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
    return dict(row) if row else None


def set_status(task_id: int, status: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def save_draft(task_id: int, draft: str) -> bool:
    """Simpan draf dan set status draft_ready.

    Tidak menimpa tugas yang sudah approved/filled/cancelled. Mengembalikan
    True bila tersimpan, False bila ditolak karena status terkunci.
    """
    marks = ",".join("?" for _ in LOCKED_STATUSES)
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE tasks SET draft = ?, status = ? "
            f"WHERE id = ? AND status NOT IN ({marks})",
            (draft, DRAFT_READY, task_id, *LOCKED_STATUSES),
        )
        return cursor.rowcount > 0


def transition(task_id: int, expected: tuple[str, ...], new_status: str) -> bool:
    """Ubah status hanya bila status saat ini ada di `expected` (atomik)."""
    marks = ",".join("?" for _ in expected)
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE tasks SET status = ? WHERE id = ? AND status IN ({marks})",
            (new_status, task_id, *expected),
        )
        return cursor.rowcount > 0


def get_state(key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def clear_state(key: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))
