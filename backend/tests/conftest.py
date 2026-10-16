import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import approval_service, main, telegram_service  # noqa: E402

ENV_VARS = (
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_FALLBACK_MODELS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_ID",
    "EDLINK_API_TOKEN",
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Database sementara dan tanpa secret asli dari backend/.env."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("app.database.DB_PATH", tmp_path / "tasks.db")


@pytest.fixture
def client(isolated_env):
    with TestClient(main.app) as c:  # menjalankan lifespan -> init_db
        yield c


@pytest.fixture
def telegram(monkeypatch):
    """Ganti seluruh pemanggilan Telegram dengan perekam (tanpa jaringan)."""

    class Rec:
        sent: list = []
        cleared: list = []

        @property
        def last(self) -> str:
            return self.sent[-1][0] if self.sent else ""

    rec = Rec()
    rec.sent, rec.cleared = [], []
    monkeypatch.setattr(
        telegram_service,
        "send_message",
        lambda text, buttons=None, chat_id=None: rec.sent.append((text, buttons)),
    )
    monkeypatch.setattr(
        telegram_service, "clear_buttons", lambda chat_id, mid: rec.cleared.append(mid)
    )
    monkeypatch.setattr(telegram_service, "answer_callback", lambda cid, text="": None)
    monkeypatch.setattr(telegram_service, "can_notify", lambda: True)
    return rec


@pytest.fixture
def fake_ai(monkeypatch):
    """Ganti generator draf (Gemini) dengan pengganti lokal."""

    def fake(**kwargs):
        return "DRAF REVISI" if kwargs.get("revision_note") else "DRAF AWAL"

    monkeypatch.setattr(main, "generate_draft", fake)
    monkeypatch.setattr(approval_service, "generate_draft", fake)
    return fake


def make_pdf(text: str | None) -> bytes:
    """PDF minimal valid. text=None -> halaman kosong (meniru PDF hasil scan)."""
    stream = (f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET" if text else "").encode()
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<</Root 1 0 R/Size {len(objs) + 1}>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


TASK = {
    "title": "Tugas Pemrograman Web",
    "description": "Buat halaman HTML sederhana.",
    "due_text": "20 Okt 2026",
    "url": "https://edlink.id/panel/classes/1/post/2?token=RAHASIA#frag",
}
