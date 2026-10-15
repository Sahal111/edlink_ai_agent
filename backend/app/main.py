import hashlib
from urllib.parse import urlsplit, urlunsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from app import approval_service, telegram_service
from app.attachment_service import process_attachments
from app.database import get_connection, init_db
from app.ai_service import generate_draft, GeminiQuotaError

app = FastAPI(
    title="EdLink AI Agent",
    version="0.3.0",
)


class AttachmentInput(BaseModel):
    name: str = Field(default="lampiran", max_length=300)
    # PDF 15 MB menjadi sekitar 20 juta karakter base64.
    content_base64: str = Field(max_length=21_000_000)


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=12000)
    due_text: str = Field(default="", max_length=300)
    url: HttpUrl
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=5)


@app.on_event("startup")
def startup():
    init_db()
    telegram_service.start_polling(
        approval_service.handle_message,
        approval_service.handle_callback,
    )


@app.on_event("shutdown")
def shutdown():
    telegram_service.stop_polling()


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
def create_task(task: TaskInput, background_tasks: BackgroundTasks):
    title = task.title.strip()

    if not title:
        raise HTTPException(422, "Judul tugas tidak boleh kosong")

    url = clean_url(str(task.url))

    source_key = hashlib.sha256(f"{url}|{title.casefold()}".encode("utf-8")).hexdigest()

    # Hanya teks hasil ekstraksi yang disimpan; file PDF-nya tidak disimpan.
    attachment_text, attachment_info = process_attachments(task.attachments)
    if attachment_info:
        print(f"DEBUG: Lampiran: {attachment_info}", flush=True)

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id, status, attachment_text FROM tasks WHERE source_key = ?",
            (source_key,),
        ).fetchone()

        if existing:
            # Tugas sudah ada, tetapi lampirannya baru terbaca sekarang.
            attachment_updated = bool(
                attachment_text and not (existing["attachment_text"] or "").strip()
            )
            if attachment_updated:
                conn.execute(
                    "UPDATE tasks SET attachment_text = ?, attachment_info = ? WHERE id = ?",
                    (attachment_text, attachment_info, existing["id"]),
                )

            if attachment_updated and existing["status"] == "detected":
                background_tasks.add_task(
                    approval_service.notify_new_task,
                    existing["id"],
                    "Lampiran tugas berhasil dibaca",
                )

            return {
                "created": False,
                "message": "Tugas sudah pernah terdeteksi",
                "task_id": existing["id"],
                "attachment_updated": attachment_updated,
                "attachment_info": attachment_info,
            }

        cursor = conn.execute(
            """
            INSERT INTO tasks (
                source_key, title, description, due_text, url,
                attachment_text, attachment_info
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                title,
                task.description.strip(),
                task.due_text.strip(),
                url,
                attachment_text,
                attachment_info,
            ),
        )

        task_id = cursor.lastrowid

    # Notifikasi + pembuatan draf berjalan di belakang agar extension tidak menunggu.
    background_tasks.add_task(approval_service.notify_new_task, task_id)

    return {
        "created": True,
        "message": "Tugas berhasil disimpan",
        "task_id": task_id,
        "status": "detected",
        "attachment_info": attachment_info,
    }


@app.get("/tasks")
def list_tasks():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title, description, due_text,
                   url, status, attachment_info, created_at
            FROM tasks
            ORDER BY id DESC
            """).fetchall()

    return [dict(row) for row in rows]


@app.post("/tasks/{task_id}/draft")
def create_task_draft(task_id: int):
    with get_connection() as conn:
        task = conn.execute(
            """
            SELECT id, title, description, attachment_text
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
            attachment_text=task["attachment_text"] or "",
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
