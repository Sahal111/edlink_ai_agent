import hashlib
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from app.database import get_connection, init_db
from app.ai_service import generate_draft, GeminiQuotaError

app = FastAPI(
    title="EdLink AI Agent",
    version="0.2.0",
)


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=12000)
    due_text: str = Field(default="", max_length=300)
    url: HttpUrl


@app.on_event("startup")
def startup():
    init_db()


def clean_url(value: str) -> str:
    parts = urlsplit(value)

    # Jangan menyimpan query atau fragment URL yang mungkin sensitif.
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            "",
            "",
        )
    )


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "edlink-ai-agent",
    }


@app.post("/tasks")
def create_task(task: TaskInput):
    title = task.title.strip()

    if not title:
        raise HTTPException(422, "Judul tugas tidak boleh kosong")

    url = clean_url(str(task.url))

    source_key = hashlib.sha256(f"{url}|{title.casefold()}".encode("utf-8")).hexdigest()

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE source_key = ?",
            (source_key,),
        ).fetchone()

        if existing:
            return {
                "created": False,
                "message": "Tugas sudah pernah terdeteksi",
                "task_id": existing["id"],
            }

        cursor = conn.execute(
            """
            INSERT INTO tasks (
                source_key, title, description, due_text, url
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                source_key,
                title,
                task.description.strip(),
                task.due_text.strip(),
                url,
            ),
        )

        task_id = cursor.lastrowid

    return {
        "created": True,
        "message": "Tugas berhasil disimpan",
        "task_id": task_id,
        "status": "detected",
    }


@app.get("/tasks")
def list_tasks():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title, description, due_text,
                   url, status, created_at
            FROM tasks
            ORDER BY id DESC
            """).fetchall()

    return [dict(row) for row in rows]


@app.post("/tasks/{task_id}/draft")
def create_task_draft(task_id: int):
    with get_connection() as conn:
        task = conn.execute(
            """
            SELECT id, title, description
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Tugas tidak ditemukan",
        )

    print("DEBUG: Mulai membuat draf AI", flush=True)

    try:
        draft = generate_draft(
            title=task["title"],
            description=task["description"],
        )
        print("DEBUG: Draf AI selesai dibuat", flush=True)

    except GeminiQuotaError as e:
        raise HTTPException(
            status_code=429,
            detail="Kuota Gemini habis. Coba lagi nanti atau ganti model.",
        ) from e

    except Exception as e:
        print(f"ERROR GEMINI: {type(e).__name__}: {e}", flush=True)
        raise HTTPException(
            status_code=502,
            detail="Gagal membuat draf AI. Periksa terminal backend.",
        ) from e

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET draft = ?, status = ?
            WHERE id = ?
            """,
            (draft, "draft_ready", task_id),
        )

    return {
        "task_id": task_id,
        "status": "draft_ready",
        "draft": draft,
    }
