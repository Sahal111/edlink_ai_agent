import pytest

from app import approval_service, task_service, telegram_service
from app.database import get_connection


@pytest.fixture
def task(client):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (source_key,title,description,url) "
            "VALUES ('k','Judul','isi','https://edlink.id/panel/classes/9/x')"
        )
        return cur.lastrowid


def press(action, task_id, mid=10):
    approval_service.handle_callback(111, mid, f"{action}:{task_id}", "cb")


def status(task_id):
    return task_service.get_task(task_id)["status"]


def test_full_happy_path(task, telegram, fake_ai):
    press("approve", task)
    assert status(task) == "detected" and "tidak sedang menunggu" in telegram.last

    press("draft", task)
    assert status(task) == "draft_ready"
    assert task_service.get_task(task)["draft"] == "DRAF AWAL"

    press("revise", task)
    assert task_service.get_state("awaiting_revision") == str(task)
    approval_service.handle_message(111, "lebih singkat")
    assert task_service.get_task(task)["draft"] == "DRAF REVISI"
    assert task_service.get_state("awaiting_revision") is None

    press("approve", task)
    assert status(task) == "approved"
    assert "extension" in telegram.last and "Simpan" in telegram.last
    assert len(telegram.cleared) >= 4  # tombol dihapus pada tiap callback


@pytest.mark.parametrize("locked", ["approved", "filled"])
def test_locked_task_cannot_be_changed_from_telegram(task, telegram, fake_ai, locked):
    task_service.set_status(task, locked)
    for action in ("approve", "cancel", "draft", "revise"):
        press(action, task)
        assert status(task) == locked


def test_cancel_then_nothing_else_works(task, telegram, fake_ai):
    task_service.set_status(task, "draft_ready")
    press("cancel", task)
    assert status(task) == "cancelled"
    press("approve", task)
    press("draft", task)
    assert status(task) == "cancelled"


def test_approval_during_generation_is_not_overwritten(task, telegram, monkeypatch):
    def approve_midway(**kwargs):
        task_service.set_status(task, "approved")
        return "DRAF TERLAMBAT"

    monkeypatch.setattr(approval_service, "generate_draft", approve_midway)
    approval_service.start_draft(task)
    assert status(task) == "approved"
    assert task_service.get_task(task)["draft"] != "DRAF TERLAMBAT"
    assert "tidak disimpan" in telegram.last


def test_invalid_callback_data_is_ignored(task, telegram):
    approval_service.handle_callback(111, 1, "approve:bukan-angka", "cb")
    approval_service.handle_callback(111, 1, "rusak", "cb")
    assert telegram.sent == []


def test_group_task_notice(client, telegram, fake_ai):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (source_key,title,description,url) VALUES "
            "('g','Tugas kelompok','isi','https://edlink.id/panel/classes/1/groupteams/2/3')"
        )
        tid = cur.lastrowid
    approval_service.notify_new_task(tid)
    assert any("Tugas kelompok" in text for text, _ in telegram.sent)


def test_empty_description_asks_before_drafting(client, telegram, fake_ai):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (source_key,title,url) VALUES "
            "('e','Kosong','https://edlink.id/panel/classes/1/x')"
        )
        tid = cur.lastrowid
    approval_service.notify_new_task(tid)
    text, buttons = telegram.sent[-1]
    assert "Deskripsi kosong" in text and buttons
    assert status(tid) == "detected"  # belum ada draf


def test_gemini_error_message_does_not_leak_details(task, telegram, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("rahasia-internal-xyz")

    monkeypatch.setattr(approval_service, "generate_draft", boom)
    approval_service.start_draft(task)
    assert "rahasia-internal-xyz" not in telegram.last
    assert "RuntimeError" in telegram.last


def test_filled_notification_mentions_manual_save(task, telegram):
    approval_service.notify_filled(task)
    assert "Simpan" in telegram.last and "tidak menekan" in telegram.last


# ---------- otorisasi chat Telegram ----------


@pytest.fixture
def tg_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN-PALSU")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "111")
    calls = []
    monkeypatch.setattr(
        telegram_service, "_call", lambda m, p=None, timeout=15.0: calls.append(m) or {}
    )
    return calls


def run_dispatch(update):
    import time

    hits = []
    telegram_service._dispatch(
        update,
        lambda chat, text: hits.append(("msg", chat, text)),
        lambda chat, mid, data, cid: hits.append(("cb", chat, data)),
    )
    time.sleep(0.3)
    return hits


def test_other_chats_are_ignored(tg_env):
    assert run_dispatch({"message": {"chat": {"id": 999}, "text": "halo"}}) == []
    cb = {"id": "x", "data": "approve:1", "message": {"chat": {"id": 999}, "message_id": 1}}
    assert run_dispatch({"callback_query": cb}) == []


def test_allowed_chat_is_served(tg_env):
    assert run_dispatch({"message": {"chat": {"id": 111}, "text": "halo"}}) == [
        ("msg", 111, "halo")
    ]
    cb = {"id": "y", "data": "approve:1", "message": {"chat": {"id": 111}, "message_id": 1}}
    assert run_dispatch({"callback_query": cb}) == [("cb", 111, "approve:1")]


def test_without_allowed_chat_only_start_replies(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN-PALSU")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_ID", raising=False)
    sent = []
    monkeypatch.setattr(
        telegram_service, "send_message", lambda text, buttons=None, chat_id=None: sent.append(text)
    )
    assert run_dispatch({"message": {"chat": {"id": 5}, "text": "/perintah"}}) == []
    assert sent == []
    run_dispatch({"message": {"chat": {"id": 5}, "text": "/start"}})
    assert sent and "5" in sent[0]


def test_send_draft_only_for_draft_ready(task, telegram):
    task_service.save_draft(task, "ISI DRAF")
    approval_service.send_draft(task)
    text, buttons = telegram.sent[-1]
    assert "ISI DRAF" in text and buttons

    for locked in ("approved", "filled", "cancelled", "detected"):
        telegram.sent.clear()
        task_service.set_status(task, locked)
        approval_service.send_draft(task)
        assert telegram.sent == [], locked  # tidak ada tombol untuk status ini


def test_send_draft_skips_empty_draft(task, telegram):
    task_service.set_status(task, "draft_ready")
    approval_service.send_draft(task)
    assert telegram.sent == []
