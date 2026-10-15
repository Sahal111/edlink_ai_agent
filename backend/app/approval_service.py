"""Alur: tugas baru -> notifikasi -> draf AI -> setuju / revisi / batal.

Status tugas: detected -> draft_ready -> approved (atau cancelled).
Pengiriman ke EdLink BELUM ada: 'approved' hanya menandai bahwa user setuju.
"""

import threading

from app import task_service, telegram_service
from app.ai_service import GeminiQuotaError, generate_draft

AWAITING_REVISION = "awaiting_revision"

_drafting: set[int] = set()
_lock = threading.Lock()


def _log(message: str) -> None:
    print(f"DEBUG: {message}", flush=True)


def _draft_buttons(task_id: int):
    return [
        [("Setuju", f"approve:{task_id}"), ("Revisi", f"revise:{task_id}")],
        [("Batal", f"cancel:{task_id}")],
    ]


def notify_new_task(task_id: int, heading: str = "Tugas baru terdeteksi") -> None:
    """Dipanggil setelah tugas baru tersimpan (atau lampirannya baru terbaca)."""
    if not telegram_service.can_notify():
        _log("Telegram belum dikonfigurasi lengkap, notifikasi dilewati")
        return

    task = task_service.get_task(task_id)
    if task is None:
        return

    description = (task["description"] or "").strip()
    has_attachment_text = bool((task["attachment_text"] or "").strip())
    info = (task["attachment_info"] or "").strip()

    text = (
        f"{heading}\n\n"
        f"{task['title']}\n"
        f"Batas: {task['due_text'] or 'tidak ada'}\n"
        f"{task['url']}"
    )
    if info:
        text += f"\nLampiran: {info}"

    if description or has_attachment_text:
        if description:
            preview = (
                description if len(description) <= 400 else description[:400] + "..."
            )
            text += f"\n\nInstruksi:\n{preview}"
        telegram_service.send_message(text)
        start_draft(task_id)
    else:
        if info:
            note = "Deskripsi kosong dan lampiran belum bisa dibaca (lihat keterangan lampiran di atas)."
        else:
            note = (
                "Deskripsi kosong dan tidak ada lampiran yang terbaca. "
                "Instruksi tugas mungkin ada di tempat lain."
            )
        telegram_service.send_message(
            f"{text}\n\n{note}",
            buttons=[[("Buat draf tetap", f"draft:{task_id}")]],
        )


def start_draft(task_id: int, revision_note: str = "") -> None:
    with _lock:
        if task_id in _drafting:
            telegram_service.send_message(
                "Draf untuk tugas ini sedang dibuat, tunggu sebentar."
            )
            return
        _drafting.add(task_id)

    try:
        task = task_service.get_task(task_id)
        if task is None:
            return

        telegram_service.send_message(
            "Membuat revisi draf..." if revision_note else "Membuat draf jawaban..."
        )

        try:
            draft = generate_draft(
                title=task["title"],
                description=task["description"],
                revision_note=revision_note,
                previous_draft=task["draft"] if revision_note else "",
                attachment_text=task["attachment_text"] or "",
            )
        except GeminiQuotaError:
            telegram_service.send_message(
                "Kuota Gemini habis. Coba lagi nanti.",
                buttons=[[("Coba lagi", f"draft:{task_id}")]],
            )
            return
        except Exception as e:
            print(f"ERROR GEMINI: {type(e).__name__}: {e}", flush=True)
            telegram_service.send_message(
                f"Gagal membuat draf: {e}",
                buttons=[[("Coba lagi", f"draft:{task_id}")]],
            )
            return

        task_service.save_draft(task_id, draft)
        telegram_service.send_message(
            f"Draf untuk: {task['title']}\n\n{draft}",
            buttons=_draft_buttons(task_id),
        )
    finally:
        with _lock:
            _drafting.discard(task_id)


def handle_callback(
    chat_id: int, message_id: int | None, data: str, callback_id: str
) -> None:
    action, _, raw_id = data.partition(":")
    telegram_service.answer_callback(callback_id)

    try:
        task_id = int(raw_id)
    except ValueError:
        return

    # Tombol dihapus agar tidak bisa ditekan dua kali.
    if message_id is not None:
        telegram_service.clear_buttons(chat_id, message_id)

    task = task_service.get_task(task_id)
    if task is None:
        telegram_service.send_message("Tugas tidak ditemukan.")
        return

    status = task["status"]

    if action == "draft":
        if status in ("approved", "cancelled"):
            telegram_service.send_message(f"Tugas ini sudah berstatus {status}.")
            return
        start_draft(task_id)

    elif action == "approve":
        if status == "approved":
            telegram_service.send_message("Draf ini sudah disetujui sebelumnya.")
        elif status != "draft_ready":
            telegram_service.send_message(
                f"Tugas tidak sedang menunggu persetujuan (status: {status})."
            )
        else:
            task_service.set_status(task_id, "approved")
            telegram_service.send_message(
                "Draf disetujui.\n\n"
                "Catatan: pengiriman otomatis ke EdLink belum aktif, jadi jawaban "
                "belum dikirim ke mana pun. Salin draf di atas lalu kumpulkan manual "
                "untuk sekarang."
            )

    elif action == "revise":
        if status != "draft_ready":
            telegram_service.send_message(
                f"Tugas tidak sedang menunggu persetujuan (status: {status})."
            )
            return
        task_service.set_state(AWAITING_REVISION, str(task_id))
        telegram_service.send_message(
            "Tulis instruksi revisinya sebagai pesan, misalnya "
            '"lebih singkat" atau "tambahkan contoh". Kirim /batal untuk membatalkan.'
        )

    elif action == "cancel":
        if status == "approved":
            telegram_service.send_message(
                "Draf ini sudah disetujui, tidak bisa dibatalkan dari sini."
            )
            return
        task_service.set_status(task_id, "cancelled")
        task_service.clear_state(AWAITING_REVISION)
        telegram_service.send_message("Tugas dibatalkan. Tidak ada yang dikirim.")


def handle_message(chat_id: int, text: str) -> None:
    pending = task_service.get_state(AWAITING_REVISION)

    if text.startswith("/batal"):
        task_service.clear_state(AWAITING_REVISION)
        telegram_service.send_message("Revisi dibatalkan.")
        return

    if text.startswith("/start"):
        telegram_service.send_message(
            "Bot aktif. Tugas baru dari EdLink akan dikirim ke sini, "
            "lengkap dengan draf dan tombol persetujuan."
        )
        return

    if pending and pending.isdigit() and not text.startswith("/"):
        task_service.clear_state(AWAITING_REVISION)
        start_draft(int(pending), revision_note=text)
        return

    telegram_service.send_message(
        "Tidak ada draf yang menunggu revisi. Gunakan tombol pada pesan tugas."
    )
