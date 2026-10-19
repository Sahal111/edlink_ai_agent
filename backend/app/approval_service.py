"""Alur: tugas baru -> notifikasi -> draf AI -> setuju / revisi / batal.

Status tugas: detected -> draft_ready -> approved -> filled (atau cancelled).

- 'approved'  : pengguna menyetujui draf di Telegram.
- 'filled'    : extension sudah menempelkan draf ke Teks Editor EdLink
                (dilaporkan lewat POST /tasks/{id}/filled).
Tanpa robot (EDLINK_ROBOT_MODE=off), backend TIDAK menekan Simpan di EdLink;
pengguna melakukannya sendiri. Dengan robot mode send, tombol Setuju di
Telegram adalah persetujuan eksplisit agar robot mengisi dan menekan Simpan
(status submitting -> submitted).
"""

import logging
import threading

from app import task_service, telegram_service
from app.ai_service import GeminiQuotaError, generate_draft
from app.task_service import APPROVED, CANCELLED, DRAFT_READY, FILLED

logger = logging.getLogger("edlink.approval")

AWAITING_REVISION = "awaiting_revision"

_drafting: set[int] = set()
_lock = threading.Lock()


def _robot():
    # Impor ditunda: robot_service mengimpor modul ini.
    from app import robot_service

    return robot_service


def _draft_buttons(task_id: int):
    label = "Setuju"
    if _robot().can_submit():
        label = (
            "Setuju & kirim ke EdLink"
            if _robot().mode() == "send"
            else "Setuju (uji coba robot)"
        )
    return [
        [(label, f"approve:{task_id}"), ("Revisi", f"revise:{task_id}")],
        [("Batal", f"cancel:{task_id}")],
    ]


def _is_group_task(task: dict) -> bool:
    return "/groupteams/" in (task.get("url") or "")


def notify_new_task(task_id: int, heading: str = "Tugas baru terdeteksi") -> None:
    """Dipanggil setelah tugas baru tersimpan (atau lampirannya baru terbaca)."""
    if not telegram_service.can_notify():
        logger.info("Telegram belum dikonfigurasi lengkap, notifikasi dilewati")
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
    if _is_group_task(task):
        text += "\nTugas kelompok: jawaban terlihat oleh anggota kelompok lain."
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


def notify_filled(task_id: int) -> None:
    """Dipanggil setelah extension melaporkan draf sudah ditempel di editor EdLink."""
    task = task_service.get_task(task_id)
    if task is None:
        return
    telegram_service.send_message(
        f"Draf untuk \"{task['title']}\" sudah ditempel ke Teks Editor di EdLink.\n\n"
        "Periksa isinya di halaman EdLink, lalu tekan Simpan sendiri bila sudah "
        "sesuai. Backend tidak menekan Simpan atau mengumpulkan tugas."
    )


def send_draft(task_id: int) -> None:
    """Kirim draf tersimpan ke Telegram dengan tombol Setuju/Revisi/Batal.

    Hanya untuk tugas berstatus draft_ready; status lain diabaikan agar tombol
    persetujuan tidak muncul untuk tugas yang sudah disetujui atau dibatalkan.
    """
    task = task_service.get_task(task_id)
    if task is None or task["status"] != DRAFT_READY or not (task["draft"] or "").strip():
        return
    telegram_service.send_message(
        f"Draf untuk: {task['title']}\n\n{task['draft']}",
        buttons=_draft_buttons(task_id),
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
            logger.error("Gemini gagal: %s: %s", type(e).__name__, e)
            telegram_service.send_message(
                f"Gagal membuat draf ({type(e).__name__}). Lihat log backend.",
                buttons=[[("Coba lagi", f"draft:{task_id}")]],
            )
            return

        if not task_service.save_draft(task_id, draft):
            # Status berubah selama menunggu Gemini (mis. sudah disetujui/dibatalkan).
            telegram_service.send_message(
                "Status tugas berubah saat draf dibuat, jadi draf baru tidak disimpan."
            )
            return

        send_draft(task_id)
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
        if status in task_service.LOCKED_STATUSES:
            telegram_service.send_message(f"Tugas ini sudah berstatus {status}.")
            return
        start_draft(task_id)

    elif action == "approve":
        if status in (APPROVED, FILLED):
            telegram_service.send_message("Draf ini sudah disetujui sebelumnya.")
        elif not task_service.transition(task_id, (DRAFT_READY,), APPROVED):
            telegram_service.send_message(
                f"Tugas tidak sedang menunggu persetujuan (status: {status})."
            )
        elif _robot().can_submit():
            sending = _robot().mode() == "send"
            telegram_service.send_message(
                "Draf disetujui. Robot sedang membuka EdLink untuk "
                + ("mengisi jawaban dan menekan Simpan..." if sending
                   else "uji coba (mengisi editor tanpa Simpan)...")
            )
            # Catat mode saat disetujui: persetujuan uji coba tidak boleh berubah jadi kirim sungguhan.
            task_service.set_state(f"approved_mode:{task_id}", _robot().mode())
            _robot().enqueue_submit(task_id)
        else:
            telegram_service.send_message(
                "Draf disetujui.\n\n"
                "Langkah berikutnya: buka halaman tugas ini di EdLink, lalu klik "
                "ikon extension. Draf akan ditempel ke Teks Editor; kamu yang "
                "menekan Simpan setelah memeriksanya. Tidak ada yang dikirim "
                "otomatis ke EdLink."
            )

    elif action == "retry":
        if status != APPROVED or not _robot().can_submit():
            telegram_service.send_message(
                f"Tidak bisa dicoba ulang (status: {status})."
            )
            return
        telegram_service.send_message("Robot mencoba lagi...")
        _robot().enqueue_submit(task_id)

    elif action == "final":
        label = task_service.get_state(f"final_label:{task_id}")
        if status != task_service.SUBMITTED or not label:
            telegram_service.send_message("Tombol itu tidak bisa ditekan lagi.")
            return
        task_service.clear_state(f"final_label:{task_id}")
        telegram_service.send_message(f'Robot menekan "{label}"...')
        _robot().enqueue_final(task_id, label)

    elif action == "revise":
        if status != DRAFT_READY:
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
        if status in (APPROVED, FILLED):
            telegram_service.send_message(
                "Draf ini sudah disetujui, tidak bisa dibatalkan dari sini."
            )
            return
        task_service.set_status(task_id, CANCELLED)
        task_service.clear_state(AWAITING_REVISION)
        telegram_service.send_message("Tugas dibatalkan. Tidak ada yang dikirim.")


def handle_message(chat_id: int, text: str) -> None:
    pending = task_service.get_state(AWAITING_REVISION)

    if text.startswith("/batal"):
        task_service.clear_state(AWAITING_REVISION)
        telegram_service.send_message("Revisi dibatalkan.")
        return

    if text.startswith("/robot"):
        if text.split()[1:2] == ["lanjut"]:
            _robot().resume()
            telegram_service.send_message("Robot dilanjutkan.")
        else:
            telegram_service.send_message(_robot().status_text())
        return

    if text.startswith("/cek"):
        if _robot().mode() == "off":
            telegram_service.send_message("Robot mati (EDLINK_ROBOT_MODE=off).")
        else:
            telegram_service.send_message("Memeriksa tugas baru di EdLink...")
            _robot().enqueue_scan()
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
