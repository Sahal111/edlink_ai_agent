import hashlib
import hmac
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from app import approval_service, task_service, telegram_service
from app.ai_service import GeminiQuotaError, generate_draft
from app.attachment_service import process_attachments
from app.database import get_connection, init_db

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("edlink.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if not os.getenv("EDLINK_API_TOKEN", "").strip():
        logger.warning(
            "EDLINK_API_TOKEN belum diisi: API tidak meminta token. "
            "Pastikan backend hanya berjalan di 127.0.0.1."
        )
    telegram_service.start_polling(
        approval_service.handle_message,
        approval_service.handle_callback,
    )
    try:
        yield
    finally:
        telegram_service.stop_polling()


app = FastAPI(
    title="EdLink AI Agent",
    version="0.4.0",
    lifespan=lifespan,
)


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    """Jika EDLINK_API_TOKEN diisi, setiap request /tasks* wajib membawanya."""
    expected = os.getenv("EDLINK_API_TOKEN", "").strip()
    if not expected:
        return
    if not x_api_token or not hmac.compare_digest(x_api_token, expected):
        raise HTTPException(status_code=401, detail="Token API salah atau tidak ada")


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


def _duplicate_response(conn, source_key, attachment_text, attachment_info, background_tasks):
    existing = conn.execute(
        "SELECT id, status, attachment_text FROM tasks WHERE source_key = ?",
        (source_key,),
    ).fetchone()
    if existing is None:
        return None

    # Tugas sudah ada, tetapi lampirannya baru terbaca sekarang.
    attachment_updated = bool(
        attachment_text and not (existing["attachment_text"] or "").strip()
    )
    if attachment_updated:
        conn.execute(
            "UPDATE tasks SET attachment_text = ?, attachment_info = ? WHERE id = ?",
            (attachment_text, attachment_info, existing["id"]),
        )

    if attachment_updated and existing["status"] == task_service.DETECTED:
        background_tasks.add_task(
            approval_service.notify_new_task,
            existing["id"],
            "Lampiran tugas berhasil dibaca",
        )

    return {
        "created": False,
        "message": "Tugas sudah pernah terdeteksi",
        "task_id": existing["id"],
        "status": existing["status"],
        "attachment_updated": attachment_updated,
        "attachment_info": attachment_info,
    }


@app.post("/tasks", dependencies=[Depends(require_token)])
def create_task(task: TaskInput, background_tasks: BackgroundTasks):
    title = task.title.strip()

    if not title:
        raise HTTPException(422, "Judul tugas tidak boleh kosong")

    url = clean_url(str(task.url))

    source_key = hashlib.sha256(f"{url}|{title.casefold()}".encode("utf-8")).hexdigest()

    # Hanya teks hasil ekstraksi yang disimpan; file PDF-nya tidak disimpan.
    attachment_text, attachment_info = process_attachments(task.attachments)
    if attachment_info:
        logger.info("Lampiran: %s", attachment_info)

    with get_connection() as conn:
        duplicate = _duplicate_response(
            conn, source_key, attachment_text, attachment_info, background_tasks
        )
        if duplicate:
            return duplicate

        try:
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
        except sqlite3.IntegrityError:
            # Dua request bersamaan untuk tugas yang sama: yang kalah dianggap duplikat.
            conn.rollback()
            duplicate = _duplicate_response(
                conn, source_key, attachment_text, attachment_info, background_tasks
            )
            if duplicate:
                return duplicate
            raise

    # Notifikasi + pembuatan draf berjalan di belakang agar extension tidak menunggu.
    background_tasks.add_task(approval_service.notify_new_task, task_id)

    return {
        "created": True,
        "message": "Tugas berhasil disimpan",
        "task_id": task_id,
        "status": task_service.DETECTED,
        "attachment_info": attachment_info,
    }


@app.get("/tasks", dependencies=[Depends(require_token)])
def list_tasks():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title, description, due_text,
                   url, status, attachment_info, created_at
            FROM tasks
            ORDER BY id DESC
            """).fetchall()

    return [dict(row) for row in rows]


@app.post("/tasks/{task_id}/draft", dependencies=[Depends(require_token)])
def create_task_draft(task_id: int):
    task = task_service.get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Tugas tidak ditemukan",
        )

    if task["status"] in task_service.LOCKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Tugas berstatus {task['status']}; draf tidak dapat dibuat ulang",
        )

    logger.info("Mulai membuat draf AI untuk tugas %s", task_id)

    try:
        draft = generate_draft(
            title=task["title"],
            description=task["description"],
            attachment_text=task["attachment_text"] or "",
        )
        logger.info("Draf AI selesai dibuat untuk tugas %s", task_id)

    except GeminiQuotaError as e:
        raise HTTPException(
            status_code=429,
            detail="Kuota Gemini habis. Coba lagi nanti atau ganti model.",
        ) from e

    except Exception as e:
        logger.error("Gemini gagal: %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=502,
            detail="Gagal membuat draf AI. Periksa terminal backend.",
        ) from e

    # Status bisa berubah selama menunggu Gemini (mis. disetujui di Telegram).
    if not task_service.save_draft(task_id, draft):
        raise HTTPException(
            status_code=409,
            detail="Status tugas berubah saat draf dibuat; draf tidak disimpan",
        )

    return {
        "task_id": task_id,
        "status": task_service.DRAFT_READY,
        "draft": draft,
    }


@app.get("/tasks/{task_id}/approved-draft", dependencies=[Depends(require_token)])
def get_approved_draft(task_id: int):
    """Draf yang sudah disetujui pengguna; dipakai extension untuk mengisi editor."""
    task = task_service.get_task(task_id)

    if task is None:
        raise HTTPException(status_code=404, detail="Tugas tidak ditemukan")

    if task["status"] not in (task_service.APPROVED, task_service.FILLED):
        raise HTTPException(
            status_code=409,
            detail=f"Tugas belum disetujui (status: {task['status']})",
        )

    if not (task["draft"] or "").strip():
        raise HTTPException(status_code=409, detail="Draf kosong")

    return {
        "task_id": task_id,
        "status": task["status"],
        "draft": task["draft"],
    }


@app.post("/tasks/{task_id}/filled", dependencies=[Depends(require_token)])
def mark_task_filled(task_id: int, background_tasks: BackgroundTasks):
    """Extension melapor bahwa draf sudah ditempel ke Teks Editor EdLink.

    Ini TIDAK berarti tugas dikumpulkan: tombol Simpan di EdLink ditekan
    sendiri oleh pengguna.
    """
    task = task_service.get_task(task_id)

    if task is None:
        raise HTTPException(status_code=404, detail="Tugas tidak ditemukan")

    if task["status"] == task_service.FILLED:
        return {"task_id": task_id, "status": task_service.FILLED, "changed": False}

    if not task_service.transition(
        task_id, (task_service.APPROVED,), task_service.FILLED
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Hanya tugas berstatus approved yang bisa ditandai filled "
            f"(status: {task['status']})",
        )

    background_tasks.add_task(approval_service.notify_filled, task_id)
    return {"task_id": task_id, "status": task_service.FILLED, "changed": True}
