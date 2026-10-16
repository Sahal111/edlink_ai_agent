import pytest

from app import main, task_service
from app.database import get_connection

from conftest import TASK, b64, make_pdf


def create(client, **overrides):
    return client.post("/tasks", json={**TASK, **overrides})


def set_status(task_id, status):
    task_service.set_status(task_id, status)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_create_and_duplicates(client):
    first = create(client).json()
    assert first["created"] is True and first["status"] == "detected"

    for variant in (
        {},  # persis sama
        {"title": "  TUGAS pemrograman WEB "},  # beda huruf besar dan spasi tepi
        {"url": "https://edlink.id/panel/classes/1/post/2?lain=1"},  # beda query
    ):
        again = create(client, **variant).json()
        assert again["created"] is False
        assert again["task_id"] == first["task_id"]
        assert again["status"] == "detected"


def test_changed_title_is_new_task(client):
    a = create(client).json()
    b = create(client, title="Tugas Pemrograman Web (revisi dosen)").json()
    assert b["created"] is True and b["task_id"] != a["task_id"]


def test_url_saved_without_query_or_fragment(client):
    tid = create(client).json()["task_id"]
    task = task_service.get_task(tid)
    assert task["url"] == "https://edlink.id/panel/classes/1/post/2"


@pytest.mark.parametrize(
    "overrides",
    [{"title": ""}, {"title": "   "}, {"url": "bukan-url"}],
)
def test_validation_rejects(client, overrides):
    assert create(client, **overrides).status_code == 422


def test_too_many_attachments(client):
    att = {"name": "a.pdf", "content_base64": ""}
    assert create(client, attachments=[att] * 6).status_code == 422


def test_pdf_attachment_text_saved(client):
    pdf = make_pdf("Soal nomor satu jelaskan konsep OOP")
    r = create(
        client,
        title="Dengan PDF",
        attachments=[{"name": "soal.pdf", "content_base64": b64(pdf)}],
    ).json()
    assert "karakter terbaca" in r["attachment_info"]
    assert "OOP" in task_service.get_task(r["task_id"])["attachment_text"]


def test_attachment_added_later_updates_existing_task(client):
    tid = create(client, title="Lampiran menyusul").json()["task_id"]
    pdf = make_pdf("Isi lampiran")
    again = create(
        client,
        title="Lampiran menyusul",
        attachments=[{"name": "x.pdf", "content_base64": b64(pdf)}],
    ).json()
    assert again["created"] is False and again["attachment_updated"] is True
    assert "Isi lampiran" in task_service.get_task(tid)["attachment_text"]


def test_bad_attachments_are_reported_not_crashing(client):
    docx = {"name": "x.docx", "content_base64": b64(b"PK\x03\x04abc")}
    bad = {"name": "y.pdf", "content_base64": "###bukan-base64###"}
    r = create(client, title="Lampiran jelek", attachments=[docx, bad]).json()
    assert "belum didukung" in r["attachment_info"]
    assert "rusak" in r["attachment_info"]


def test_list_tasks_hides_draft_and_attachment_text(client):
    create(client)
    rows = client.get("/tasks").json()
    assert rows and "draft" not in rows[0] and "attachment_text" not in rows[0]


def test_concurrent_duplicate_insert_is_handled(client, monkeypatch):
    """Dua request bersamaan: yang kalah menabrak UNIQUE harus jadi duplikat, bukan 500."""
    first = create(client).json()["task_id"]

    real = main._duplicate_response
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(main, "_duplicate_response", flaky)
    r = create(client)
    assert r.status_code == 200
    assert r.json()["created"] is False and r.json()["task_id"] == first


# ---------- /draft ----------


def test_draft_not_found(client):
    assert client.post("/tasks/9999/draft").status_code == 404


def test_draft_without_gemini_key_is_502(client):
    tid = create(client).json()["task_id"]
    assert client.post(f"/tasks/{tid}/draft").status_code == 502


def test_draft_success(client, fake_ai):
    tid = create(client).json()["task_id"]
    r = client.post(f"/tasks/{tid}/draft")
    assert r.status_code == 200 and r.json()["status"] == "draft_ready"
    assert task_service.get_task(tid)["draft"] == "DRAF AWAL"


@pytest.mark.parametrize("status", ["approved", "filled", "cancelled"])
def test_draft_cannot_overwrite_locked_status(client, fake_ai, status):
    tid = create(client).json()["task_id"]
    set_status(tid, status)
    r = client.post(f"/tasks/{tid}/draft")
    assert r.status_code == 409
    assert task_service.get_task(tid)["status"] == status


def test_draft_status_changed_while_generating(client, monkeypatch):
    tid = create(client).json()["task_id"]

    def approve_midway(**kwargs):
        set_status(tid, "approved")
        return "DRAF TERLAMBAT"

    monkeypatch.setattr(main, "generate_draft", approve_midway)
    assert client.post(f"/tasks/{tid}/draft").status_code == 409
    task = task_service.get_task(tid)
    assert task["status"] == "approved" and task["draft"] != "DRAF TERLAMBAT"


# ---------- approved-draft dan filled ----------


def test_approved_draft_requires_approval(client):
    tid = create(client).json()["task_id"]
    task_service.save_draft(tid, "ISI DRAF")
    assert client.get(f"/tasks/{tid}/approved-draft").status_code == 409

    set_status(tid, "approved")
    r = client.get(f"/tasks/{tid}/approved-draft")
    assert r.status_code == 200 and r.json()["draft"] == "ISI DRAF"

    assert client.get("/tasks/9999/approved-draft").status_code == 404


def test_filled_only_from_approved(client, telegram):
    tid = create(client).json()["task_id"]
    task_service.save_draft(tid, "ISI DRAF")

    assert client.post(f"/tasks/{tid}/filled").status_code == 409  # draft_ready

    set_status(tid, "approved")
    r = client.post(f"/tasks/{tid}/filled")
    assert r.status_code == 200 and r.json()["changed"] is True
    assert task_service.get_task(tid)["status"] == "filled"
    assert "Simpan sendiri" in telegram.last or "tekan Simpan" in telegram.last

    again = client.post(f"/tasks/{tid}/filled")  # idempoten
    assert again.status_code == 200 and again.json()["changed"] is False

    assert client.post("/tasks/9999/filled").status_code == 404


def test_existing_approved_task_reports_status_to_extension(client):
    tid = create(client).json()["task_id"]
    set_status(tid, "approved")
    again = create(client).json()
    assert again["created"] is False and again["status"] == "approved"


# ---------- token ----------


def test_token_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("EDLINK_API_TOKEN", "rahasia-uji")
    assert client.get("/health").status_code == 200  # health tetap terbuka
    assert client.get("/tasks").status_code == 401
    assert client.get("/tasks", headers={"X-API-Token": "salah"}).status_code == 401
    assert client.get("/tasks", headers={"X-API-Token": "rahasia-uji"}).status_code == 200
    assert client.post("/tasks", json=TASK).status_code == 401
    assert client.get("/tasks/1/approved-draft").status_code == 401
    assert client.post("/tasks/1/filled").status_code == 401
    assert client.post("/tasks/1/draft").status_code == 401


def test_no_token_configured_allows_requests(client):
    assert client.get("/tasks").status_code == 200


def test_connections_are_closed(client):
    """Regresi M-3: get_connection harus menutup koneksi."""
    with get_connection() as conn:
        pass
    with pytest.raises(Exception):
        conn.execute("SELECT 1")
