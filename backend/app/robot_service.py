"""Pekerja latar belakang untuk robot browser EdLink.

Satu thread khusus menjalankan semua pekerjaan browser secara berurutan:
- pemindaian berkala "Perlu Dikerjakan" -> tugas baru -> Telegram,
- pengisian/penyimpanan jawaban setelah pengguna menyetujui di Telegram,
- (opsional) penekanan tombol pengumpulan terpisah setelah persetujuan kedua.

EDLINK_ROBOT_MODE:
  off     robot mati (default)
  scan    hanya mendeteksi tugas baru dan mengirimnya ke Telegram
  dryrun  scan + saat disetujui, isi editor lalu BATAL (tidak menyimpan); kirim screenshot
  send    scan + saat disetujui, isi editor dan tekan Simpan; kirim screenshot
"""

import logging
import os
import queue
import random
import threading
import time

from app import approval_service, edlink_service, task_service, telegram_service
from app.attachment_service import process_attachments
from app.task_service import APPROVED, SUBMITTED, SUBMITTING

logger = logging.getLogger("edlink.robot")

MODES = ("off", "scan", "dryrun", "send")
LOGIN_FAILED_KEY = "robot_login_blocked"
MAX_NEW_PER_SCAN = 5

_jobs: "queue.Queue[tuple]" = queue.Queue()
_stop = threading.Event()
_thread: threading.Thread | None = None
_last = {"scan_at": None, "scan_result": "belum pernah", "error": ""}


class _Att:
    def __init__(self, name, content_base64):
        self.name = name
        self.content_base64 = content_base64


def mode() -> str:
    value = os.getenv("EDLINK_ROBOT_MODE", "off").strip().lower()
    return value if value in MODES else "off"


def can_submit() -> bool:
    """True bila persetujuan di Telegram akan memicu robot (dryrun/send)."""
    return mode() in ("dryrun", "send") and edlink_service.credentials_configured()


def poll_seconds() -> float:
    try:
        minutes = float(os.getenv("EDLINK_POLL_MINUTES", "15"))
    except ValueError:
        minutes = 15
    return max(minutes, 2) * 60


def status_text() -> str:
    blocked = task_service.get_state(LOGIN_FAILED_KEY)
    lines = [
        f"Robot EdLink: mode {mode()}",
        f"Kredensial: {'terisi' if edlink_service.credentials_configured() else 'BELUM diisi'}",
        f"Pemindaian terakhir: {_last['scan_at'] or '-'} ({_last['scan_result']})",
    ]
    if _last["error"]:
        lines.append(f"Kesalahan terakhir: {_last['error']}")
    if blocked:
        lines.append(
            f"DIJEDA: {blocked}\nPerbaiki penyebabnya lalu kirim /robot lanjut."
        )
    return "\n".join(lines)


# ------------------------------------------------------------- API publik

def enqueue_scan() -> None:
    _jobs.put(("scan",))


def enqueue_submit(task_id: int) -> None:
    _jobs.put(("submit", task_id))


def enqueue_final(task_id: int, label: str) -> None:
    _jobs.put(("final", task_id, label))


def resume() -> None:
    task_service.clear_state(LOGIN_FAILED_KEY)


def start() -> bool:
    global _thread
    if mode() == "off":
        logger.info("Robot EdLink nonaktif (EDLINK_ROBOT_MODE=off)")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    if not edlink_service.credentials_configured():
        logger.warning("EDLINK_ROBOT_MODE aktif tetapi EDLINK_USERNAME/PASSWORD belum diisi")
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="edlink-robot")
    _thread.start()
    logger.info("Robot EdLink dimulai (mode %s)", mode())
    return True


def stop() -> None:
    _stop.set()
    if _thread is not None and _thread.is_alive():
        _jobs.put(("stop",))


# ---------------------------------------------------------------- loop

def _loop() -> None:
    next_scan = time.monotonic() + 20  # beri waktu server naik dulu
    while not _stop.is_set():
        wait = max(next_scan - time.monotonic(), 0.5)
        try:
            job = _jobs.get(timeout=wait)
        except queue.Empty:
            job = ("scan",) if mode() != "off" else None
            if job:
                next_scan = time.monotonic() + poll_seconds() * random.uniform(0.9, 1.1)
        if job is None or job[0] == "stop":
            continue
        try:
            if job[0] == "scan":
                _do_scan()
            elif job[0] == "submit":
                _do_submit(job[1])
            elif job[0] == "final":
                _do_final(job[1], job[2])
        except Exception as e:  # jangan sampai thread mati
            logger.error("Pekerjaan robot gagal: %s: %s", type(e).__name__, e)


def _blocked() -> str | None:
    return task_service.get_state(LOGIN_FAILED_KEY)


def _handle_failure(e: Exception, context: str) -> str:
    """Catat kegagalan; login yang diblokir menjeda robot sampai /robot lanjut."""
    if isinstance(e, edlink_service.LoginBlocked):
        task_service.set_state(LOGIN_FAILED_KEY, str(e))
        message = f"Robot dijeda: {e}\nKirim /robot lanjut setelah diperbaiki."
    elif isinstance(e, edlink_service.RobotError):
        message = str(e)
    else:
        message = f"{type(e).__name__} (lihat log server)"
        logger.error("%s gagal: %s: %s", context, type(e).__name__, e)
    _last["error"] = message
    return message


def _do_scan() -> None:
    if mode() == "off" or _blocked():
        return
    if not edlink_service.credentials_configured():
        _last["scan_result"] = "kredensial kosong"
        return

    created = 0
    skipped = 0
    try:
        with edlink_service.open_session() as page:
            tasks = edlink_service.scan_tasks(page)
            fresh = [
                t for t in tasks
                if not task_service.task_exists_for_url(t["url"])
                and not task_service.get_state("unreadable:" + t["url"])
            ]
            for item in fresh[:MAX_NEW_PER_SCAN]:
                try:
                    data = edlink_service.read_task(page, item["url"])
                except edlink_service.RobotError as e:
                    skipped += 1
                    logger.info("Tugas dilewati: %s", e)
                    info = getattr(e, "info", None)
                    if info:
                        try:
                            telegram_service.send_message(
                            "Diagnosa tugas yang tidak terbaca:\n"
                            f"Alamat: {info.get('path')}\n"
                            f"Judul halaman: {info.get('title')}\n"
                            f"post-box ada: {info.get('postBox')}\n"
                            f"Teks pendek: {info.get('labels')}"
                            )
                            _send_proof(getattr(e, "proof", None), "Halaman yang tidak terbaca")
                        except Exception as send_error:
                            logger.error("Diagnosa gagal dikirim: %s", type(send_error).__name__)
                        task_service.set_state("unreadable:" + item["url"], "1")
                    continue
                text, info = process_attachments(
                    [_Att(a["name"], a["content_base64"]) for a in data.get("attachments", [])]
                )
                task_id = task_service.create_task(
                    data["title"], data.get("description", ""), data.get("due_text", ""),
                    data["url"], text, info,
                )
                if task_id is not None:
                    created += 1
                    approval_service.notify_new_task(task_id)
            more = max(len(fresh) - MAX_NEW_PER_SCAN, 0)
        _last["error"] = ""
        _last["scan_result"] = (
            f"{len(tasks)} tugas di dasbor, {created} baru"
            + (f", {skipped} gagal dibaca" if skipped else "")
            + (f", {more} menunggu pemindaian berikutnya" if more else "")
        )
    except Exception as e:
        message = _handle_failure(e, "Pemindaian")
        _last["scan_result"] = "gagal"
        if _last.get("notified") != message:
            _last["notified"] = message
            telegram_service.send_message(f"Robot gagal memindai EdLink: {message}")
    finally:
        _last["scan_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def _retry_buttons(task_id: int):
    return [[("Coba lagi", f"retry:{task_id}")]]


def _do_submit(task_id: int) -> None:
    task = task_service.get_task(task_id)
    if task is None:
        return
        # Persetujuan yang diberikan saat mode bukan "send" (mis. uji coba) tidak berlaku
    # untuk pengiriman sungguhan: minta persetujuan baru.
    if (
        mode() == "send"
        and task_service.get_state(f"approved_mode:{task_id}") != "send"
    ):
        if task_service.transition(task_id, (APPROVED,), task_service.DRAFT_READY):
            telegram_service.send_message(
                "Mode robot sekarang 'send', tetapi persetujuan sebelumnya diberikan untuk "
                "uji coba. Setujui ulang draf ini bila kamu memang ingin robot menyimpannya."
            )
            approval_service.send_draft(task_id)
        return
    # Hanya tugas yang sudah disetujui; klaim atomik agar tidak berjalan dua kali.
    if not task_service.transition(task_id, (APPROVED,), SUBMITTING):
        telegram_service.send_message(
            f"Tugas tidak dalam status disetujui (status: {task['status']}); robot tidak berjalan."
        )
        return
    if _blocked():
        task_service.transition(task_id, (SUBMITTING,), APPROVED)
        telegram_service.send_message(
            "Robot sedang dijeda (login bermasalah). Kirim /robot untuk melihat sebabnya.",
            buttons=_retry_buttons(task_id),
        )
        return

    press_save = mode() == "send"
    try:
        with edlink_service.open_session() as page:
            result = edlink_service.fill_and_save(page, task["url"], task["draft"], press_save)
    except Exception as e:
        task_service.transition(task_id, (SUBMITTING,), APPROVED)
        telegram_service.send_message(
            f"Robot gagal bekerja di EdLink: {_handle_failure(e, 'Pengiriman')}",
            buttons=_retry_buttons(task_id),
        )
        return

    proof = result.get("proof")
    if not result["ok"]:
        task_service.transition(task_id, (SUBMITTING,), APPROVED)
        _send_proof(proof, f"Gagal: {result['reason']}")
        telegram_service.send_message(
            f"Robot tidak berhasil untuk \"{task['title']}\": {result['reason']}.\n"
            "Tidak ada yang tersimpan di EdLink.",
            buttons=_retry_buttons(task_id),
        )
        return

    if not press_save:
        task_service.transition(task_id, (SUBMITTING,), APPROVED)
        _send_proof(proof, "Uji coba: teks masuk editor")
        telegram_service.send_message(
            f"Uji coba (dryrun) berhasil untuk \"{task['title']}\": teks masuk ke editor, "
            "lalu ditutup tanpa Simpan. Tidak ada yang tersimpan.\n"
            "Ubah EDLINK_ROBOT_MODE=send di server bila sudah yakin.",
            buttons=_retry_buttons(task_id),
        )
        return

    task_service.transition(task_id, (SUBMITTING,), SUBMITTED)
    _send_proof(proof, f"Setelah Simpan: {task['title']}")
    message = (
        f"Jawaban untuk \"{task['title']}\" sudah diisi dan tombol Simpan ditekan.\n"
        "Cek screenshot di atas atau halaman tugas di EdLink untuk memastikan."
    )
    final_label = result.get("final_button")
    if final_label:
        message += (
            f"\n\nAda tombol \"{final_label}\" terpisah di halaman tugas. "
            "Belum ditekan; tekan tombol di bawah bila kamu ingin robot menekannya."
        )
        telegram_service.send_message(
            message, buttons=[[(f"Tekan {final_label}", f"final:{task_id}")]]
        )
        task_service.set_state(f"final_label:{task_id}", final_label)
    else:
        telegram_service.send_message(message)


def _do_final(task_id: int, label: str) -> None:
    task = task_service.get_task(task_id)
    if task is None or task["status"] != SUBMITTED or mode() != "send":
        telegram_service.send_message("Tombol pengumpulan tidak bisa ditekan (status/mode tidak sesuai).")
        return
    try:
        with edlink_service.open_session() as page:
            result = edlink_service.press_final(page, task["url"], label)
    except Exception as e:
        telegram_service.send_message(f"Robot gagal: {_handle_failure(e, 'Pengumpulan')}")
        return
    _send_proof(result.get("proof"), f"Setelah menekan {label}")
    if result["ok"]:
        telegram_service.send_message(f"Tombol \"{label}\" ditekan. Cek screenshot untuk hasilnya.")
    else:
        telegram_service.send_message(f"Gagal: {result['reason']}")


def _send_proof(path: str | None, caption: str) -> None:
    if not path:
        return
    try:
        telegram_service.send_photo(path, caption)
    except Exception as e:
        logger.error("Gagal mengirim screenshot: %s", type(e).__name__)
