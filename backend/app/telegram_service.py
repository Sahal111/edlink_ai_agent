import logging
import os
import threading
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Token bot ada di URL request; jangan sampai tercetak oleh logger httpx.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"
MAX_LEN = 4000  # batas Telegram 4096 karakter per pesan

_stop = threading.Event()
_thread: threading.Thread | None = None


logger = logging.getLogger("edlink.telegram")


def _log(message: str) -> None:
    logger.info("%s", message)


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def allowed_chat_id() -> int | None:
    value = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return None


def is_configured() -> bool:
    return bool(_token())


def can_notify() -> bool:
    return is_configured() and allowed_chat_id() is not None


def _call(method: str, payload: dict | None = None, timeout: float = 15.0):
    response = httpx.post(
        f"{API_BASE}/bot{_token()}/{method}",
        json=payload or {},
        timeout=timeout,
    )
    try:
        data = response.json()
    except ValueError as e:
        raise RuntimeError(
            f"Telegram {method}: respons bukan JSON (status {response.status_code})"
        ) from e

    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} gagal: {data.get('description')}")
    return data["result"]


def _split(text: str) -> list[str]:
    chunks = []
    while len(text) > MAX_LEN:
        cut = text.rfind("\n", 0, MAX_LEN)
        if cut < MAX_LEN // 2:
            cut = MAX_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def send_message(text: str, buttons=None, chat_id: int | None = None) -> None:
    """Kirim pesan teks biasa. `buttons` = [[(label, callback_data), ...], ...]
    dan dipasang pada potongan pesan terakhir."""
    chat_id = chat_id if chat_id is not None else allowed_chat_id()
    if chat_id is None or not is_configured():
        return

    chunks = _split(text)
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if buttons and i == len(chunks) - 1:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        _call("sendMessage", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except Exception as e:
        _log(f"answerCallbackQuery gagal: {type(e).__name__}")


def clear_buttons(chat_id: int, message_id: int) -> None:
    """Hapus tombol dari pesan agar tidak bisa ditekan dua kali."""
    try:
        _call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )
    except Exception as e:
        _log(f"editMessageReplyMarkup gagal: {type(e).__name__}")


def _run_safely(handler, *args) -> None:
    try:
        handler(*args)
    except Exception as e:
        logger.error("Handler Telegram gagal: %s: %s", type(e).__name__, e)


def _dispatch(update: dict, on_message, on_callback) -> None:
    allowed = allowed_chat_id()
    message = update.get("message")
    callback = update.get("callback_query")

    if message:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()

        if allowed is None:
            # Belum ada chat ID yang diizinkan: hanya bantu user menemukan ID-nya.
            if text.startswith("/start"):
                send_message(
                    f"Halo! Chat ID kamu: {chat_id}\n\n"
                    f"Isi TELEGRAM_ALLOWED_CHAT_ID={chat_id} di backend/.env "
                    "lalu restart backend.",
                    chat_id=chat_id,
                )
            return

        if chat_id != allowed:
            return  # abaikan chat lain tanpa membalas

        if text:
            threading.Thread(
                target=_run_safely, args=(on_message, chat_id, text), daemon=True
            ).start()

    elif callback:
        callback_message = callback.get("message") or {}
        chat_id = (callback_message.get("chat") or {}).get("id")

        if allowed is None or chat_id != allowed:
            answer_callback(callback["id"])
            return

        threading.Thread(
            target=_run_safely,
            args=(
                on_callback,
                chat_id,
                callback_message.get("message_id"),
                callback.get("data", ""),
                callback["id"],
            ),
            daemon=True,
        ).start()


def _poll_loop(on_message, on_callback) -> None:
    _log("Telegram polling dimulai")
    offset = None

    while not _stop.is_set():
        try:
            payload = {
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"],
            }
            if offset is not None:
                payload["offset"] = offset

            updates = _call("getUpdates", payload, timeout=35.0)
            for update in updates:
                offset = update["update_id"] + 1
                _dispatch(update, on_message, on_callback)
        except Exception as e:
            # Termasuk 409 saat dua proses backend polling bersamaan (mis. reload).
            logger.error("Polling Telegram gagal: %s: %s", type(e).__name__, e)
            _stop.wait(5)

    _log("Telegram polling berhenti")


def start_polling(on_message, on_callback) -> bool:
    global _thread

    if not is_configured():
        _log("TELEGRAM_BOT_TOKEN belum diisi, Telegram dinonaktifkan")
        return False

    if _thread is not None and _thread.is_alive():
        return True

    if allowed_chat_id() is None:
        _log(
            "TELEGRAM_ALLOWED_CHAT_ID belum diisi: kirim /start ke bot "
            "untuk melihat chat ID kamu"
        )

    _stop.clear()
    _thread = threading.Thread(
        target=_poll_loop, args=(on_message, on_callback), daemon=True
    )
    _thread.start()
    return True


def stop_polling() -> None:
    _stop.set()
