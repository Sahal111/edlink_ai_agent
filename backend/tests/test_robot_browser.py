"""Pengujian robot terhadap situs EdLink TIRUAN (browser Chromium sungguhan).

Dilewati otomatis bila Playwright/Chromium tidak terpasang.
"""

import pytest

pytest.importorskip("playwright")

from app import edlink_service, robot_service, task_service  # noqa: E402
from mock_edlink import Site  # noqa: E402

DRAFT = "Sistem operasi mengelola proses.\n\nIa juga mengelola memori & berkas."


@pytest.fixture(autouse=True)
def clean_queue():
    while not robot_service._jobs.empty():
        robot_service._jobs.get_nowait()
    yield


@pytest.fixture
def site():
    s = Site().start()
    yield s
    s.stop()


@pytest.fixture
def robot_env(monkeypatch, tmp_path, site):
    monkeypatch.setenv("EDLINK_BASE_URL", site.url)
    monkeypatch.setenv("EDLINK_USERNAME", "mhs")
    monkeypatch.setenv("EDLINK_PASSWORD", "rahasia-uji")
    monkeypatch.setenv("EDLINK_ROBOT_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setattr(edlink_service, "proof_dir", lambda: tmp_path)
    monkeypatch.setenv("EDLINK_ROBOT_NO_SANDBOX", "1")


def task_url(site):
    return f"{site.url}{__import__('mock_edlink').TASK_PATH}"


def test_login_scan_and_read(robot_env, site):
    with edlink_service.open_session() as page:
        tasks = edlink_service.scan_tasks(page)
        assert [t["url"] for t in tasks] == [task_url(site)]
        data = edlink_service.read_task(page, tasks[0]["url"])
    assert data["title"] == "TK#1 Sistem Operasi"
    assert data["due_text"].startswith("25 Okt")
    assert data["attachments"] and data["attachments"][0]["name"] == "soal.pdf"
    assert site.logins == 1


def test_session_reused_without_second_login(robot_env, site):
    with edlink_service.open_session():
        pass
    with edlink_service.open_session():
        pass
    assert site.logins == 1


def test_wrong_password_stops_without_retry(robot_env, site, monkeypatch):
    monkeypatch.setenv("EDLINK_PASSWORD", "salah")
    with pytest.raises(edlink_service.LoginBlocked, match="ditolak"):
        with edlink_service.open_session():
            pass
    assert site.logins == 1  # tidak mencoba berulang


def test_captcha_stops_before_typing_password(robot_env, site):
    site.captcha = True
    with pytest.raises(edlink_service.LoginBlocked, match="captcha"):
        with edlink_service.open_session():
            pass
    assert site.logins == 0


def test_dryrun_fills_but_does_not_save(robot_env, site):
    with edlink_service.open_session() as page:
        res = edlink_service.fill_and_save(page, task_url(site), DRAFT, press_save=False)
    assert res["ok"] and not res["saved"]
    assert site.answers == []


def test_send_presses_save(robot_env, site):
    with edlink_service.open_session() as page:
        res = edlink_service.fill_and_save(page, task_url(site), DRAFT, press_save=True)
    assert res["ok"] and res["saved"], res
    assert len(site.answers) == 1
    assert "mengelola memori & berkas" in site.answers[0]
    assert res["final_button"] == ""


def test_final_button_reported_not_pressed(robot_env, site):
    site.final_button = True
    with edlink_service.open_session() as page:
        res = edlink_service.fill_and_save(page, task_url(site), DRAFT, press_save=True)
        assert res["final_button"] == "Kumpulkan"
        assert site.finalized == 0
        out = edlink_service.press_final(page, task_url(site), "Kumpulkan")
    assert out["ok"] and site.finalized == 1


# ---- alur penuh lewat robot_service dengan Telegram palsu


def _sent_text(telegram):
    return "\n".join(t for t, _ in telegram.sent)


@pytest.fixture
def robot_tg(telegram, monkeypatch):
    photos = []
    monkeypatch.setattr(
        robot_service.telegram_service, "send_photo", lambda p, c="", chat_id=None: photos.append(c)
    )
    return photos


def test_scan_creates_task_and_notifies(robot_env, site, telegram, fake_ai, robot_tg, monkeypatch):
    monkeypatch.setenv("EDLINK_ROBOT_MODE", "scan")
    task_service  # noqa: B018
    from app.database import init_db

    init_db()
    robot_service._do_scan()
    robot_service._do_scan()  # kedua kali: tidak ada tugas baru
    with __import__("app.database", fromlist=["x"]).get_connection() as c:
        rows = c.execute("SELECT title, status, attachment_text FROM tasks").fetchall()
    assert len(rows) == 1
    assert "fungsi sistem operasi" in rows[0]["attachment_text"]
    assert "Tugas baru terdeteksi" in _sent_text(telegram)
    assert rows[0]["status"] == "draft_ready"


def test_approve_send_end_to_end(robot_env, site, telegram, fake_ai, robot_tg, monkeypatch):
    from app import approval_service
    from app.database import init_db

    monkeypatch.setenv("EDLINK_ROBOT_MODE", "send")
    init_db()
    robot_service._do_scan()
    tid = 1
    approval_service.handle_callback(1, 10, f"approve:{tid}", "cb")
    # handle_callback hanya mengantrekan; jalankan pekerjaan secara sinkron
    job = robot_service._jobs.get_nowait()
    assert job == ("submit", tid)
    robot_service._do_submit(tid)
    assert task_service.get_task(tid)["status"] == "submitted"
    assert site.answers and "DRAF AWAL" in site.answers[0]
    assert robot_tg  # screenshot terkirim


def test_dryrun_keeps_task_approved(robot_env, site, telegram, fake_ai, robot_tg, monkeypatch):
    from app import approval_service
    from app.database import init_db

    monkeypatch.setenv("EDLINK_ROBOT_MODE", "dryrun")
    init_db()
    robot_service._do_scan()
    approval_service.handle_callback(1, 10, "approve:1", "cb")
    robot_service._jobs.get_nowait()
    robot_service._do_submit(1)
    assert task_service.get_task(1)["status"] == "approved"
    assert site.answers == []
    assert "dryrun" in _sent_text(telegram)


def test_failed_save_returns_to_approved(robot_env, site, telegram, fake_ai, robot_tg, monkeypatch):
    from app import approval_service
    from app.database import init_db

    monkeypatch.setenv("EDLINK_ROBOT_MODE", "send")
    init_db()
    robot_service._do_scan()
    approval_service.handle_callback(1, 10, "approve:1", "cb")
    robot_service._jobs.get_nowait()
    monkeypatch.setattr(
        edlink_service, "fill_and_save",
        lambda *a, **k: {"ok": False, "saved": False, "reason": "uji gagal", "proof": None},
    )
    robot_service._do_submit(1)
    assert task_service.get_task(1)["status"] == "approved"
    assert "uji gagal" in _sent_text(telegram)
    assert telegram.sent[-1][1][0][0][1] == "retry:1"


def test_login_block_pauses_robot(robot_env, site, telegram, fake_ai, robot_tg, monkeypatch):
    from app.database import init_db

    monkeypatch.setenv("EDLINK_ROBOT_MODE", "scan")
    monkeypatch.setenv("EDLINK_PASSWORD", "salah")
    init_db()
    robot_service._do_scan()
    assert robot_service._blocked()
    n = site.logins
    robot_service._do_scan()  # dijeda: tidak login lagi
    assert site.logins == n
    robot_service.resume()
    assert not robot_service._blocked()
