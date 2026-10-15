from app.database import get_connection


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


def save_draft(task_id: int, draft: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET draft = ?, status = ? WHERE id = ?",
            (draft, "draft_ready", task_id),
        )


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
